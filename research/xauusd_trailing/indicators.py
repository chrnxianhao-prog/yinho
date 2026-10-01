from __future__ import annotations

import numpy as np
import pandas as pd


def ema_sma_seed(values: pd.Series, period: int) -> pd.Series:
    """Recursive EMA seeded with the first complete simple moving average."""
    array = values.to_numpy(dtype=float)
    result = np.full(len(array), np.nan, dtype=float)
    if period < 1:
        raise ValueError("period must be positive")
    alpha = 2.0 / (period + 1.0)
    valid = np.flatnonzero(np.isfinite(array))
    if len(valid) < period:
        return pd.Series(result, index=values.index, name=values.name)

    first = int(valid[0])
    seed_end = first + period
    if seed_end > len(array) or not np.isfinite(array[first:seed_end]).all():
        # Start at the first contiguous complete window; reject internal holes thereafter.
        for candidate in range(first, len(array) - period + 1):
            window = array[candidate : candidate + period]
            if np.isfinite(window).all():
                first, seed_end = candidate, candidate + period
                break
        else:
            return pd.Series(result, index=values.index, name=values.name)

    result[seed_end - 1] = float(array[first:seed_end].mean())
    for index in range(seed_end, len(array)):
        if not np.isfinite(array[index]):
            continue
        previous = result[index - 1]
        if np.isfinite(previous):
            result[index] = alpha * array[index] + (1.0 - alpha) * previous
    return pd.Series(result, index=values.index, name=values.name)


def macd_frame(
    close: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> pd.DataFrame:
    fast_ema = ema_sma_seed(close, fast)
    slow_ema = ema_sma_seed(close, slow)
    line = fast_ema - slow_ema
    signal_line = ema_sma_seed(line, signal)
    result = pd.DataFrame(
        {"macd": line, "macd_signal": signal_line, "macd_hist": line - signal_line},
        index=close.index,
    )
    result["golden_cross"] = (line.shift(1) <= signal_line.shift(1)) & (line > signal_line)
    result["dead_cross"] = (line.shift(1) >= signal_line.shift(1)) & (line < signal_line)
    result[["golden_cross", "dead_cross"]] = result[["golden_cross", "dead_cross"]].fillna(False)
    return result


def h1_range_frame(h1: pd.DataFrame, bars: int = 5) -> pd.DataFrame:
    """Map rolling H1 values to bar close timestamps, never the forming candle."""
    ordered = h1.sort_values("timestamp_utc").reset_index(drop=True)
    close_times = ordered["timestamp_utc"] + pd.Timedelta(hours=1)
    out = pd.DataFrame({"bar_close_time_utc": close_times})
    out["range_high"] = ordered["high"].rolling(bars, min_periods=bars).max().to_numpy()
    out["range_low"] = ordered["low"].rolling(bars, min_periods=bars).min().to_numpy()
    out["avg_high"] = ordered["high"].rolling(bars, min_periods=bars).mean().to_numpy()
    out["avg_low"] = ordered["low"].rolling(bars, min_periods=bars).mean().to_numpy()
    out["midline"] = (out["avg_high"] + out["avg_low"]) / 2.0
    return out
