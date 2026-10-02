"""显式人工停止/解除停止标记；不下单、不平仓、不启动进程。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.xauusd_trailing.runtime import RuntimeRecorder, read_record


def main() -> int:
    parser = argparse.ArgumentParser(description="Recorded user stop for XAUUSD Demo; never restarts or closes positions")
    parser.add_argument("action", choices=("status", "stop", "clear-stop"))
    parser.add_argument("--runtime-dir", type=Path, default=ROOT / "artifacts/xauusd_demo/runtime")
    parser.add_argument("--confirm-manual-stop", action="store_true")
    parser.add_argument("--confirm-demo-resume-intent", action="store_true")
    args = parser.parse_args()
    runtime = RuntimeRecorder(args.runtime_dir.resolve())
    try:
        if args.action == "stop":
            request = runtime.request_manual_stop(confirmed=args.confirm_manual_stop)
            result = {"manual_stop_request": request, "acknowledged": False,
                      "note": "Runner must acknowledge; no forced close or process termination was performed."}
        elif args.action == "clear-stop":
            runtime.clear_manual_stop(confirmed=args.confirm_demo_resume_intent)
            result = {"manual_stop_cleared": True, "runner_started": False, "automatic_restart": False}
        else:
            result = {"control": runtime.control(), "runtime": read_record(runtime.runtime_path),
                      "note": "Desired state is an intent, not proof that a runner is alive; use watchdog for health."}
    except (OSError, ValueError) as exc:
        print(json.dumps({"control_failed": True, "error_type": type(exc).__name__, "automatic_restart": False}), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
