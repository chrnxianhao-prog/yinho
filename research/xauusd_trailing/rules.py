from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from math import floor
from typing import MutableMapping, MutableSet


UTC = timezone.utc


@dataclass(frozen=True)
class RiskSize:
    lots: float | None
    reason: str | None
    stop_distance_usd: float
    risk_budget_usd: float | None


@dataclass(frozen=True)
class StopCountResult:
    losing_stops: int
    all_stops: int
    counted_toward_limit: int
    halted_cycles: tuple[str, ...]


def size_for_stop(
    *,
    equity: float,
    risk_per_trade_pct: float | None,
    fixed_fallback_lots: float,
    stop_distance_usd: float,
    contract_size_oz: float,
    volume_min: float,
    volume_step: float,
    max_stop_distance_usd: float,
) -> RiskSize:
    """Shared deterministic sizing rule used by backtest and Demo runner."""
    distance = abs(float(stop_distance_usd))
    if not all(value > 0 for value in (equity, contract_size_oz, volume_min, volume_step)):
        return RiskSize(None, "INVALID_RISK_INPUT", distance, None)
    if distance <= 0:
        return RiskSize(None, "INVALID_STOP_DISTANCE", distance, None)
    if distance > max_stop_distance_usd:
        return RiskSize(None, "MAX_STOP_DISTANCE_EXCEEDED", distance, None)

    budget: float | None
    if risk_per_trade_pct is None:
        raw_lots = fixed_fallback_lots
        budget = None
    else:
        budget = equity * risk_per_trade_pct
        raw_lots = budget / (distance * contract_size_oz)

    steps = floor(raw_lots / volume_step + 1e-12)
    lots = round(steps * volume_step, 8)
    if lots + 1e-12 < volume_min:
        return RiskSize(None, "BELOW_MINIMUM_VOLUME", distance, budget)
    return RiskSize(lots, None, distance, budget)


def cross_is_eligible(
    cross_at: datetime,
    decision_at: datetime,
    *,
    lookback_minutes: int,
    require_latest: bool = False,
) -> bool:
    """A closed-bar cross must be causal and within the configured window."""
    cross_utc = cross_at.astimezone(UTC)
    decision_utc = decision_at.astimezone(UTC)
    if cross_utc > decision_utc:
        return False
    if require_latest:
        return cross_utc == decision_utc
    return (decision_utc - cross_utc).total_seconds() <= lookback_minutes * 60


def is_utc_blackout(at: datetime, windows: tuple[str, ...] | list[str]) -> bool:
    """UTC wall-clock windows use start-inclusive/end-exclusive semantics."""
    utc_time = at.astimezone(UTC).time().replace(tzinfo=None)
    for window in windows:
        start_text, end_text = window.split("-", 1)
        start = time.fromisoformat(start_text)
        end = time.fromisoformat(end_text)
        if start < end and start <= utc_time < end:
            return True
        if start > end and (utc_time >= start or utc_time < end):
            return True
    return False


def tighten_stop(side: str, current: float, candidate: float) -> float:
    """Monotonic stop update shared across historical and Demo execution."""
    if side.upper() in {"LONG", "BUY"}:
        return max(current, candidate)
    if side.upper() in {"SHORT", "SELL"}:
        return min(current, candidate) if current > 0 else candidate
    raise ValueError("side must identify a long/buy or short/sell position")


def record_stop_exit(
    *,
    cycle_id: str,
    next_cycle_id: str,
    net_pnl: float,
    losing_stops_by_cycle: MutableMapping[str, int],
    all_stops_by_cycle: MutableMapping[str, int],
    halted_cycles: MutableSet[str],
    max_losing_stops_per_cycle: int,
    stop_rule_scope: str,
    count_profitable_stops: bool,
) -> StopCountResult:
    """Count every SL separately from net-losing SLs and apply configured scope."""
    all_count = all_stops_by_cycle.get(cycle_id, 0) + 1
    all_stops_by_cycle[cycle_id] = all_count
    is_losing = net_pnl < 0
    losing_count = losing_stops_by_cycle.get(cycle_id, 0)
    if is_losing:
        losing_count += 1
        losing_stops_by_cycle[cycle_id] = losing_count
    counted = all_count if count_profitable_stops else losing_count
    newly_halted: list[str] = []
    if counted >= max_losing_stops_per_cycle:
        targets: list[str] = []
        if stop_rule_scope in {"current_cycle", "both"}:
            targets.append(cycle_id)
        if stop_rule_scope in {"next_cycle", "both"}:
            targets.append(next_cycle_id)
        for target in targets:
            if target not in halted_cycles:
                halted_cycles.add(target)
                newly_halted.append(target)
    return StopCountResult(losing_count, all_count, counted, tuple(newly_halted))


def entry_block_reason(
    *,
    cycle_id: str,
    entries_this_cycle: int,
    max_entries_per_cycle: int,
    halted_cycles: MutableSet[str] | set[str],
    risk_halted: bool,
    entry_lockout: bool,
    in_blackout: bool,
    before_session_close: bool,
    aggregate_lots: float = 0.0,
    requested_lots: float = 0.0,
    max_total_lots: float | None = None,
) -> str | None:
    """Central entry gate; order and backtest use identical rejection semantics."""
    if entry_lockout:
        return "ENTRY_LOCKOUT"
    if risk_halted:
        return "RISK_HALT"
    if in_blackout:
        return "BLACKOUT_WINDOW"
    if before_session_close:
        return "SESSION_CLOSE_ENTRY_BUFFER"
    if cycle_id in halted_cycles:
        return "CYCLE_STOP_LIMIT"
    if entries_this_cycle >= max_entries_per_cycle:
        return "CYCLE_ENTRY_LIMIT"
    if max_total_lots is not None and aggregate_lots + requested_lots > max_total_lots + 1e-12:
        return "MAX_TOTAL_LOTS"
    return None


def risk_halt_reason(
    *,
    equity: float,
    equity_peak: float,
    daily_pnl: float,
    day_start_equity: float,
    max_daily_loss_pct: float,
    max_drawdown_pct: float,
) -> str | None:
    """Account-level close-only circuit breaker shared by both execution modes."""
    if day_start_equity > 0 and daily_pnl <= -(day_start_equity * max_daily_loss_pct):
        return "MAX_DAILY_LOSS"
    drawdown = max(0.0, (equity_peak - equity) / equity_peak) if equity_peak > 0 else 1.0
    if drawdown >= max_drawdown_pct:
        return "MAX_DRAWDOWN"
    return None


def margin_required(lots: float, price: float, contract_size_oz: float, leverage: float) -> float:
    return abs(lots) * contract_size_oz * price / leverage


def margin_level_pct(equity: float, margin: float) -> float:
    if margin <= 0:
        return float("inf")
    return equity / margin * 100.0
