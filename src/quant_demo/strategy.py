from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd

from nautilus_trader.config import StrategyConfig
from nautilus_trader.model import Bar
from nautilus_trader.model import BarType
from nautilus_trader.model import InstrumentId
from nautilus_trader.model import OrderSide
from nautilus_trader.model import TimeInForce
from nautilus_trader.trading import Strategy

from quant_demo.indicator_engine import IndicatorStream, SignalLevel, evaluate_signal_row


class IndicatorSignalConfig(StrategyConfig):
    def __init__(
        self,
        *,
        market_id: str,
        instrument_id: InstrumentId,
        bar_type: BarType,
        trade_size: Decimal,
        rule: dict[str, Any],
        allow_short: bool = False,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        self.market_id = market_id
        self.instrument_id = instrument_id
        self.bar_type = bar_type
        self.trade_size = trade_size
        self.rule = rule
        self.allow_short = allow_short


class IndicatorSignalStrategy(Strategy):
    """Nautilus adapter for the same indicator and scoring engine as paper replay."""

    def __init__(self, config: IndicatorSignalConfig) -> None:
        super().__init__(config)
        self.indicators = IndicatorStream(config.rule)
        self._last_level: SignalLevel | None = None
        self._target_position = Decimal("0")

    def on_start(self) -> None:
        if self.config.rule.get("plugins"):
            raise ValueError(
                "Nautilus backtest cannot currently execute external indicator plugins. "
                "Remove plugins from this rule or implement a streaming plugin adapter first.",
            )
        self.subscribe_bars(self.config.bar_type)
        rule = self.config.rule
        self.log.info(
            f"[{self.config.market_id}] shared scoring strategy started: {self.config.instrument_id}, "
            f"EMA({rule['fast_ema']}/{rule['slow_ema']}), MACD({rule['macd_fast']}/"
            f"{rule['macd_slow']}/{rule['macd_signal']}), RSI({rule['rsi_period']}), "
            f"ATR({rule['atr_period']}), size={self.config.trade_size}, "
            f"allow_short={self.config.allow_short}",
        )

    def on_bar(self, bar: Bar) -> None:
        timestamp = pd.Timestamp(bar.ts_event, unit="ns", tz="UTC").isoformat().replace("+00:00", "Z")
        values = self.indicators.update(
            {
                "timestamp": timestamp,
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            },
        )
        point = evaluate_signal_row(pd.Series(values), self.config.rule)
        changed = point.level != self._last_level
        self._last_level = point.level
        if not changed or point.level == SignalLevel.WATCH:
            return

        print(
            f"[signal][{self.config.market_id}] instrument={self.config.instrument_id} "
            f"level={point.level.value} entry={point.entry_score:.0f} exit={point.exit_score:.0f} "
            f"close={point.price} reasons={'；'.join(point.reasons)}",
            flush=True,
        )
        self.log.info(
            f"[{self.config.market_id}] level={point.level.value} instrument={self.config.instrument_id} "
            f"entry={point.entry_score:.0f} exit={point.exit_score:.0f} reasons={'；'.join(point.reasons)}",
        )

        # Keep the paper replay rule: only transitions into strong levels can auto-trade.
        if point.level == SignalLevel.STRONG_ENTRY:
            target = self.config.trade_size
        elif point.level == SignalLevel.STRONG_EXIT:
            target = -self.config.trade_size if self.config.allow_short else Decimal("0")
        else:
            return

        delta = target - self._target_position
        if delta == 0:
            return
        self._submit_market(OrderSide.BUY if delta > 0 else OrderSide.SELL, abs(delta))
        self._target_position = target

    def _submit_market(self, side: OrderSide, quantity: Decimal) -> None:
        instrument: Any = self.cache.instrument(self.config.instrument_id)
        order = self.order_factory.market(
            instrument_id=self.config.instrument_id,
            order_side=side,
            quantity=instrument.make_qty(quantity),
            time_in_force=TimeInForce.DAY,
        )
        self.submit_order(order)
        print(
            f"[order][{self.config.market_id}] side={side} instrument={self.config.instrument_id} "
            f"quantity={quantity}",
            flush=True,
        )
        self.log.info(
            f"[{self.config.market_id}] order_submitted side={side} "
            f"instrument={self.config.instrument_id} quantity={quantity}",
        )

    def on_stop(self) -> None:
        self.close_all_positions(self.config.instrument_id)
        self.log.info(f"[{self.config.market_id}] strategy stopped")
