from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from quant_demo.config import load_settings
from quant_demo.data import make_bar_type
from quant_demo.instruments import build_instrument
try:
    from .common import bars_from_csv, write_bars_to_catalog
except ImportError:  # Direct `python data_ingestion/build_catalog.py` execution.
    from common import bars_from_csv, write_bars_to_catalog


def _path(root: Path, value: str) -> Path:
    candidate = Path(value)
    return candidate if candidate.is_absolute() else root / candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert real CSV files into a Nautilus ParquetDataCatalog.")
    parser.add_argument("--config", default="configs/settings.yaml")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete and recreate the configured catalog directory before writing",
    )
    parser.add_argument(
        "--markets",
        nargs="+",
        choices=("stock_cn", "futures_cn", "fx_gold"),
        help="Only build selected markets; default builds every enabled market",
    )
    args = parser.parse_args()
    settings = load_settings(_path(ROOT, args.config))
    data_cfg = settings["data"]
    catalog_path = _path(ROOT, data_cfg["catalog_path"])

    if args.reset and catalog_path.exists():
        if ROOT not in catalog_path.resolve().parents:
            raise ValueError("Refusing to reset a catalog outside the project root")
        shutil.rmtree(catalog_path)
    catalog_path.mkdir(parents=True, exist_ok=True)

    source_dirs = {
        "stock_cn": _path(ROOT, data_cfg["ashare_data_path"]),
        "futures_cn": _path(ROOT, data_cfg["futures_data_path"]),
        "fx_gold": _path(ROOT, data_cfg["mt5_data_path"]),
    }
    total = 0
    for market_id, market_cfg in settings["markets"].items():
        if not market_cfg.get("enabled", False):
            continue
        if args.markets and market_id not in args.markets:
            continue
        timeframe = market_cfg["timeframe"]
        for instrument_cfg in market_cfg["instruments"]:
            instrument = build_instrument(market_id, market_cfg, instrument_cfg)
            bar_type = make_bar_type(instrument, timeframe)
            csv_path = _path(source_dirs[market_id], instrument_cfg["data_file"])
            bars = bars_from_csv(csv_path, instrument=instrument, bar_type=bar_type)
            write_bars_to_catalog(catalog_path, instrument=instrument, bars=bars)
            total += len(bars)
            first = bars[0].ts_init
            last = bars[-1].ts_init
            print(
                f"[{market_id}] {instrument.id}: {len(bars)} bars, "
                f"{first} -> {last}, catalog={catalog_path.resolve()}",
            )
    if total == 0:
        raise RuntimeError("No enabled real-data bars were written")
    print(f"Catalog build complete: {total} bars")


if __name__ == "__main__":
    main()
