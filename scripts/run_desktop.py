from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.adapters.mt5_demo_adapter import load_dotenv


def _same_terminal_is_running(terminal_path: Path) -> bool:
    if os.name != "nt":
        return False
    query = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'terminal64.exe' } | "
        "Select-Object -ExpandProperty ExecutablePath"
    )
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", query],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    target = os.path.normcase(os.path.abspath(terminal_path))
    return any(
        line.strip() and os.path.normcase(os.path.abspath(line.strip())) == target
        for line in result.stdout.splitlines()
    )


def _start_mt5_terminal() -> None:
    raw_path = os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH")
    if not raw_path:
        print("MT5_PATH is not configured; opening the analysis window only.")
        return
    terminal_path = Path(raw_path).expanduser()
    if not terminal_path.is_file():
        print(f"Configured MT5 terminal was not found: {terminal_path}")
        return
    if _same_terminal_is_running(terminal_path):
        print(f"MT5 terminal already running: {terminal_path}")
        return
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) if os.name == "nt" else 0
    subprocess.Popen(
        [str(terminal_path)],
        cwd=str(terminal_path.parent),
        close_fds=True,
        creationflags=flags,
    )
    print(f"Started MT5 terminal: {terminal_path}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    _start_mt5_terminal()

    # The server is hard-bound to loopback; a browser token is not needed in
    # this local desktop process. Network-facing run_web.py keeps its token gate.
    os.environ["WEB_API_TOKEN"] = ""
    os.environ["WEB_HOST"] = "127.0.0.1"
    port = int(os.getenv("WEB_PORT", "8787"))
    base_url = f"http://127.0.0.1:{port}"

    try:
        import uvicorn
        import webview
    except ImportError as exc:
        raise SystemExit(
            r"Desktop dependencies are missing. Run: .\.venv\Scripts\python.exe -m pip install -r requirements.txt",
        ) from exc

    server = uvicorn.Server(
        uvicorn.Config(
            "quant_demo.web_app:app",
            host="127.0.0.1",
            port=port,
            log_level="warning",
            access_log=False,
        ),
    )
    server_thread = Thread(target=server.run, name="quant-desktop-api", daemon=True)
    server_thread.start()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not server.started:
        if not server_thread.is_alive():
            raise SystemExit("本地 API 服务启动失败，请检查端口是否被占用。")
        time.sleep(0.2)
    if not server.started:
        server.should_exit = True
        raise SystemExit("本地 API 服务启动超时。")

    window = webview.create_window(
        "银禾量化交易工作台 · MT5 Demo",
        f"{base_url}/",
        width=1600,
        height=1000,
        min_size=(1120, 740),
        text_select=True,
        confirm_close=True,
    )
    window.events.closed += lambda *_args: setattr(server, "should_exit", True)
    print(f"Desktop terminal ready at {base_url} (loopback only).")
    try:
        webview.start()
    finally:
        server.should_exit = True
        server_thread.join(timeout=8)


if __name__ == "__main__":
    main()
