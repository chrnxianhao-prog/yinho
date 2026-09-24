from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _normalize(frame: pd.DataFrame) -> pd.DataFrame:
    mapping = {
        "日期": "timestamp",
        "时间": "timestamp",
        "开盘": "open",
        "最高": "high",
        "最低": "low",
        "收盘": "close",
        "成交量": "volume",
        "成交额": "turnover",
        "date": "timestamp",
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    output = frame.rename(columns=mapping).copy()
    required = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [column for column in required if column not in output.columns]
    if missing:
        raise ValueError(f"AKShare stock response is missing columns: {', '.join(missing)}")
    for column in ("open", "high", "low", "close", "volume"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="coerce")
    output = output.dropna(subset=required)
    output = output.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    output = output[output["high"] >= output[["open", "close"]].max(axis=1)]
    output = output[output["low"] <= output[["open", "close"]].min(axis=1)]
    output["timestamp"] = output["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    return output.reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch real A-share OHLCV data with AKShare.")
    parser.add_argument("--symbol", default="600000", help="Six-digit A-share code, e.g. 600000")
    parser.add_argument("--start", default="20180101", help="Start date YYYYMMDD")
    parser.add_argument("--end", default="20261231", help="End date YYYYMMDD")
    parser.add_argument("--adjust", default="", choices=("", "qfq", "hfq"))
    parser.add_argument("--minute", action="store_true", help="Use recent 1-minute endpoint instead of daily history")
    parser.add_argument("--output", default=None, help="Output CSV path")
    args = parser.parse_args()

    try:
        import akshare as ak
    except ImportError as exc:
        raise SystemExit("AKShare is not installed. Run: pip install -r requirements.txt") from exc

    if args.minute:
        frame = ak.stock_zh_a_hist_min_em(
            symbol=args.symbol,
            start_date=f"{args.start[:4]}-{args.start[4:6]}-{args.start[6:8]} 09:30:00",
            end_date=f"{args.end[:4]}-{args.end[4:6]}-{args.end[6:8]} 15:00:00",
            period="1",
            adjust=args.adjust,
        )
        default_name = f"{args.symbol}_M1.csv"
    else:
        source = "Eastmoney"
        try:
            frame = ak.stock_zh_a_hist(
                symbol=args.symbol,
                period="daily",
                start_date=args.start,
                end_date=args.end,
                adjust=args.adjust,
            )
        except Exception as primary_error:
            prefix = "sh" if args.symbol.startswith(("5", "6", "9")) else "sz"
            source = "Sina"
            try:
                frame = ak.stock_zh_a_daily(
                    symbol=f"{prefix}{args.symbol}",
                    start_date=args.start,
                    end_date=args.end,
                    adjust=args.adjust,
                )
            except Exception:
                source = "Tencent"
                try:
                    frame = ak.stock_zh_a_hist_tx(
                        symbol=f"{prefix}{args.symbol}",
                        start_date=args.start,
                        end_date=args.end,
                        adjust=args.adjust,
                    )
                except Exception as fallback_error:
                    raise RuntimeError(
                        f"All A-share sources failed; Eastmoney={primary_error}; fallback={fallback_error}",
                    ) from fallback_error
        default_name = f"{args.symbol}_daily.csv"

    output = Path(args.output) if args.output else Path("data/ashare") / default_name
    output.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize(frame)
    normalized.to_csv(output, index=False, encoding="utf-8-sig")
    print(f"Saved {len(normalized)} real A-share bars from {source if not args.minute else 'Eastmoney'}: {output.resolve()}")
    if args.minute:
        print("Note: AKShare's public A-share minute endpoint is intended for a recent limited window.")


if __name__ == "__main__":
    main()
