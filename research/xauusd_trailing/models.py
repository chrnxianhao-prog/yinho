from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
    # entry_lots is a fixed-size fallback used only when risk_per_trade_pct=None.
    entry_lots: float = 0.05
    max_total_lots: float | None = None
    risk_per_trade_pct: float | None = 0.005
    max_stop_distance_usd: float = 8.0
    volume_min_lots: float = 0.01
    volume_step_lots: float = 0.01
    max_entries_per_cycle: int = 10
    max_losing_stops_per_cycle: int = 10
    stop_rule_scope: str = "current_cycle"
    count_profitable_stops: bool = False
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
    session_close_buffer_minutes: int = 15
    no_new_entry_before_close_minutes: int = 30
    allow_hold_through_session_break: bool = False
    min_stop_distance_usd: float = 0.0
    max_daily_loss_pct: float = 0.03
    max_drawdown_pct: float = 0.10
    leverage: float = 100.0
    margin_call_level: float = 100.0
    stop_out_level: float = 50.0
    swap_long_usd_per_lot_per_night: float = 0.0
    swap_short_usd_per_lot_per_night: float = 0.0
    triple_swap_weekday: int = 2  # Monday=0; default Wednesday.
    require_latest_m1_cross: bool = True
    blackout_windows: tuple[str, ...] = ()
    regime_lookback_days: int = 20
    regime_trend_threshold_pct: float = 0.01

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
        if self.max_entries_per_cycle < 1 or self.max_losing_stops_per_cycle < 1:
            raise ValueError("entry and losing-stop limits must be positive integers")
        if self.stop_rule_scope not in {"current_cycle", "next_cycle", "both"}:
            raise ValueError("stop_rule_scope must be current_cycle, next_cycle, or both")
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
        if self.risk_per_trade_pct is not None and not 0 < self.risk_per_trade_pct <= 1:
            raise ValueError("risk_per_trade_pct must be null or in (0, 1]")
        if self.max_stop_distance_usd <= 0:
            raise ValueError("max_stop_distance_usd must be positive")
        if min(self.volume_min_lots, self.volume_step_lots) <= 0:
            raise ValueError("volume_min_lots and volume_step_lots must be positive")
        if min(self.session_close_buffer_minutes, self.no_new_entry_before_close_minutes) < 0:
            raise ValueError("session close buffers cannot be negative")
        if self.min_stop_distance_usd < 0:
            raise ValueError("min_stop_distance_usd cannot be negative")
        if not 0 < self.max_daily_loss_pct <= 1 or not 0 < self.max_drawdown_pct <= 1:
            raise ValueError("account risk limits must be in (0, 1]")
        if self.leverage <= 0 or self.margin_call_level < 0 or self.stop_out_level < 0:
            raise ValueError("leverage must be positive and margin levels cannot be negative")
        if self.margin_call_level < self.stop_out_level:
            raise ValueError("margin_call_level must be greater than or equal to stop_out_level")
        if self.triple_swap_weekday not in range(7):
            raise ValueError("triple_swap_weekday must be Monday=0 through Sunday=6")
        if self.regime_lookback_days < 1 or self.regime_trend_threshold_pct < 0:
            raise ValueError("regime lookback must be positive and threshold non-negative")
        for window in self.blackout_windows:
            if not isinstance(window, str) or "-" not in window:
                raise ValueError("blackout_windows entries must be UTC HH:MM-HH:MM strings")
            start, end = window.split("-", 1)
            try:
                datetime.strptime(start, "%H:%M")
                datetime.strptime(end, "%H:%M")
            except ValueError as exc:
                raise ValueError(f"invalid UTC blackout window: {window}") from exc
            if start == end:
                raise ValueError("blackout window start and end must differ")
        if self.contract_size_oz is not None and self.contract_size_oz <= 0:
            raise ValueError("contract_size_oz must be positive when supplied")
