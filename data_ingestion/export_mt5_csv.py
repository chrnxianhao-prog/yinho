from __future__ import annotations

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


def _load_dotenv(path: Path) -> None:
    """Load simple KEY=VALUE lines without putting secrets in source code."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export real OHLCV bars from a MetaTrader 5 terminal.")
    parser.add_argument("--symbols", nargs="+", default=["EURUSD", "XAUUSD"])
    parser.add_argument("--start", default="2024-01-01T00:00:00Z")
    parser.add_argument("--end", default="2026-01-01T00:00:00Z")
    parser.add_argument("--timeframe", default="M1", choices=("M1", "M5", "M15", "H1", "D1"))
    parser.add_argument("--output-dir", default="data/mt5")
    parser.add_argument("--mt5-path", default=None, help="Optional terminal executable path")
    args = parser.parse_args()
    _load_dotenv(Path(".env"))

    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise SystemExit(
            "MetaTrader5 is not installed or this is not Windows. "
            "Install the Windows package with: pip install MetaTrader5",
        ) from exc

    login = os.getenv("MT5_LOGIN")
    password = os.getenv("MT5_PASSWORD")
    server = os.getenv("MT5_SERVER")
    terminal_path = args.mt5_path or os.getenv("MT5_PATH") or os.getenv("MT5_TERMINAL_PATH")
    initialize_kwargs: dict[str, object] = {}
    if login:
        initialize_kwargs["login"] = int(login)
    if password:
        initialize_kwargs["password"] = password
    if server:
        initialize_kwargs["server"] = server
    if terminal_path:
        initialize_kwargs["path"] = terminal_path

    if not mt5.initialize(**initialize_kwargs):
        raise SystemExit(f"mt5.initialize failed: {mt5.last_error()}")

    timeframe = getattr(mt5, f"TIMEFRAME_{args.timeframe}")
    start = _parse_utc(args.start)
    end = _parse_utc(args.end)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        for symbol in args.symbols:
            if not mt5.symbol_select(symbol, True):
                raise SystemExit(f"MT5 symbol_select failed for {symbol}: {mt5.last_error()}")
            rates = mt5.copy_rates_range(symbol, timeframe, start, end)
            if rates is None or len(rates) == 0:
                # Some broker terminals return no data for copy_rates_range even
                # though chart history is available. Fall back to bounded
                # position-based chunks and then apply the requested UTC range.
                chunks: list[pd.DataFrame] = []
                position = 0
                chunk_size = 50_000
                while position < 1_000_000:
                    chunk = mt5.copy_rates_from_pos(symbol, timeframe, position, chunk_size)
                    if chunk is None or len(chunk) == 0:
                        break
                    chunk_frame = pd.DataFrame(chunk)
                    chunks.append(chunk_frame)
                    oldest = pd.to_datetime(chunk_frame["time"].min(), unit="s", utc=True)
                    position += len(chunk_frame)
                    if oldest <= pd.Timestamp(start) or len(chunk_frame) < chunk_size:
                        break
                if not chunks:
                    raise SystemExit(f"No MT5 rates for {symbol}: {mt5.last_error()}")
                frame = pd.concat(chunks, ignore_index=True)
            else:
                frame = pd.DataFrame(rates)
            frame["timestamp"] = pd.to_datetime(frame["time"], unit="s", utc=True)
            frame = frame[(frame["timestamp"] >= pd.Timestamp(start)) & (frame["timestamp"] <= pd.Timestamp(end))]
            if frame.empty:
                raise SystemExit(
                    f"MT5 history for {symbol} does not cover the requested range; "
                    "increase 'Max bars in chart' in MT5 Tools > Options > Charts",
                )
            frame["volume"] = frame["real_volume"].where(
                frame["real_volume"] > 0,
                frame["tick_volume"],
            )
            output = frame[["timestamp", "open", "high", "low", "close", "volume", "spread"]]
            output = output.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
            path = output_dir / f"{symbol.upper()}_{args.timeframe}.csv"
            output.to_csv(path, index=False)
            print(f"Saved {len(output)} real MT5 bars for {symbol}: {path.resolve()}")
            print(f"Coverage: {output['timestamp'].iloc[0]} -> {output['timestamp'].iloc[-1]}")
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
