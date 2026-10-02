"""独立 Windows 任务入口；自动恢复已退出的 Demo，人工停止持续有效。"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research.xauusd_trailing.runtime import ExclusiveRunnerLock, append_event
from research.xauusd_trailing.supervision import RecoveryConfig, RecoverySupervisor
from scripts.xauusd_watchdog import check_heartbeat, record_check


def main() -> int:
    parser = argparse.ArgumentParser(description="XAUUSD Demo fault recovery; manual stop always wins")
    parser.add_argument("--service-config", type=Path, default=ROOT / "artifacts/xauusd_demo/runtime/recovery_config.json")
    parser.add_argument("--configure", action="store_true")
    parser.add_argument("--confirm-demo-auto-recovery", action="store_true")
    parser.add_argument("--python-executable", type=Path)
    parser.add_argument("--env-path", type=Path)
    parser.add_argument("--terminal-path", type=Path)
    parser.add_argument("--once", action="store_true", help="One observation, still subject to explicit saved recovery permission")
    args = parser.parse_args()
    path = args.service_config.resolve()
    if args.configure:
        if not args.confirm_demo_auto_recovery or not all((args.python_executable, args.env_path, args.terminal_path)):
            parser.error("configure requires explicit Demo recovery confirmation and interpreter/env/terminal paths")
        config = RecoveryConfig(
            workspace=str(ROOT), python_executable=str(args.python_executable.resolve()),
            env_path=str(args.env_path.resolve()), terminal_path=str(args.terminal_path.resolve()),
            strategy_config=str(ROOT / "research/xauusd_trailing/config.example.yaml"),
            log_dir=str(ROOT / "artifacts/xauusd_demo"), state_file=str(ROOT / "artifacts/xauusd_demo/state.json"),
            runtime_dir=str(path.parent), enabled=True, demo_only_confirmed=True,
        )
        for name in ("python_executable", "env_path", "terminal_path", "strategy_config"):
            if not Path(getattr(config, name)).is_file():
                parser.error(f"configured file does not exist: {name}")
        config.save(path)
        append_event(path.parent / "supervisor.jsonl", "AUTO_RECOVERY_EXPLICITLY_ENABLED", automatic_restart=True)
        return 0
    config = RecoveryConfig.load(path)
    if Path(config.workspace).resolve() != ROOT or Path(config.runtime_dir).resolve() != path.parent:
        raise ValueError("supervisor config belongs to another workspace/runtime")
    log_dir, runtime_dir = Path(config.log_dir), Path(config.runtime_dir)

    def health() -> dict:
        result = check_heartbeat(log_dir, runtime_dir=runtime_dir, max_age_seconds=config.max_heartbeat_age_seconds)
        return record_check(result, log_dir / "watchdog.jsonl", runtime_dir / "watchdog_status.json")

    supervisor = RecoverySupervisor(config, health)
    # 独立排他锁：计划任务重复触发不会建立第二个守护。
    with ExclusiveRunnerLock(runtime_dir / "supervisor"):
        append_event(supervisor.events_path, "SUPERVISOR_STARTED", process_id=os.getpid(), automatic_restart=True)
        while True:
            try:
                supervisor.poll_once()
            except Exception as exc:
                append_event(supervisor.events_path, "SUPERVISOR_CHECK_FAILED", error_type=type(exc).__name__,
                             automatic_restart=True, restart_attempted=False)
                if args.once:
                    return 1
            if args.once:
                return 0
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # pythonw 没有控制台，不输出可能带密码的异常消息。
        append_event(ROOT / "artifacts/xauusd_demo/runtime/supervisor.jsonl", "SUPERVISOR_FATAL",
                     error_type=type(exc).__name__, automatic_restart=True)
        raise SystemExit(1) from None
