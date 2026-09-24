from __future__ import annotations

from typing import Any

from nautilus_trader.model import AggregationSource
from nautilus_trader.model import BarAggregation
from nautilus_trader.model import BarSpecification
from nautilus_trader.model import BarType
from nautilus_trader.model import PriceType


_AGGREGATIONS = {
    "SECOND": BarAggregation.SECOND,
    "MINUTE": BarAggregation.MINUTE,
    "HOUR": BarAggregation.HOUR,
    "DAY": BarAggregation.DAY,
    "WEEK": BarAggregation.WEEK,
    "MONTH": BarAggregation.MONTH,
}


def make_bar_type(instrument: Any, timeframe: str) -> BarType:
    """Build an external LAST bar type such as ``1-MINUTE`` or ``1-DAY``."""
    try:
        count_text, aggregation_text = timeframe.upper().split("-", 1)
        count = int(count_text)
        aggregation = _AGGREGATIONS[aggregation_text]
    except (ValueError, KeyError) as exc:
        raise ValueError(f"Unsupported timeframe: {timeframe!r}") from exc
    return BarType(
        instrument.id,
        BarSpecification(count, aggregation, PriceType.LAST),
        AggregationSource.EXTERNAL,
    )
