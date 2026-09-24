from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from quant_demo.indicator_plugins import IndicatorRegistry


class SignalLevel(StrEnum):
    WATCH = "WATCH"
    ENTRY = "ENTRY"
    STRONG_ENTRY = "STRONG_ENTRY"
    EXIT = "EXIT"
    STRONG_EXIT = "STRONG_EXIT"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class SignalPoint:
    timestamp: str
    price: float
    level: SignalLevel
    entry_score: float
    exit_score: float
    reasons: list[str]
    indicators: dict[str, float | None]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["level"] = self.level.value
        return payload


DEFAULT_RULE: dict[str, Any] = {
    "fast_ema": 10,
    "slow_ema": 30,
    "macd_fast": 12,
    "macd_slow": 26,
    "macd_signal": 9,
    "rsi_period": 14,
    "atr_period": 14,
    "volume_period": 20,
    "volume_ratio_threshold": 1.2,
    "entry_threshold": 60,
    "strong_entry_threshold": 80,
    "exit_threshold": 60,
    "strong_exit_threshold": 80,
    "weights": {
        "ema_trend": 30,
        "macd_momentum": 20,
        "rsi_zone": 15,
        "volume_confirmation": 20,
        "price_confirmation": 15,
    },
}


def merge_rule(defaults: dict[str, Any], override: dict[str, Any] | None = None) -> dict[str, Any]:
    merged = {**DEFAULT_RULE, **defaults}
    merged["weights"] = {**DEFAULT_RULE["weights"], **defaults.get("weights", {})}
    if override:
        merged.update({key: value for key, value in override.items() if key != "weights"})
        merged["weights"].update(override.get("weights", {}))
    return merged


