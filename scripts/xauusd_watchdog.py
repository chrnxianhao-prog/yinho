from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


UTC = timezone.utc


def latest_heartbeat(log_dir: Path) -> tuple[dict[str, Any] | None, Path | None]:
    latest_record: dict[str, Any] | None = None
    latest_path: Path | None = None
    latest_at: datetime | None = None
    for path in log_dir.glob("xauusd_demo_*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        if record.get("event") != "HEARTBEAT":
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
    log_dir: Path, *, now: datetime | None = None, max_age_seconds: float = 180.0
) -> dict[str, Any]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    record, source = latest_heartbeat(log_dir)
    if record is None:
        return {"event": "WATCHDOG_ALERT", "reason": "NO_HEARTBEAT", "age_seconds": None, "source_log": None}
    last = datetime.fromisoformat(str(record["time_utc"]).replace("Z", "+00:00")).astimezone(UTC)
    age = max(0.0, (current - last).total_seconds())
    return {
        "event": "WATCHDOG_ALERT" if age > max_age_seconds else "WATCHDOG_OK",
        "reason": "HEARTBEAT_STALE" if age > max_age_seconds else None,
        "age_seconds": round(age, 3),
        "max_age_seconds": max_age_seconds,
        "last_heartbeat_utc": last.isoformat(),
        "source_log": str(source) if source else None,
        "runner_state": record.get("runner_state"),
        "entry_lockout": record.get("entry_lockout"),
        "risk_halt": record.get("risk_halt"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Alert only when XAUUSD Demo runner heartbeats are stale")
    parser.add_argument("--log-dir", type=Path, default=Path("artifacts/xauusd_demo"))
    parser.add_argument("--max-age-seconds", type=float, default=180.0)
    parser.add_argument("--alert-log", type=Path)
    args = parser.parse_args()
    if args.max_age_seconds <= 0:
        parser.error("--max-age-seconds must be positive")
    log_dir = args.log_dir.resolve()
    result = check_heartbeat(log_dir, max_age_seconds=args.max_age_seconds)
    result["checked_at_utc"] = datetime.now(UTC).isoformat()
    alert_path = args.alert_log.resolve() if args.alert_log else log_dir / "watchdog.jsonl"
    alert_path.parent.mkdir(parents=True, exist_ok=True)
    with alert_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 1 if result["event"] == "WATCHDOG_ALERT" else 0


if __name__ == "__main__":
    raise SystemExit(main())
