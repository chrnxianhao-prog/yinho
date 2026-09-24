from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "日期": "timestamp",
        "开盘价": "open",
        "最高价": "high",
        "最低价": "low",
        "收盘价": "close",
        "成交量": "volume",
        "持仓量": "open_interest",
        "动态结算价": "settle",
        "结算价": "settle",
        "date": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
        "hold": "open_interest",
    }
    output = frame.rename(columns=mapping).copy()
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in output.columns]
    if missing:
        raise ValueError(f"AKShare futures response is missing columns: {', '.join(missing)}")
    for column in ("open", "high", "low", "close", "volume", "open_interest"):
        if column in output.columns:
            output[column] = pd.to_numeric(output[column], errors="coerce")
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="coerce")
    output = output.dropna(subset=required)
    output = output.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    output = output[output["high"] >= output[["open", "close"]].max(axis=1)]
    output = output[output["low"] <= output[["open", "close"]].min(axis=1)]
    output["timestamp"] = output["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return output.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch real domestic futures continuous data with AKShare.")
    parser.add_argument("--symbol", default="RB0", help="AKShare continuous symbol, e.g. RB0")
    parser.add_argument("--start", default="20180101", help="Start date YYYYMMDD")
    parser.add_argument("--end", default="20261231", help="End date YYYYMMDD")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()

    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("AKShare is not installed. Run: pip install -r requirements.txt") from exc

    source = "Sina main-continuous"
    try:
        frame = ak.futures_main_sina(
            symbol=args.symbol.upper(),
            start_date=args.start,
            end_date=args.end,
        )
    except Exception as primary_error:
        source = "Sina daily-continuous"
        try:
            frame = ak.futures_zh_daily_sina(symbol=args.symbol.upper())
            if "date" in frame.columns:
                dates = pd.to_datetime(frame["date"], errors="coerce")
                start = pd.Timestamp(args.start)
                end = pd.Timestamp(args.end)
                frame = frame[(dates >= start) & (dates <= end)]
        except Exception as fallback_error:
            raise RuntimeError(
                f"All futures sources failed; primary={primary_error}; fallback={fallback_error}",
            ) from fallback_error
    output = Path(args.output) if args.output else Path("data/futures") / f"{args.symbol.upper()}_daily.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize(frame)
    normalized.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"Saved {len(normalized)} real futures bars from {source}: {output.resolve()}")
    print("Note: RB0 is a daily main-continuous series; daily data does not preserve night-session bars.")


if __name__ == "__main__":
    main()
