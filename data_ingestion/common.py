from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import Price
from nautilus_trader.model import Quantity
from nautilus_trader.persistence import ParquetDataCatalog


def clean_ohlcv_frame(
    frame: pd.DataFrame,
    *,
    timestamp_col: str = "timestamp",
    volume_col: str = "volume",
) -> pd.DataFrame:
    """Normalize a vendor OHLCV frame and remove unusable rows."""
    required = [timestamp_col, "open", "high", "low", "close", volume_col]
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV frame is missing columns: {', '.join(missing)}")

    output = frame[required].copy()
    output = output.rename(columns={volume_col: "volume"})
    output["timestamp"] = pd.to_datetime(output[timestamp_col], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=["timestamp", "open", "high", "low", "close", "volume"])
    output = output[output["high"] >= output[["open", "close"]].max(axis=1)]
    output = output[output["low"] <= output[["open", "close"]].min(axis=1)]
    output = output[output["volume"] >= 0]
    output = output.sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return output.reset_index(drop=True)


def _price(value: Any, precision: int) -> Price:
    return Price.from_str(f"{float(value):.{precision}f}")


def bars_from_csv(
    csv_path: str | Path,
    *,
    instrument: Any,
    bar_type: BarType,
    timestamp_col: str = "timestamp",
    volume_col: str = "volume",
) -> list[Bar]:
    """Read a cleaned OHLCV CSV and construct Nautilus ``Bar`` objects."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"Real data CSV not found: {path}")
    frame = clean_ohlcv_frame(
        pd.read_csv(path),
        timestamp_col=timestamp_col,
        volume_col=volume_col,
    )
    bars: list[Bar] = []
    for row in frame.itertuples(index=False):
        timestamp_ns = int(row.timestamp.value)
        bars.append(
            Bar(
                bar_type=bar_type,
                open=_price(row.open, instrument.price_precision),
                high=_price(row.high, instrument.price_precision),
                low=_price(row.low, instrument.price_precision),
                close=_price(row.close, instrument.price_precision),
                volume=Quantity.from_int(int(round(float(row.volume)))),
                ts_event=timestamp_ns,
                ts_init=timestamp_ns,
            ),
        )
    if not bars:
        raise ValueError(f"No valid OHLCV rows found in {path}")
    return bars


def write_bars_to_catalog(
    catalog_path: str | Path,
    *,
    instrument: Any,
    bars: list[Bar],
) -> None:
    """Write one instrument and its bars into a Nautilus Parquet catalog."""
    catalog_dir = Path(catalog_path)
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog = ParquetDataCatalog(str(catalog_dir))
    catalog.write_instruments([instrument])
    catalog.write_bars(sorted(bars, key=lambda bar: bar.ts_init), skip_disjoint_check=True)
