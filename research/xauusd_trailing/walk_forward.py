from __future__ import annotations

from dataclasses import replace

import pandas as pd

from .engine import run_backtest
from .models import BacktestConfig


DEFAULT_SPLITS = (
    ("development", "2020-01-01", "2023-01-01"),
    ("validation", "2023-01-01", "2024-01-01"),
    ("out_of_sample", "2024-01-01", "2025-01-01"),
)


def run_walk_forward(
    m1: pd.DataFrame,
    m5: pd.DataFrame,
    h1: pd.DataFrame,
    config: BacktestConfig,
    splits: tuple[tuple[str, str, str], ...] = DEFAULT_SPLITS,
) -> dict[str, dict[str, object]]:
    """Evaluate frozen settings on chronological, non-overlapping periods."""
    reports: dict[str, dict[str, object]] = {}
    for name, start, end in splits:
        split_config = replace(config, start=start, end=end)
        result = run_backtest(m1, m5, h1, split_config)
        reports[name] = {
            "start_inclusive": start,
            "end_exclusive": end,
            "metrics": result.metrics,
            "audit": result.audit,
            "trades": result.trades,
            "events": result.events,
            "equity": result.equity,
            "open_positions": result.open_positions,
        }
    return reports
