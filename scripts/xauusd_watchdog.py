from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.xauusd_trailing.runtime import (
    RuntimeRecorder, append_event, classify_health, process_probe, read_record, write_record,
)


UTC = timezone.utc


def latest_heartbeat(log_dir: Path, *, run_id: str | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    latest_record: dict[str, Any] | None = None
    latest_path: Path | None = None
    latest_at: datetime | None = None
    for path in log_dir.glob("xauusd_demo_*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict) or record.get("event") != "HEARTBEAT":
                            continue
                        if run_id is not None and record.get("run_id") != run_id:
                            continue
                        stamp = datetime.fromisoformat(str(record["time_utc"]).replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            continue
                        stamp = stamp.astimezone(UTC)
                        if latest_at is None or stamp > latest_at:
                            latest_record, latest_path, latest_at = record, path, stamp
                    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                        continue
        except OSError:
            continue
    return latest_record, latest_path


def check_heartbeat(
    log_dir: Path, *, now: datetime | None = None, max_age_seconds: float = 180.0,
    runtime_dir: Path | None = None,
    probe: Callable[[int], dict[str, Any]] = process_probe,
) -> dict[str, Any]:
    if not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be finite and positive")
    current = (now or datetime.now(UTC)).astimezone(UTC)
    runtime = RuntimeRecorder(runtime_dir or log_dir / "runtime")
    try:
        ledger = read_record(runtime.runtime_path)
        if ledger is not None:
            if not ledger.get("run_id"):
                raise ValueError("runtime record is missing run_id")
            control = runtime.control()
            record, source = latest_heartbeat(log_dir, run_id=str(ledger["run_id"]))
            result = classify_health(ledger, control, record, now=current, max_age_seconds=max_age_seconds, probe=probe)
            return {
                **result, "max_age_seconds": max_age_seconds,
                "source_log": str(source) if source else ledger.get("log_path"),
                "last_heartbeat_utc": record.get("time_utc") if record else None,
                "last_known_runner_state": record.get("runner_state") if record else None,
            }
        if runtime.control_path.exists():
            runtime.control()  # 损坏的控制文件不能降级成“正常”。
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return {"event": "WATCHDOG_ALERT", "classification": "ANOMALY",
                "reason": "RUNTIME_AUDIT_UNREADABLE", "error_type": type(exc).__name__,
                "source_log": None, "automatic_restart": False}
    # 兼容旧日志：没有运行审计/手动停机凭证，任何消失的进程仍按异常处理。
    try:
        record, source = latest_heartbeat(log_dir)
    except (OSError, ValueError) as exc:
        return {"event": "WATCHDOG_ALERT", "classification": "ANOMALY",
                "reason": "HEARTBEAT_AUDIT_UNREADABLE", "error_type": type(exc).__name__,
                "source_log": None, "automatic_restart": False}
    if record is None:
        return {"event": "WATCHDOG_ALERT", "classification": "ANOMALY", "reason": "NO_HEARTBEAT",
                "age_seconds": None, "source_log": None, "automatic_restart": False}
    last = datetime.fromisoformat(str(record["time_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    age = (current - last).total_seconds()
    reason = "HEARTBEAT_IN_FUTURE" if age < 0 else "HEARTBEAT_STALE" if age > max_age_seconds else None
    try:
        identity = probe(int(record["process_id"])) if record.get("process_id") else None
    except (OSError, ValueError, TypeError):
        identity = {"running": None}
        reason = "PROCESS_STATE_UNVERIFIABLE"
    if identity is not None and identity.get("running") is not True:
        reason = "PROCESS_NOT_RUNNING" if identity.get("running") is False else "PROCESS_STATE_UNVERIFIABLE"
    if reason is None and identity is None:
        reason = "PROCESS_ID_MISSING"
    if reason is None and record.get("terminal_connected") is False:
        reason = "MT5_DISCONNECTED"
    if reason is None and (record.get("terminal_query_error_type") or record.get("position_query_error_type")):
        reason = "MT5_QUERY_FAILED"
    if reason is None and (record.get("entry_lockout") or record.get("last_runtime_error_type")):
        reason = "RUNTIME_ENTRY_LOCKOUT"
    if reason is None:
        # 旧日志没有创建标识和 run_id，PID 可能已被其它进程复用，不承诺正在运行。
        reason = "LEGACY_RUNTIME_UNVERIFIABLE"
    return {
        "event": "WATCHDOG_ALERT" if reason else "WATCHDOG_OK",
        "classification": "ANOMALY" if reason else "RUNNING", "reason": reason,
        "age_seconds": round(age, 3),
        "max_age_seconds": max_age_seconds,
        "last_heartbeat_utc": last.isoformat(),
        "source_log": str(source) if source else None,
        "process_id": record.get("process_id"),
        "process_running": identity.get("running") if identity else None,
        "last_known_runner_state": record.get("runner_state"),
        "entry_lockout": record.get("entry_lockout"),
        "risk_halt": record.get("risk_halt"),
        "automatic_restart": False,
    }


def record_check(result: dict[str, Any], alert_path: Path, status_path: Path) -> dict[str, Any]:
    """保存当前告警和历史；恢复只记事件，绝不执行重启/平仓。"""
    previous = read_record(status_path)
    issue = [result.get("run_id"), result.get("process_id"), result.get("reason")]
    abnormal = result["event"] == "WATCHDOG_ALERT"
    changed = previous is None or previous.get("issue") != issue or previous.get("abnormal") != abnormal
    result = {**result, "new_incident": bool(abnormal and changed)}
    append_event(alert_path, result["event"], **{key: value for key, value in result.items() if key != "event"})
    if abnormal and changed:
        append_event(alert_path, "RUNTIME_ANOMALY_DETECTED", reason=result.get("reason"), issue=issue)
    elif not abnormal and previous and previous.get("abnormal"):
        append_event(alert_path, "RUNTIME_ANOMALY_CLEARED", previous_issue=previous.get("issue"), automatic_restart=False)
    write_record(status_path, {"issue": issue, "abnormal": abnormal, "checked_at_utc": datetime.now(UTC).isoformat(), "result": result})
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit XAUUSD Demo process, heartbeat and manual stop; alert only, never recover")
    parser.add_argument("--log-dir", type=Path, default=Path("artifacts/xauusd_demo"))
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    parser.add_argument("--alert-log", type=Path)
    parser.add_argument("--runtime-dir", type=Path, help="Defaults to log-dir/runtime; audit only, no automatic recovery")
    args = parser.parse_args()
    if not math.isfinite(args.max_age_seconds) or args.max_age_seconds <= 0:
        parser.error("--max-age-seconds must be positive")
    log_dir = args.log_dir.resolve()
    runtime_dir = args.runtime_dir.resolve() if args.runtime_dir else log_dir / "runtime"
    result = check_heartbeat(log_dir, max_age_seconds=args.max_age_seconds, runtime_dir=runtime_dir)
    result["checked_at_utc"] = datetime.now(UTC).isoformat()
    alert_path = args.alert_log.resolve() if args.alert_log else log_dir / "watchdog.jsonl"
    try:
        result = record_check(result, alert_path, runtime_dir / "watchdog_status.json")
    except (OSError, ValueError) as exc:
        result = {"event": "WATCHDOG_ALERT", "classification": "ANOMALY", "reason": "ALERT_WRITE_FAILED",
                  "error_type": type(exc).__name__, "automatic_restart": False}
        try:
            append_event(alert_path, "WATCHDOG_ALERT", **{key: value for key, value in result.items() if key != "event"})
        except OSError:
            pass  # 磁盘/权限故障下仍输出控制台告警和非零退出码。
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["event"] == "WATCHDOG_ALERT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
