from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from datetime import datetime, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from quant_demo.adapters.mt5_demo_adapter import Mt5DemoAdapter
from research.xauusd_trailing.demo_runner import DemoStrategyRunner, next_local_time
from research.xauusd_trailing.models import BacktestConfig
from research.xauusd_trailing.runtime import ExclusiveRunnerLock, RuntimeRecorder, append_event, run_recorded


def _load_config(path: Path) -> BacktestConfig:
    import yaml

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    raw.pop("data", None)
    raw.pop("output", None)
    raw.pop("run_walk_forward", None)
    allowed = {item.name for item in fields(BacktestConfig)}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"unknown strategy config keys: {', '.join(unknown)}")
    return BacktestConfig(**raw)


def _cutoff(value: str | None, timezone_name: str) -> datetime | None:
    if value is None:
        return None
    tz = ZoneInfo(timezone_name)
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def _cycle_anchor(value: str | None, timezone_name: str) -> datetime:
    tz = ZoneInfo(timezone_name)
    if value is None:
        return next_local_time(datetime.now(timezone.utc), timezone_name, time(9, 0))
    parsed = datetime.fromisoformat(value)
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed.astimezone(tz)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the XAUUSD strategy on a Demo account only; never use for live trading."
    )
    parser.add_argument("--config", type=Path, default=ROOT / "research/xauusd_trailing/config.example.yaml")
    parser.add_argument("--env-path", type=Path, default=ROOT / ".env")
    parser.add_argument("--log-dir", type=Path, default=ROOT / "artifacts/xauusd_demo")
    parser.add_argument("--state-file", type=Path, default=ROOT / "artifacts/xauusd_demo/state.json")
    parser.add_argument("--runtime-dir", type=Path, help="Runtime audit/control directory; defaults to log-dir/runtime")
    parser.add_argument("--stop-new-entries-at", help="Optional local ISO datetime after which new entries are disabled")
    parser.add_argument("--cycle-anchor-local", help="Cycle boundary anchor; defaults to the next 09:00 in strategy timezone")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument(
        "--lockout-recovery-polls", type=int, default=60,
        help="Consecutive successful polls required to auto-clear entry lockout",
    )
    parser.add_argument("--reset-lockout", action="store_true", help="Manually clear a persisted entry lockout at startup")
    parser.add_argument("--reset-risk-halt", action="store_true", help="Manually clear the persisted account risk halt at startup")
    parser.add_argument(
        "--heartbeat-seconds",
        type=float,
        default=60.0,
        help="Interval between liveness records; does not change configured entry check frequency",
    )
    parser.add_argument(
        "--confirm-demo-strategy", action="store_true",
        help="Required explicit confirmation before any strategy order can be sent to MT5 Demo.",
    )
    args = parser.parse_args()
    if not args.confirm_demo_strategy:
        raise SystemExit("No connection or order attempted. Pass --confirm-demo-strategy only for the verified Demo account.")

    runtime = RuntimeRecorder((args.runtime_dir or args.log_dir / "runtime").resolve())
    try:
        with ExclusiveRunnerLock(args.state_file.resolve()):
            if not runtime.start_allowed():
                print(json.dumps({"manual_stop_active": True, "connection_attempted": False, "automatic_restart": False}), flush=True)
                return 0
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log_path = args.log_dir.resolve() / f"xauusd_demo_{stamp}.jsonl"
            runtime.start(log_path)
            code = run_recorded(runtime, lambda: _execute_strategy(args, runtime, log_path))
            print(json.dumps({"runner_exit_code": code, "run_id": runtime.run_id, "automatic_restart": False}), flush=True)
            return code
    except Exception as exc:
        recorded = False
        try:
            append_event(runtime.events_path, "RUNTIME_AUDIT_OR_LOCK_FAILURE", error_type=type(exc).__name__)
            recorded = True
        except OSError:
            pass  # 审计目录不可写时至少保留非零退出和控制台告警。
        print(json.dumps({"runner_failed": True, "error_type": type(exc).__name__,
                          "audit_recorded": recorded, "automatic_restart": False}), flush=True)
        return 1


def _execute_strategy(args: argparse.Namespace, runtime: RuntimeRecorder, log_path: Path) -> str | None:
    config = _load_config(args.config.resolve())
    if config.symbol != "XAUUSD":
        raise SystemExit("This Demo runner is restricted to the XAUUSD strategy symbol.")
    cutoff = _cutoff(args.stop_new_entries_at, config.timezone)
    anchor = _cycle_anchor(args.cycle_anchor_local, config.timezone)
    adapter = Mt5DemoAdapter(args.env_path.resolve())
    try:
        runner = DemoStrategyRunner(
            adapter, config, stop_new_entries_at=cutoff, cycle_anchor_at=anchor,
            log_path=log_path, state_path=args.state_file.resolve(), poll_seconds=args.poll_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            lockout_recovery_polls=args.lockout_recovery_polls,
            reset_lockout=args.reset_lockout,
            reset_risk_halt=args.reset_risk_halt,
            cycle_anchor_explicit=args.cycle_anchor_local is not None,
            stop_requested=runtime.manual_stop_requested, run_id=runtime.run_id,
        )
        print(json.dumps({
            "demo_only": True, "run_id": runtime.run_id,
            "new_entries_cutoff_local": cutoff.isoformat() if cutoff else None,
            "cycle_anchor_local": anchor.isoformat(),
            "heartbeat_interval_seconds": args.heartbeat_seconds,
            "state_file": str(args.state_file.resolve()),
            "reset_lockout": args.reset_lockout,
            "reset_risk_halt": args.reset_risk_halt,
            "session_log": str(log_path), "runtime_dir": str(runtime.directory),
            "automatic_restart": False,
        }, ensure_ascii=False), flush=True)
        runner.run()
        return runner.exit_reason
    finally:
        adapter.close()


if __name__ == "__main__":
    raise SystemExit(main())
