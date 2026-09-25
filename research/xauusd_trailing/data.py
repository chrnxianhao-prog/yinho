from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


_COLUMN_ALIASES = {
    "timestamp_utc": ("timestamp_utc", "datetime", "timestamp", "time", "date"),
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "tick_volume": ("tick_volume", "volume", "tickvol"),
    "spread_points": ("spread_points", "spread"),
    "real_volume": ("real_volume",),
}


@dataclass
class DataAudit:
    rows: dict[str, int]
    duplicates: dict[str, int]
    invalid_ohlc: dict[str, int]
    aggregation_mismatches: dict[str, int]
    gaps_over_one_minute: int

    @property
    def ok(self) -> bool:
        return (
            not any(self.duplicates.values())
            and not any(self.invalid_ohlc.values())
            and not any(self.aggregation_mismatches.values())
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": self.rows,
            "duplicates": self.duplicates,
            "invalid_ohlc": self.invalid_ohlc,
            "aggregation_mismatches": self.aggregation_mismatches,
            "gaps_over_one_minute": self.gaps_over_one_minute,
            "ok": self.ok,
        }


def _to_utc(series: pd.Series, source_timezone: str) -> pd.Series:
    first_value = series.dropna().iloc[0]
    if pd.Timestamp(first_value).tzinfo is not None:
        return pd.to_datetime(series, errors="raise", utc=True)
    parsed = pd.to_datetime(series, errors="raise")
    return parsed.dt.tz_localize(source_timezone, ambiguous="raise", nonexistent="raise").dt.tz_convert("UTC")


def timestamp_ns(series: pd.Series) -> np.ndarray:
    """Normalize pandas' variable datetime resolution to integer nanoseconds."""
    return series.dt.as_unit("ns").astype("int64").to_numpy()


def load_mt5_csv(path: str | Path, source_timezone: str = "UTC") -> pd.DataFrame:
    """Load a MT5 CSV; naive timestamps require an explicit source timezone."""
    csv_path = Path(path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        first_line = handle.readline()
    delimiter = max((",", ";", "\t"), key=first_line.count)
    frame = pd.read_csv(csv_path, sep=delimiter, encoding="utf-8-sig")
    original = {str(column).strip().strip("<>").lower(): column for column in frame.columns}
    if "timestamp_utc" not in original and "datetime" not in original and "timestamp" not in original:
        if "date" in original and "time" in original:
            date_col, time_col = original["date"], original["time"]
            frame["timestamp_utc"] = frame[date_col].astype(str).str.strip() + " " + frame[time_col].astype(str).str.strip()
            original["timestamp_utc"] = "timestamp_utc"
    rename: dict[Any, str] = {}
    for canonical, aliases in _COLUMN_ALIASES.items():
        match = next((original[name] for name in aliases if name in original), None)
        if match is not None:
            rename[match] = canonical
    frame = frame.rename(columns=rename)
    required = {"timestamp_utc", "open", "high", "low", "close"}
    missing = required - set(frame.columns)
    if missing:
        raise ValueError(f"{path}: missing required CSV columns: {', '.join(sorted(missing))}")
    frame["timestamp_utc"] = _to_utc(frame["timestamp_utc"], source_timezone)
    for field in ("open", "high", "low", "close", "tick_volume", "spread_points", "real_volume"):
        if field in frame:
            frame[field] = pd.to_numeric(frame[field], errors="raise")
    return frame.sort_values("timestamp_utc").reset_index(drop=True)


def validate_frames(m1: pd.DataFrame, m5: pd.DataFrame, h1: pd.DataFrame) -> DataAudit:
    frames = {"M1": m1, "M5": m5, "H1": h1}
    duplicates: dict[str, int] = {}
    invalid: dict[str, int] = {}
    rows: dict[str, int] = {}
    for name, frame in frames.items():
        rows[name] = len(frame)
        if frame.empty:
            raise ValueError(f"{name} dataset is empty")
        if frame["timestamp_utc"].dt.tz is None:
            raise ValueError(f"{name} timestamp_utc must be timezone-aware")
        if not frame["timestamp_utc"].is_monotonic_increasing:
            raise ValueError(f"{name} must be sorted by timestamp_utc")
        duplicates[name] = int(frame["timestamp_utc"].duplicated().sum())
        invalid[name] = int(
            (
                (frame["low"] > frame[["open", "close"]].min(axis=1))
                | (frame["high"] < frame[["open", "close"]].max(axis=1))
                | (frame["high"] < frame["low"])
                | (frame[["open", "high", "low", "close"]] <= 0).any(axis=1)
                | ~np.isfinite(frame[["open", "high", "low", "close"]]).all(axis=1)
            ).sum()
        )
    if any(duplicates.values()) or any(invalid.values()):
        raise ValueError(f"bar data failed integrity checks: duplicates={duplicates}, invalid_ohlc={invalid}")

    mismatches = {
        "M5": _aggregation_mismatches(m1, m5, "5min"),
        "H1": _aggregation_mismatches(m1, h1, "1h"),
    }
    m1_times = timestamp_ns(m1["timestamp_utc"])
    gaps = int(np.sum(np.diff(m1_times) > pd.Timedelta(minutes=1).value))
    return DataAudit(rows, duplicates, invalid, mismatches, gaps)


def _aggregation_mismatches(m1: pd.DataFrame, higher: pd.DataFrame, rule: str) -> int:
    base = m1.copy()
    base["bucket"] = base["timestamp_utc"].dt.floor(rule)
    base["bucket_count"] = base.groupby("bucket")["timestamp_utc"].transform("size")
    expected = base.groupby("bucket").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), count=("close", "size")
    )
    period_minutes = int(pd.Timedelta(rule).total_seconds() // 60)
    expected = expected[expected["count"] == period_minutes]
    actual = higher.copy().set_index(higher["timestamp_utc"].dt.floor(rule))
    actual = actual[~actual.index.duplicated(keep="last")]
    common = expected.index.intersection(actual.index)
    mismatches = 0
    for field in ("open", "high", "low", "close"):
        left = expected.loc[common, field].to_numpy(dtype=float)
        right = actual.loc[common, field].to_numpy(dtype=float)
        mismatches += int((~np.isclose(left, right, rtol=0, atol=1e-8)).sum())
    return mismatches
