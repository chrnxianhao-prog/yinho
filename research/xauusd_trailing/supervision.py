"""独立 Demo 守护：只恢复已经退出的运行器，不强杀、不下单、不清除人工停止。"""
from __future__ import annotations

import ctypes
import math
import os
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .runtime import RuntimeRecorder, append_event, process_probe, read_record, write_record

UTC = timezone.utc


@dataclass(frozen=True)
class RecoveryConfig:
    workspace: str
    python_executable: str
    env_path: str
    terminal_path: str
    strategy_config: str
    log_dir: str
    state_file: str
    runtime_dir: str
    enabled: bool = False
    demo_only_confirmed: bool = False
    poll_seconds: float = 15.0
    max_heartbeat_age_seconds: float = 180.0
    retry_base_seconds: float = 60.0
    retry_max_seconds: float = 900.0
    startup_grace_seconds: float = 180.0
    stop_new_entries_at: str | None = None

    def __post_init__(self) -> None:
        if self.enabled is not True or self.demo_only_confirmed is not True:
            raise ValueError("automatic recovery requires explicit Demo-only confirmation")
        for name in ("workspace", "python_executable", "env_path", "terminal_path", "strategy_config",
                     "log_dir", "state_file", "runtime_dir"):
            if not Path(getattr(self, name)).is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        for name in ("poll_seconds", "max_heartbeat_age_seconds", "retry_base_seconds", "retry_max_seconds",
                     "startup_grace_seconds"):
            value = getattr(self, name)
            if not isinstance(value, (float, int)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid recovery interval: {name}")
        if self.retry_max_seconds < self.retry_base_seconds:
            raise ValueError("maximum retry interval must not be smaller than base interval")
        if self.stop_new_entries_at is not None:
            datetime.fromisoformat(self.stop_new_entries_at)

    @classmethod
    def load(cls, path: Path) -> "RecoveryConfig":
        raw = read_record(path)
        if raw is None:
            raise ValueError("recovery config is missing")
        return cls(**{key: value for key, value in raw.items() if key != "version"})

    def save(self, path: Path) -> None:
        write_record(path, asdict(self))  # 仅路径和控制参数，没有登录信息或密钥。


def terminal_window_ready(executable: Path) -> bool:
    """只读 Windows 窗口及所属进程路径；不 initialize、不隐式启动 MT5。"""
    if os.name != "nt":
        return False
    from ctypes import wintypes

    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    found = False
    expected = os.path.normcase(str(executable.resolve()))

    @callback_type
    def inspect(window: Any, _: Any) -> bool:
        nonlocal found
        if not user.IsWindowVisible(window):
            return True
        pid = wintypes.DWORD()
        user.GetWindowThreadProcessId(window, ctypes.byref(pid))
        handle = kernel.OpenProcess(0x1000, False, pid.value)
        if not handle:
            return True
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            length = wintypes.DWORD(len(buffer))
            if kernel.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                found = os.path.normcase(buffer.value) == expected
        finally:
            kernel.CloseHandle(handle)
        return not found

    user.EnumWindows(inspect, 0)
    return found


def launch_runner(config: RecoveryConfig) -> dict[str, Any]:
    command = [config.python_executable, "-X", "utf8", "-u",
               str(Path(config.workspace) / "scripts/run_xauusd_demo_strategy.py"),
               "--confirm-demo-strategy", "--env-path", config.env_path,
               "--config", config.strategy_config, "--log-dir", config.log_dir,
               "--state-file", config.state_file, "--runtime-dir", config.runtime_dir]
    if config.stop_new_entries_at is not None:
        command.extend(["--stop-new-entries-at", config.stop_new_entries_at])
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    log_dir = Path(config.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = log_dir / f"runner_recovered_{stamp}.stdout.log"
    stderr_path = log_dir / f"runner_recovered_{stamp}.stderr.log"
    with stdout_path.open("x", encoding="utf-8") as out, stderr_path.open("x", encoding="utf-8") as err:
        child = subprocess.Popen(
            command, cwd=config.workspace, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            shell=False, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    return {"launcher_pid": child.pid, "launcher_start_token": process_probe(child.pid).get("start_token"),
            "stdout_log": str(stdout_path), "stderr_log": str(stderr_path)}


class RecoverySupervisor:
    def __init__(
        self, config: RecoveryConfig, health_check: Callable[[], dict[str, Any]], *,
        probe: Callable[[int], dict[str, Any]] = process_probe,
        terminal_ready: Callable[[Path], bool] = terminal_window_ready,
        launch: Callable[[RecoveryConfig], dict[str, Any]] = launch_runner,
    ) -> None:
        self.config = config
        self.runtime = RuntimeRecorder(Path(config.runtime_dir))
        self.status_path = self.runtime.directory / "supervisor_status.json"
        self.events_path = self.runtime.directory / "supervisor.jsonl"
        self.health_check, self.probe, self.terminal_ready, self.launch = health_check, probe, terminal_ready, launch

    def poll_once(self, *, now: datetime | None = None) -> dict[str, Any]:
        current = (now or datetime.now(UTC)).astimezone(UTC)
        previous = read_record(self.status_path) or {}
        failures = int(previous.get("consecutive_attempts", 0))
        pending = previous.get("pending_launch")
        last_attempt = previous.get("last_attempt_utc")
        result: dict[str, Any] = {
            "automatic_restart": True, "checked_at_utc": current.isoformat(),
            "supervisor_pid": os.getpid(), "supervisor_start_token": process_probe(os.getpid()).get("start_token"),
            "action": "OBSERVE", "reason": None,
        }

        def save(action: str, reason: str | None = None) -> dict[str, Any]:
            result.update(action=action, reason=reason)
            issue = [action, reason, result.get("run_id")]
            if previous.get("issue") != issue or action == "RESTART_REQUESTED":
                append_event(self.events_path, "SUPERVISOR_" + action, **result)
            write_record(self.status_path, {
                "issue": issue, "consecutive_attempts": failures, "pending_launch": pending,
                "last_attempt_utc": last_attempt, "result": result,
            })
            return result

        control = self.runtime.control()
        # 人工意图高于 PID、退出原因和自动恢复；永不自动 clear-stop。
        if control["desired_state"] == "STOPPED":
            return save("MANUAL_STOP_HELD", "EXPLICIT_USER_STOP")
        ledger = read_record(self.runtime.runtime_path)
        if ledger is None:
            return save("BLOCKED", "NO_RUN_AUDIT_REQUIRES_EXPLICIT_INITIAL_START")
        result["run_id"] = ledger.get("run_id")
        health = self.health_check()
        result["runner_health"] = health
        identity = self.probe(int(ledger.get("process_id") or 0))
        if identity.get("running") is None:
            return save("BLOCKED", "PROCESS_IDENTITY_UNVERIFIABLE")
        if identity.get("running") is True:
            if not ledger.get("process_start_token") or not identity.get("start_token"):
                return save("BLOCKED", "PROCESS_ID_REUSED_OR_UNVERIFIABLE")
            if identity["start_token"] == ledger["process_start_token"]:
                if health.get("event") == "WATCHDOG_OK":
                    failures, pending, last_attempt = 0, None, None
                    return save("HEALTHY")
                # 不强杀正在发送/等待订单的进程，避免自动重发和双实例。
                return save("LIVE_PROCESS_ALERT" if health.get("event") == "WATCHDOG_ALERT" else "OBSERVE",
                            health.get("reason"))
            result["previous_pid_reused"] = True  # 创建标识不同：旧实例已退出，不终止新占用者。
        if pending:
            pending_identity = self.probe(int(pending["launcher_pid"]))
            if pending_identity.get("running") is None:
                return save("BLOCKED", "PENDING_LAUNCH_UNVERIFIABLE")
            if pending_identity.get("running") is True:
                if not pending_identity.get("start_token") or not pending.get("launcher_start_token"):
                    return save("BLOCKED", "PENDING_LAUNCH_UNVERIFIABLE")
                if pending_identity["start_token"] == pending["launcher_start_token"]:
                    elapsed = (current - datetime.fromisoformat(last_attempt)).total_seconds()
                    if elapsed > self.config.startup_grace_seconds:
                        return save("LIVE_PROCESS_ALERT", "LAUNCH_STALLED_NO_RUN_AUDIT")
                    return save("STARTING", "WAIT_FOR_NEW_RUN_AUDIT")
        if failures and last_attempt:
            attempt_at = datetime.fromisoformat(last_attempt)
            elapsed = (current - attempt_at).total_seconds()
            interval = min(self.config.retry_max_seconds, self.config.retry_base_seconds * 2 ** min(failures - 1, 10))
            if elapsed < interval:
                result["retry_in_seconds"] = round(interval - elapsed, 1)
                return save("BACKOFF", "RESTART_RATE_LIMIT")
        if ledger.get("phase") not in {"RUNNING", "ABNORMAL_EXIT", "MANUAL_STOPPED"}:
            return save("BLOCKED", "RUNTIME_PHASE_UNVERIFIABLE")
        # 状态丢失/损坏不能用自动重启洗掉周期计数或订单不确定锁定。
        state_path = Path(self.config.state_file)
        if read_record(state_path) is None:
            return save("BLOCKED", "STRATEGY_STATE_MISSING")
        if not self.terminal_ready(Path(self.config.terminal_path)):
            return save("WAIT_MT5", "NO_VISIBLE_CONFIGURED_MT5_WINDOW")
        # 人工停止可能与启动交接同时发生，发起新进程前再次检查。
        if not self.runtime.start_allowed():
            return save("MANUAL_STOP_HELD", "EXPLICIT_USER_STOP")
        failures += 1
        last_attempt = current.isoformat()
        pending = None
        save("RESTART_PREPARED", "ABNORMAL_PROCESS_EXIT")  # 先刷盘，守护崩溃后也不能紧密重试。
        try:
            pending = self.launch(self.config)
        except Exception as exc:
            result["error_type"] = type(exc).__name__
            return save("LAUNCH_FAILED", "RUNNER_LAUNCH_FAILED")
        result.update(pending)
        return save("RESTART_REQUESTED", "ABNORMAL_PROCESS_EXIT")
