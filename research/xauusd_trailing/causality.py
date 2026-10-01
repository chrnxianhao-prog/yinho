from __future__ import annotations

from collections.abc import Callable

import pandas as pd
from pandas.testing import assert_frame_equal


def assert_prefix_causal(
    source: pd.DataFrame,
    feature_builder: Callable[[pd.DataFrame], pd.DataFrame],
    through_index: int,
    columns: list[str] | None = None,
) -> None:
    """Prove future rows cannot alter computed features through a chosen cutoff."""
    if through_index < 0 or through_index >= len(source) - 1:
        raise ValueError("through_index must leave at least one future row")
    changed_future = source.copy(deep=True)
    future_index = changed_future.index[through_index + 1 :]
    numeric_columns = [
        name for name in ("open", "high", "low", "close") if name in changed_future.columns
    ]
    if not numeric_columns:
        raise ValueError("source must include at least one OHLC price column")
    changed_future.loc[future_index, numeric_columns] = (
        changed_future.loc[future_index, numeric_columns].astype(float) * 1.7 + 137.0
    )
    original_features = feature_builder(source)
    changed_features = feature_builder(changed_future)
    selected = columns or list(original_features.columns)
    assert_frame_equal(
        original_features.loc[:, selected].iloc[: through_index + 1].reset_index(drop=True),
        changed_features.loc[:, selected].iloc[: through_index + 1].reset_index(drop=True),
        check_dtype=False,
        check_exact=False,
        rtol=0,
        atol=1e-12,
    )
