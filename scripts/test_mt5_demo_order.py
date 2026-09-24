from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import sleep

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.adapters.mt5_demo_adapter import Mt5DemoAdapter


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Open and automatically close one tiny MT5 Demo market order.",
    )
    parser.add_argument("--symbol", default="EURUSD", help="Broker symbol, e.g. EURUSD or XAUUSD")
    parser.add_argument("--side", default="BUY", choices=("BUY", "SELL"))
    parser.add_argument("--volume", type=float, default=0.01, help="Volume in broker lots")
    parser.add_argument("--hold-seconds", type=int, default=3)
    parser.add_argument(
        "--confirm-demo-order",
        action="store_true",
        help="Required second confirmation; without it no MT5 connection or order is attempted",
    )
    args = parser.parse_args()

    if not args.confirm_demo_order:
        raise SystemExit(
            "No order sent. Add --confirm-demo-order only after verifying that .env points to a Demo account.",
        )
    if args.volume <= 0:
        raise SystemExit("--volume must be positive")
    if args.hold_seconds < 0:
        raise SystemExit("--hold-seconds cannot be negative")

    adapter = Mt5DemoAdapter(ROOT / ".env")
    try:
        account = adapter.connect_demo()
        print(
            f"Connected to MT5 Demo: login={account.login} server={account.server} "
            f"currency={account.currency} balance={account.balance}",
        )
        print(f"Submitting one {args.side} {args.symbol} order, volume={args.volume} lots")
        opened = adapter.open_market(args.symbol, args.side, args.volume)
        print(f"OPEN accepted: order={opened.order_ticket} deal={opened.deal_ticket} price={opened.price}")

        position = adapter.wait_for_new_position(args.symbol)
        if args.hold_seconds:
            sleep(args.hold_seconds)
        closed = adapter.close_position(position)
        print(f"CLOSE accepted: order={closed.order_ticket} deal={closed.deal_ticket} price={closed.price}")
        print("MT5 Demo open/close smoke test completed.")
    except Exception as exc:
        raise SystemExit(f"MT5 Demo smoke test failed: {exc}") from exc
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