class _EwmState:
    """Pandas-compatible adjust=False EWM with a min_periods warmup."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1.0)
        self.count = 0
        self.value: float | None = None

    def update(self, value: float | None) -> float | None:
        if value is None or not np.isfinite(value):
            return self.value if self.count >= self.period else None
        self.count += 1
        if self.value is None:
            self.value = value
        else:
            self.value = self.alpha * value + (1.0 - self.alpha) * self.value
        return self.value if self.count >= self.period else None


class IndicatorStream:
    """Incremental implementation shared by batch analysis and Nautilus bars."""

    def __init__(self, rule: dict[str, Any]) -> None:
        self.rule = rule
        self.ema_fast = _EwmState(int(rule["fast_ema"]))
        self.ema_slow = _EwmState(int(rule["slow_ema"]))
        self.macd_fast = _EwmState(int(rule["macd_fast"]))
        self.macd_slow = _EwmState(int(rule["macd_slow"]))
        self.macd_signal = _EwmState(int(rule["macd_signal"]))
        self.rsi_gain = _EwmState(int(rule["rsi_period"]))
        self.rsi_loss = _EwmState(int(rule["rsi_period"]))
        self.atr = _EwmState(int(rule["atr_period"]))
        self.volume_period = int(rule["volume_period"])
        self.volumes: deque[float] = deque(maxlen=self.volume_period)
        self.previous_close: float | None = None

    def update(self, row: dict[str, Any]) -> dict[str, Any]:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        volume = float(row["volume"])

        ema_fast = self.ema_fast.update(close)
        ema_slow = self.ema_slow.update(close)
        macd_fast = self.macd_fast.update(close)
        macd_slow = self.macd_slow.update(close)
        macd = None if macd_fast is None or macd_slow is None else macd_fast - macd_slow
        macd_signal = self.macd_signal.update(macd)
        macd_hist = None if macd is None or macd_signal is None else macd - macd_signal

        if self.previous_close is None:
            delta = None
            true_range = high - low
        else:
            delta = close - self.previous_close
            true_range = max(high - low, abs(high - self.previous_close), abs(low - self.previous_close))
        avg_gain = self.rsi_gain.update(None if delta is None else max(delta, 0.0))
        avg_loss = self.rsi_loss.update(None if delta is None else max(-delta, 0.0))
        if avg_gain is None or avg_loss is None:
            rsi = None
        elif avg_loss == 0.0:
            rsi = 100.0
        else:
            relative_strength = avg_gain / avg_loss
            rsi = 100.0 - (100.0 / (1.0 + relative_strength))

        atr = self.atr.update(true_range)
        atr_percent = None if atr is None or close == 0.0 else atr / close * 100.0

        self.volumes.append(volume)
        volume_ratio = None
        if len(self.volumes) >= self.volume_period:
            average_volume = sum(self.volumes) / self.volume_period
            if average_volume != 0.0:
                volume_ratio = volume / average_volume

        self.previous_close = close
        return {
            **row,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "macd": macd,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "rsi": rsi,
            "atr": atr,
            "atr_percent": atr_percent,
            "volume_ratio": volume_ratio,
        }


def compute_indicator_frame(frame: pd.DataFrame, rule: dict[str, Any]) -> pd.DataFrame:
    required = {"timestamp", "open", "high", "low", "close", "volume"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"OHLCV frame is missing: {', '.join(missing)}")

    output = frame.copy()
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        output[column] = pd.to_numeric(output[column], errors="coerce")
    output = output.dropna(subset=list(required)).sort_values("timestamp").drop_duplicates("timestamp")

    stream = IndicatorStream(rule)
    calculated = [stream.update(row.to_dict()) for _, row in output.iterrows()]
    for column in (
        "ema_fast", "ema_slow", "macd", "macd_signal", "macd_hist", "rsi", "atr", "atr_percent", "volume_ratio",
    ):
        output[column] = [item[column] for item in calculated]
    return output.reset_index(drop=True)


def _finite(value: Any) -> float | None:
    try:
        converted = float(value)
    except (TypeError, ValueError):
        return None
    return converted if np.isfinite(converted) else None


def evaluate_signal_row(row: pd.Series, rule: dict[str, Any]) -> SignalPoint:
    weights = rule["weights"]
    ready = all(
        _finite(row.get(column)) is not None
        for column in ("ema_fast", "ema_slow", "macd_hist", "rsi", "volume_ratio")
    )
    reasons: list[str] = []
    entry_score = 0.0
    exit_score = 0.0

    if ready:
        if row["ema_fast"] > row["ema_slow"]:
            entry_score += float(weights["ema_trend"])
            reasons.append("快速EMA位于慢速EMA上方")
        else:
            exit_score += float(weights["ema_trend"])
            reasons.append("快速EMA位于慢速EMA下方")

        if row["macd_hist"] > 0:
            entry_score += float(weights["macd_momentum"])
            reasons.append("MACD动能为正")
        else:
            exit_score += float(weights["macd_momentum"])
            reasons.append("MACD动能为负")

        rsi = float(row["rsi"])
        if 50 <= rsi <= 72:
            entry_score += float(weights["rsi_zone"])
            reasons.append("RSI位于多头有效区")
        elif rsi < 45 or rsi >= 78:
            exit_score += float(weights["rsi_zone"])
            reasons.append("RSI进入退出警戒区")

        if row["volume_ratio"] >= float(rule["volume_ratio_threshold"]):
            if row["close"] >= row["open"]:
                entry_score += float(weights["volume_confirmation"])
                reasons.append("放量上涨确认")
            else:
                exit_score += float(weights["volume_confirmation"])
                reasons.append("放量下跌确认")

        if row["close"] >= row["ema_fast"]:
            entry_score += float(weights["price_confirmation"])
            reasons.append("价格位于快速EMA上方")
        else:
            exit_score += float(weights["price_confirmation"])
            reasons.append("价格跌破快速EMA")

        for condition in rule.get("custom_conditions", []):
            column = str(condition["column"])
            value = _finite(row.get(column))
            if value is None:
                continue
            operator = str(condition.get("operator", "greater_than"))
            if operator == "between":
                lower, upper = condition["value"]
                matched = float(lower) <= value <= float(upper)
            else:
                threshold = float(condition["value"])
                matched = value > threshold if operator == "greater_than" else value < threshold
            if matched:
                direction = str(condition.get("direction", "ENTRY")).upper()
                weight = float(condition.get("weight", 0))
                if direction == "ENTRY":
                    entry_score += weight
                else:
                    exit_score += weight
                reasons.append(str(condition.get("reason", f"自定义指标 {column} 命中")))

    if not ready:
        level = SignalLevel.WATCH
        reasons = ["指标预热中"]
    elif entry_score >= float(rule["strong_entry_threshold"]) and entry_score > exit_score:
        level = SignalLevel.STRONG_ENTRY
    elif exit_score >= float(rule["strong_exit_threshold"]) and exit_score > entry_score:
        level = SignalLevel.STRONG_EXIT
    elif entry_score >= float(rule["entry_threshold"]) and entry_score > exit_score:
        level = SignalLevel.ENTRY
    elif exit_score >= float(rule["exit_threshold"]) and exit_score > entry_score:
        level = SignalLevel.EXIT
    else:
        level = SignalLevel.WATCH

    timestamp = pd.Timestamp(row["timestamp"]).isoformat().replace("+00:00", "Z")
    indicators = {
        "ema_fast": _finite(row.get("ema_fast")),
        "ema_slow": _finite(row.get("ema_slow")),
        "macd": _finite(row.get("macd")),
        "macd_signal": _finite(row.get("macd_signal")),
        "macd_hist": _finite(row.get("macd_hist")),
        "rsi": _finite(row.get("rsi")),
        "atr": _finite(row.get("atr")),
        "atr_percent": _finite(row.get("atr_percent")),
        "volume_ratio": _finite(row.get("volume_ratio")),
    }
    return SignalPoint(
        timestamp=timestamp,
        price=float(row["close"]),
        level=level,
        entry_score=entry_score,
        exit_score=exit_score,
        reasons=reasons,
        indicators=indicators,
    )


def analyze_frame(
    frame: pd.DataFrame,
    rule: dict[str, Any],
    registry: "IndicatorRegistry | None" = None,
) -> tuple[pd.DataFrame, list[SignalPoint]]:
    enriched = compute_indicator_frame(frame, rule)
    if registry is not None and rule.get("plugins"):
        enriched = registry.apply(enriched, list(rule["plugins"]))
    points = [evaluate_signal_row(row, rule) for _, row in enriched.iterrows()]
    enriched["signal_level"] = [point.level.value for point in points]
    enriched["entry_score"] = [point.entry_score for point in points]
    enriched["exit_score"] = [point.exit_score for point in points]
    return enriched, points


def signal_transitions(points: list[SignalPoint]) -> list[SignalPoint]:
    transitions: list[SignalPoint] = []
    previous: SignalLevel | None = None
    for point in points:
        if point.level != previous and point.level != SignalLevel.WATCH:
            transitions.append(point)
        previous = point.level
    return transitions
