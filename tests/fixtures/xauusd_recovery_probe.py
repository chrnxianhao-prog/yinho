"""仅供进程恢复集成测试：没有 MT5、账户、行情和下单代码。"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.environ["XAUUSD_TEST_CODE_ROOT"])
from research.xauusd_trailing.runtime import ExclusiveRunnerLock, RuntimeRecorder, append_event, read_record, run_recorded

parser = argparse.ArgumentParser()
parser.add_argument("--confirm-demo-strategy", action="store_true")
for name in ("env-path", "config", "log-dir", "state-file", "runtime-dir"):
    parser.add_argument("--" + name, type=Path)
args = parser.parse_args()
runtime = RuntimeRecorder(args.runtime_dir)


def simulate() -> str:
    deadline = time.monotonic() + 20  # 测试失败也不会留下无限存活的假运行器。
    while time.monotonic() < deadline:
        if runtime.manual_stop_requested():
            return "USER_MANUAL_STOP"
        command = read_record(args.env_path)
        if command and command.get("crash"):
            raise RuntimeError("intentional isolated test failure")
        append_event(
            args.log_dir / "xauusd_demo_probe.jsonl", "HEARTBEAT", run_id=runtime.run_id,
            process_id=os.getpid(), terminal_connected=True,
        )
        time.sleep(0.05)
    return "TEST_TIMEOUT"


with ExclusiveRunnerLock(args.state_file):
    if not runtime.start_allowed():
        raise SystemExit(0)
    runtime.start(args.log_dir / "xauusd_demo_probe.jsonl")
    raise SystemExit(run_recorded(runtime, simulate))
