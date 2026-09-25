from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time


@dataclass(frozen=True)
class BacktestConfig:
    timezone: str = "America/Mexico_City"
    source_timezone: str = "UTC"
    ohlc_price_side: str = "BID"
    start: str = "2020-01-01"
    end: str = "2025-01-01"  # exclusive
    symbol: str = "XAUUSD"
    initial_equity: float = 2000.0
    account_currency: str = "USD"
    contract_size_oz: float | None = None
    entry_lots: float = 0.05
    max_total_lots: float | None = None
    max_entries_per_cycle: int = 10
    pause_after_stops: int = 10
    cycle_hours: int = 5
    cycle_anchor_local: str = "2020-01-01T09:00:00"
    check_minutes: int = 5
    range_bars: int = 5
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    spread_usd: float = 0.20
    slippage_usd_per_side: float = 0.10
    commission_usd_per_lot_side: float = 0.0
    weekend_close_local: str = "21:55"
    weekend_max_quote_age_minutes: float = 5.0

    def __post_init__(self) -> None:
        if self.initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        if self.account_currency.upper() != "USD":
            raise ValueError("this version reports USD only; other account currencies need an FX conversion series")
        if self.ohlc_price_side.upper() != "BID":
            raise ValueError("this execution model requires Bid OHLC; Ask bars are not supported")
        if self.entry_lots <= 0:
            raise ValueError("entry_lots must be positive")
        if self.max_total_lots is not None and self.max_total_lots < self.entry_lots:
            raise ValueError("max_total_lots must be null or at least one positive entry_lots")
        if self.max_entries_per_cycle < 1 or self.pause_after_stops < 1:
            raise ValueError("entry and stop limits must be positive integers")
        # The implementation deliberately uses the confirmed five-hour local windows.
        if self.cycle_hours != 5:
            raise ValueError("only the confirmed five-hour local cycle is supported")
        try:
            anchor = datetime.fromisoformat(self.cycle_anchor_local)
        except ValueError as exc:
            raise ValueError("cycle_anchor_local must be an ISO local datetime") from exc
        if anchor.tzinfo is not None:
            raise ValueError("cycle_anchor_local must be a timezone-naive local datetime")
        if self.check_minutes < 1 or 60 % self.check_minutes:
            raise ValueError("check_minutes must evenly divide one hour")
        if min(self.range_bars, self.macd_fast, self.macd_slow, self.macd_signal) < 1:
            raise ValueError("indicator periods must be positive")
        if self.macd_fast >= self.macd_slow:
            raise ValueError("macd_fast must be smaller than macd_slow")
        if min(self.spread_usd, self.slippage_usd_per_side, self.commission_usd_per_lot_side) < 0:
            raise ValueError("execution costs cannot be negative")
        if self.weekend_max_quote_age_minutes < 0:
            raise ValueError("weekend_max_quote_age_minutes cannot be negative")
        if self.contract_size_oz is not None and self.contract_size_oz <= 0:
            raise ValueError("contract_size_oz must be positive when supplied")
        try:
            time.fromisoformat(self.weekend_close_local)
        except ValueError as exc:
            raise ValueError("weekend_close_local must be HH:MM") from exc
