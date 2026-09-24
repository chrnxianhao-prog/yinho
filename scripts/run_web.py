from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from pathlib import Path
from threading import Timer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.adapters.mt5_demo_adapter import load_dotenv


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the multi-market paper trading terminal.")
    parser.add_argument("--host", default=os.getenv("WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("WEB_PORT", "8787")))
    parser.add_argument("--open-browser", action="store_true", help="Open the terminal in the default browser")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")

    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if args.host not in local_hosts and not os.getenv("WEB_API_TOKEN"):
        raise SystemExit(
            "Refusing non-local bind without WEB_API_TOKEN. Set a long random token in .env first.",
        )

    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit("Install web dependencies with: pip install -r requirements.txt") from exc

    print(f"Starting multi-market paper terminal at http://{args.host}:{args.port}")
    print("A-share and futures orders are local paper only; MT5 accepts Demo accounts only.")
    print("Live QMT/CTP execution is disabled until broker onboarding is complete.")
    if args.open_browser:
        browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        Timer(1.2, lambda: webbrowser.open(f"http://{browser_host}:{args.port}")).start()
    uvicorn.run("quant_demo.web_app:app", host=args.host, port=args.port, reload=False)


if __name__ == "__main__":
    main()
