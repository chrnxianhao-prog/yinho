"""运行审计，不接入 MT5、不下单，也不自动启动或结束任何进程。"""
from __future__ import annotations

import ctypes
import json
import os
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


UTC = timezone.utc
VERSION = 1


def read_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("version") != VERSION:
        raise ValueError(f"invalid runtime record: {path.name}")
    return value


def write_record(path: Path, value: dict[str, Any]) -> None:
    """先刷盘再原子替换；磁盘/权限异常不能伪装成正常退出。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump({"version": VERSION, **value}, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def append_event(path: Path, event: str, **fields: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"time_utc": datetime.now(UTC).isoformat(), "event": event, **fields}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def process_probe(pid: int) -> dict[str, Any]:
    """只读存活/创建标识。Windows 不使用 os.kill(pid, 0)，避免误结束进程。"""
    if pid <= 0:
        return {"running": False, "start_token": None}
    if os.name == "nt":
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            return {"running": False if error == 87 else None, "start_token": None, "error_code": error}
        try:
            code = wintypes.DWORD()
            if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
                return {"running": None, "start_token": None, "error_code": ctypes.get_last_error()}
            times = [wintypes.FILETIME() for _ in range(4)]
            token = None
            if kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
                token = str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
            return {"running": code.value == 259, "start_token": token, "exit_code": code.value}
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)  # POSIX 的信号 0 仅检查权限/存活。
    except ProcessLookupError:
        return {"running": False, "start_token": None}
    except PermissionError:
        return {"running": None, "start_token": None}
    token = None
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            fields = stat.read_text().rsplit(")", 1)[1].split()
            if fields[0] == "Z":
                return {"running": False, "start_token": fields[19]}
            token = fields[19]
        except (OSError, IndexError):
            pass
    return {"running": True, "start_token": token}


class ExclusiveRunnerLock:
    """同一策略 state 文件只允许一个运行器；进程被强杀后由操作系统释放锁。"""

    def __init__(self, state_path: Path) -> None:
        self.path = state_path.with_name(state_path.name + ".runner.lock")
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveRunnerLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another XAUUSD runner owns this strategy state; no connection attempted") from exc
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


class RuntimeRecorder:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.runtime_path = directory / "runtime.json"
        self.control_path = directory / "control.json"
        self.events_path = directory / "lifecycle.jsonl"
        self.run_id: str | None = None

    def control(self) -> dict[str, Any]:
        value = read_record(self.control_path) or {"desired_state": "RUNNING"}
        if value.get("desired_state") not in {"RUNNING", "STOPPED"}:
            raise ValueError("invalid desired_state; refusing to continue")
        if value["desired_state"] == "STOPPED" and value.get("manual_confirmation") is not True:
            raise ValueError("stop intent lacks explicit manual confirmation")
        return value

    def start_allowed(self) -> bool:
        return self.control()["desired_state"] == "RUNNING"

    def start(self, log_path: Path) -> None:
        if not self.start_allowed():
            raise RuntimeError("manual stop remains active; do not automatically resume Demo")
        previous = read_record(self.runtime_path)
        if previous is not None and previous.get("phase") == "RUNNING":
            identity = process_probe(int(previous.get("process_id") or 0))
            token = previous.get("process_start_token")
            same_process = identity.get("running") is True and (
                token is None or identity.get("start_token") == token
            )
            if same_process or identity.get("running") is None:
                raise RuntimeError("previous runner is alive or unverifiable; refusing to replace its audit record")
            # 强杀/断电无法执行 finally；下一次显式启动也必须补记旧运行异常。
            append_event(
                self.events_path, "RUNTIME_PREVIOUS_EXIT_UNRECORDED",
                run_id=previous.get("run_id"), process_id=previous.get("process_id"),
                classification="ANOMALY", reason="PROCESS_EXIT_WITHOUT_FINISH_RECORD",
                exit_time_known=False, automatic_restart=False,
            )
        self.run_id = uuid.uuid4().hex
        identity = process_probe(os.getpid())
        write_record(self.runtime_path, {
            "run_id": self.run_id, "process_id": os.getpid(),
            "process_start_token": identity.get("start_token"),
            "started_at_utc": datetime.now(UTC).isoformat(), "phase": "RUNNING",
            "log_path": str(log_path), "automatic_restart": False,
        })
        append_event(self.events_path, "RUNTIME_STARTED", run_id=self.run_id, process_id=os.getpid())

    def manual_stop_requested(self) -> bool:
        value = self.control()
        return (
            value["desired_state"] == "STOPPED" and value.get("target_run_id") == self.run_id
            and self.run_id is not None and bool(value.get("request_id"))
        )

    def request_manual_stop(self, *, confirmed: bool) -> dict[str, Any]:
        if not confirmed:
            raise ValueError("manual stop requires explicit human confirmation")
        running = read_record(self.runtime_path)
        value = {
            "desired_state": "STOPPED", "manual_confirmation": True,
            "request_id": uuid.uuid4().hex, "target_run_id": running.get("run_id") if running else None,
            "requested_at_utc": datetime.now(UTC).isoformat(),
        }
        write_record(self.control_path, value)
        append_event(self.events_path, "USER_MANUAL_STOP_REQUESTED", **value)
        return value

    def clear_manual_stop(self, *, confirmed: bool) -> None:
        if not confirmed:
            raise ValueError("clearing manual stop requires explicit Demo resume confirmation")
        write_record(self.control_path, {"desired_state": "RUNNING", "requested_at_utc": datetime.now(UTC).isoformat()})
        append_event(self.events_path, "USER_RESUME_INTENT_RECORDED", automatic_restart=False)

    def finish(
        self, reason: str, exit_code: int, *, error_type: str | None = None,
        stack_locations: list[dict[str, Any]] | None = None,
    ) -> int:
        control = self.control()
        manual = (
            reason == "USER_MANUAL_STOP" and exit_code == 0
            and control["desired_state"] == "STOPPED" and self.run_id is not None
            and control.get("target_run_id") == self.run_id and bool(control.get("request_id"))
        )
        code = 0 if manual else (exit_code or 1)
        value = read_record(self.runtime_path)
        if value is None or value.get("run_id") != self.run_id:
            raise RuntimeError("runtime identity changed; cannot mark a normal exit")
        value.update(
            phase="MANUAL_STOPPED" if manual else "ABNORMAL_EXIT", exit_code=code,
            exit_reason=reason, error_type=error_type, ended_at_utc=datetime.now(UTC).isoformat(),
            classification="USER_MANUAL_STOP" if manual else "ANOMALY", automatic_restart=False,
            stack_locations=stack_locations or [],
            manual_stop_request_id=control.get("request_id") if manual else None,
            manual_stop_acknowledgement=dict(control) if manual else None,
        )
        write_record(self.runtime_path, value)
        append_event(self.events_path, "RUNTIME_MANUAL_STOPPED" if manual else "RUNTIME_ABNORMAL_EXIT", **value)
        return code


def run_recorded(
    runtime: RuntimeRecorder, operation: Callable[[], str | None],
) -> int:
    """不做重试/重启。只留退出类型与调用位置，不记录密钥、locals 或异常消息。"""
    try:
        reason = operation()
    except KeyboardInterrupt:
        return runtime.finish("UNCONFIRMED_INTERRUPT", 130, error_type="KeyboardInterrupt")
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return runtime.finish("UNEXPECTED_SYSTEM_EXIT", code, error_type="SystemExit")
    except Exception as exc:
        locations = [
            {"file": frame.filename, "line": frame.lineno, "function": frame.name}
            for frame in traceback.extract_tb(exc.__traceback__)
        ]
        return runtime.finish("UNHANDLED_EXCEPTION", 1, error_type=type(exc).__name__, stack_locations=locations)
    return runtime.finish(reason or "UNEXPECTED_CLEAN_EXIT", 0)


def classify_health(
    runtime: dict[str, Any], control: dict[str, Any], heartbeat: dict[str, Any] | None,
    *, now: datetime, max_age_seconds: float,
    probe: Callable[[int], dict[str, Any]] = process_probe,
) -> dict[str, Any]:
    """只承认已确认且已被本次运行接受的手动停止；其他退出默认异常。"""
    result: dict[str, Any] = {
        "event": "WATCHDOG_ALERT", "classification": "ANOMALY", "reason": None,
        "run_id": runtime.get("run_id"), "process_id": runtime.get("process_id"),
        "automatic_restart": False, "age_seconds": None,
    }
    if runtime.get("phase") == "MANUAL_STOPPED":
        # 不能用后补的停止标记，给先前的不明退出洗掉异常。
        acknowledgement = runtime.get("manual_stop_acknowledgement") or {}
        if not isinstance(acknowledgement, dict):
            return {**result, "reason": "MANUAL_STOP_UNVERIFIABLE"}
        if (acknowledgement.get("manual_confirmation") is True
                and acknowledgement.get("desired_state") == "STOPPED"
                and acknowledgement.get("target_run_id") == runtime.get("run_id")
                and acknowledgement.get("request_id") == runtime.get("manual_stop_request_id")
                and runtime.get("manual_stop_request_id")
                and runtime.get("exit_reason") == "USER_MANUAL_STOP"
                and runtime.get("exit_code") == 0
                and runtime.get("classification") == "USER_MANUAL_STOP"):
            return {**result, "event": "WATCHDOG_MANUAL_STOP", "classification": "USER_MANUAL_STOP"}
        return {**result, "reason": "MANUAL_STOP_UNVERIFIABLE"}
    if runtime.get("phase") == "ABNORMAL_EXIT":
        return {**result, "reason": "UNEXPECTED_EXIT", "exit_reason": runtime.get("exit_reason"),
                "exit_code": runtime.get("exit_code"), "error_type": runtime.get("error_type")}
    if runtime.get("phase") != "RUNNING":
        return {**result, "reason": "RUNTIME_PHASE_INVALID"}
    identity = probe(int(runtime.get("process_id") or 0))
    result["process_running"] = identity.get("running")
    if identity.get("running") is not True:
        reason = "PROCESS_NOT_RUNNING" if identity.get("running") is False else "PROCESS_STATE_UNVERIFIABLE"
        return {**result, "reason": reason}
    token = runtime.get("process_start_token")
    if token is None or identity.get("start_token") != token:
        return {**result, "reason": "PROCESS_ID_REUSED_OR_UNVERIFIABLE"}
    if heartbeat is not None and heartbeat.get("run_id") != runtime.get("run_id"):
        heartbeat = None
    if heartbeat is None:
        started = datetime.fromisoformat(str(runtime["started_at_utc"]))
        if started.tzinfo is None:
            raise ValueError("runtime start time must include timezone")
        age = (now - started).total_seconds()
        if 0 <= age <= max_age_seconds:
            return {**result, "event": "WATCHDOG_STARTING", "classification": "STARTING", "age_seconds": age}
        return {**result, "reason": "NO_HEARTBEAT_FOR_CURRENT_RUN", "age_seconds": age}
    last = datetime.fromisoformat(str(heartbeat["time_utc"]).replace("Z", "+00:00"))
    if last.tzinfo is None:
        raise ValueError("heartbeat must include timezone")
    age = (now - last).total_seconds()
    result["age_seconds"] = round(age, 3)
    if age < 0:
        return {**result, "reason": "HEARTBEAT_IN_FUTURE"}
    if age > max_age_seconds:
        return {**result, "reason": "HEARTBEAT_STALE"}
    if heartbeat.get("process_id") != runtime.get("process_id"):
        return {**result, "reason": "HEARTBEAT_PROCESS_ID_MISMATCH"}
    if heartbeat.get("terminal_connected") is False:
        return {**result, "reason": "MT5_DISCONNECTED"}
    if heartbeat.get("terminal_query_error_type") or heartbeat.get("position_query_error_type"):
        return {**result, "reason": "MT5_QUERY_FAILED"}
    if heartbeat.get("last_runtime_error_type") or heartbeat.get("entry_lockout"):
        return {**result, "reason": "RUNTIME_ENTRY_LOCKOUT"}
    if control.get("desired_state") == "STOPPED":
        if control.get("target_run_id") != runtime.get("run_id"):
            return {**result, "reason": "STOP_INTENT_TARGET_MISMATCH"}
        requested = datetime.fromisoformat(str(control["requested_at_utc"]))
        if requested.tzinfo is None:
            raise ValueError("manual stop request must include timezone")
        if (now - requested).total_seconds() > max_age_seconds:
            return {**result, "reason": "MANUAL_STOP_NOT_ACKNOWLEDGED"}
        return {**result, "event": "WATCHDOG_MANUAL_STOP_PENDING", "classification": "STOPPING"}
    # 熔断/周期暂停/休市中仍有心跳，不等于进程停止，也不触发自动交易恢复。
    return {**result, "event": "WATCHDOG_OK", "classification": "RUNNING"}
