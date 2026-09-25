from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from quant_demo.adapters.mt5_demo_adapter import Mt5DemoAdapter  # noqa: E402
from research.xauusd_trailing.data import validate_frames  # noqa: E402


UTC = timezone.utc
TIMEFRAMES = (("M1", "TIMEFRAME_M1", 1), ("M5", "TIMEFRAME_M5", 5), ("H1", "TIMEFRAME_H1", 60))


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def rates_frame(rates: Any, minutes: int) -> pd.DataFrame:
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread_points", "real_volume"])
    frame = pd.DataFrame(rates)
    frame["timestamp_utc"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    rename = {"spread": "spread_points"}
    frame = frame.rename(columns=rename)
    columns = [name for name in ("timestamp_utc", "open", "high", "low", "close", "tick_volume", "spread_points", "real_volume") if name in frame]
    return frame[columns].sort_values("timestamp_utc").drop_duplicates("timestamp_utc").reset_index(drop=True)


def export_history(
    mt5: Any,
    *,
    symbol: str,
    start: datetime,
    end: datetime,
    output_dir: Path,
    endpoint_tolerance_days: int = 5,
) -> dict[str, Any]:
    if start >= end:
        raise ValueError("end must be later than start")
    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"Could not select {symbol}: {mt5.last_error()}")
    coverage: dict[str, dict[str, Any]] = {}
    frames: dict[str, pd.DataFrame] = {}
    output_dir.mkdir(parents=True, exist_ok=True)
    for name, timeframe_name, minutes in TIMEFRAMES:
        timeframe = getattr(mt5, timeframe_name)
        rates = mt5.copy_rates_range(symbol, timeframe, start, end)
        frame = rates_frame(rates, minutes)
        if frame.empty:
            raise RuntimeError(f"MT5 returned no {name} history for {symbol}")
        first = frame["timestamp_utc"].iloc[0].to_pydatetime()
        last = (frame["timestamp_utc"].iloc[-1] + pd.Timedelta(minutes=minutes)).to_pydatetime()
        start_ok = first <= start + timedelta(days=endpoint_tolerance_days)
        end_ok = last >= end - timedelta(days=endpoint_tolerance_days)
        coverage[name] = {
            "rows": len(frame),
            "first_bar_utc": first.isoformat(),
            "last_bar_close_utc": last.isoformat(),
            "requested_start_utc": start.isoformat(),
            "requested_end_utc_exclusive": end.isoformat(),
            "start_covered_with_tolerance": start_ok,
            "end_covered_with_tolerance": end_ok,
        }
        if not start_ok or not end_ok:
            raise RuntimeError(f"{name} coverage does not reach the requested interval: {coverage[name]}")
        path = output_dir / f"{symbol}_{name}.csv"
        frame.to_csv(path, index=False)
        coverage[name]["file"] = str(path)
        frames[name] = frame
    audit = validate_frames(frames["M1"], frames["M5"], frames["H1"])
    if not audit.ok:
        raise RuntimeError(f"exported MT5 bars failed M1/M5/H1 integrity audit: {audit.as_dict()}")
    manifest = {
        "symbol": symbol,
        "source": "MetaTrader5 Demo terminal, copy_rates_range",
        "timestamps": "UTC bar-open time",
        "spread_column_preserved": "spread_points",
        "coverage_endpoint_tolerance_days": endpoint_tolerance_days,
        "timeframes": coverage,
        "data_audit": audit.as_dict(),
    }
    (output_dir / "coverage.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only MT5 Demo export of XAUUSD M1/M5/H1 history")
    parser.add_argument("--symbol", default="XAUUSD")
    parser.add_argument("--start", default="2020-01-01T00:00:00Z")
    parser.add_argument("--end", default="2025-01-01T00:00:00Z", help="Exclusive UTC endpoint")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data/mt5")
    parser.add_argument("--env-path", type=Path, default=ROOT / ".env")
    parser.add_argument("--confirm-demo-read", action="store_true", help="Required to connect to the Demo terminal for read-only export")
    args = parser.parse_args()
    if not args.confirm_demo_read:
        raise SystemExit("No MT5 connection attempted. Pass --confirm-demo-read for read-only Demo history export.")
    adapter = Mt5DemoAdapter(args.env_path.resolve())
    try:
        adapter.connect_demo(require_trading=False)
        report = export_history(
            adapter.mt5,
            symbol=args.symbol,
            start=_parse_utc(args.start),
            end=_parse_utc(args.end),
            output_dir=args.output_dir.resolve(),
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        adapter.close()


if __name__ == "__main__":
    main()
