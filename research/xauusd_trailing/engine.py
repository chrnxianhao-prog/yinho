from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .cycles import cycle_window, resolve_cycle_anchor
from .data import timestamp_ns, validate_frames
from .indicators import h1_range_frame, macd_frame
from .metrics import calculate_metrics
from .models import BacktestConfig
from .rules import (
    cross_is_eligible,
    entry_block_reason,
    is_utc_blackout,
    margin_level_pct,
    margin_required,
    record_stop_exit,
    risk_halt_reason,
    size_for_stop,
    tighten_stop,
)
from .sessions import infer_session_breaks, within_entry_buffer


UTC = timezone.utc


@dataclass
class Position:
    position_id: str
    side: str
    lots: float
    entry_time_utc: pd.Timestamp
    entry_cycle_id: str
    entry_bid: float
    entry_fill: float
    entry_mid: float
    initial_stop: float
    active_stop: float
    initial_risk_usd: float
    m1_cross_id: str
    m5_cross_id: str
    entry_regime: str
    stop_updates: int = 0
    swap: float = 0.0
    mae_usd: float = 0.0
    mfe_usd: float = 0.0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    events: pd.DataFrame
    equity: pd.DataFrame
    open_positions: pd.DataFrame
    metrics: dict[str, object]
    audit: dict[str, object]
    reports: dict[str, pd.DataFrame]


def _utc(value: str) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _cycle_for(
    local: datetime,
    cycle_hours: int,
    cycle_anchor_local: datetime | str = "2020-01-01T09:00:00",
) -> tuple[str, datetime, datetime]:
    timezone_name = getattr(local.tzinfo, "key", None) or str(local.tzinfo)
    return cycle_window(local, timezone_name, cycle_hours, cycle_anchor_local)


def _next_cycle_id(
    local: datetime,
    cycle_hours: int,
    cycle_anchor_local: datetime | str = "2020-01-01T09:00:00",
) -> str:
    _, _, end = _cycle_for(local, cycle_hours, cycle_anchor_local)
    return f"{end.date().isoformat()}T{end.hour:02d}:{end.minute:02d}"


def _cross_events(frame: pd.DataFrame, timeframe: str, config: BacktestConfig) -> list[tuple[int, str, str]]:
    indicators = macd_frame(frame["close"], config.macd_fast, config.macd_slow, config.macd_signal)
    close_ns = timestamp_ns(frame["timestamp_utc"] + pd.Timedelta(timeframe))
    result: list[tuple[int, str, str]] = []
    for index, row in enumerate(indicators.itertuples(index=False)):
        if row.golden_cross:
            direction = "LONG"
        elif row.dead_cross:
            direction = "SHORT"
        else:
            continue
        stamp = pd.Timestamp(close_ns[index], tz="UTC")
        result.append((int(close_ns[index]), direction, f"{timeframe}:{stamp.isoformat()}"))
    return result


class CrossBook:
    def __init__(self, events: list[tuple[int, str, str]]) -> None:
        self.events = events
        self.times = np.fromiter((item[0] for item in events), dtype=np.int64, count=len(events))

    def latest_unused(
        self,
        start_ns: int,
        end_ns: int,
        direction: str,
        consumed: set[str],
        *,
        require_latest: bool = False,
    ) -> tuple[int, str, str] | None:
        left = int(np.searchsorted(self.times, start_ns, side="left"))
        right = int(np.searchsorted(self.times, end_ns, side="right"))
        decision = datetime.fromtimestamp(end_ns / 1_000_000_000, UTC)
        for item in reversed(self.events[left:right]):
            if item[1] != direction or item[2] in consumed:
                continue
            event_at = datetime.fromtimestamp(item[0] / 1_000_000_000, UTC)
            if cross_is_eligible(
                event_at,
                decision,
                lookback_minutes=5,
                require_latest=require_latest,
            ):
                return item
        return None


def _market_regime(
    at_ns: int,
    h1: pd.DataFrame,
    h1_close_ns: np.ndarray,
    config: BacktestConfig,
) -> str:
    current = int(np.searchsorted(h1_close_ns, at_ns, side="right") - 1)
    bars_back = config.regime_lookback_days * 24
    if current < bars_back or "close" not in h1:
        return "UNKNOWN"
    start = float(h1["close"].iloc[current - bars_back])
    end = float(h1["close"].iloc[current])
    if start <= 0:
        return "UNKNOWN"
    change = end / start - 1.0
    if change >= config.regime_trend_threshold_pct:
        return "UP"
    if change <= -config.regime_trend_threshold_pct:
        return "DOWN"
    return "RANGE"


def _group_report(trades: pd.DataFrame, key: str) -> pd.DataFrame:
    if trades.empty or key not in trades:
        return pd.DataFrame(columns=[key, "trades", "net_pnl", "win_rate", "mean_r_multiple"])
    rows: list[dict[str, object]] = []
    for label, group in trades.groupby(key, dropna=False, sort=True):
        pnl = pd.to_numeric(group["net_pnl"], errors="coerce").dropna()
        rows.append({
            key: label,
            "trades": int(len(group)),
            "net_pnl": float(pnl.sum()) if len(pnl) else None,
            "win_rate": float((pnl > 0).mean()) if len(pnl) else None,
            "mean_r_multiple": float(group["r_multiple"].dropna().mean()) if group["r_multiple"].notna().any() else None,
        })
    return pd.DataFrame(rows)


def run_backtest(m1: pd.DataFrame, m5: pd.DataFrame, h1: pd.DataFrame, config: BacktestConfig) -> BacktestResult:
    """Run a deterministic, M1 event simulation; this module never connects to a broker."""
    if config.contract_size_oz is None:
        raise ValueError("contract_size_oz is required for backtests; set it from the MT5 symbol specification")
    audit_obj = validate_frames(m1, m5, h1)
    if not audit_obj.ok:
        raise ValueError(f"input data failed integrity audit: {audit_obj.as_dict()}")
    tz = ZoneInfo(config.timezone)
    cycle_anchor = resolve_cycle_anchor(config.cycle_anchor_local, config.timezone)
    start_utc, end_utc = _utc(config.start), _utc(config.end)
    if start_utc >= end_utc:
        raise ValueError("backtest end must be later than start")

    m1 = m1.sort_values("timestamp_utc").reset_index(drop=True)
    m5 = m5.sort_values("timestamp_utc").reset_index(drop=True)
    h1 = h1.sort_values("timestamp_utc").reset_index(drop=True)
    m1_ns = timestamp_ns(m1["timestamp_utc"])
    m5_close_ns = timestamp_ns(m5["timestamp_utc"] + pd.Timedelta(minutes=5))
    h1_ranges = h1_range_frame(h1, config.range_bars)
    h1_close_ns = timestamp_ns(h1_ranges["bar_close_time_utc"])
    m1_crosses = CrossBook(_cross_events(m1, "1min", config))
    m5_crosses = CrossBook(_cross_events(m5, "5min", config))
    session_breaks = infer_session_breaks(m1["timestamp_utc"], threshold_minutes=30)
    break_indices = sorted(session_breaks)

    positions: list[Position] = []
    trades: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    consumed_crosses: set[str] = set()
    entries_by_cycle: dict[str, int] = {}
    losing_stops_by_cycle: dict[str, int] = {}
    all_stops_by_cycle: dict[str, int] = {}
    halted_cycles: set[str] = set()
    daily_equity: dict[date, dict[str, object]] = {}
    realized_net = 0.0
    cumulative_swap = 0.0
    entry_counter = 0
    data_gap_count = 0
    data_gap_with_position_count = 0
    session_breaks_with_position_count = 0
    signal_checks = 0
    rejected_signals = 0
    skipped_risk_signals = 0
    margin_stop_out_count = 0
    margin_call_active = False
    equity_peak = config.initial_equity
    current_trading_day: date | None = None
    day_start_equity = config.initial_equity
    risk_halted_day: date | None = None

    def record_event(event_type: str, at: pd.Timestamp, **details: object) -> None:
        events.append({"event_type": event_type, "event_time_utc": at.isoformat(), **details})

    def mark_equity(bid: float) -> float:
        mark_mid = bid + config.spread_usd / 2.0
        open_net = 0.0
        for position in positions:
            size = position.lots * config.contract_size_oz
            direction = 1.0 if position.side == "LONG" else -1.0
            open_net += direction * size * (mark_mid - position.entry_mid)
            open_net -= size * (config.spread_usd + 2.0 * config.slippage_usd_per_side)
            open_net -= position.lots * config.commission_usd_per_lot_side * 2.0
            open_net += position.swap
        return config.initial_equity + realized_net + open_net

    def close_position(
        position: Position,
        close_time: pd.Timestamp,
        trigger_time: pd.Timestamp,
        exit_fill: float,
        exit_mid: float,
        reason: str,
        close_cycle_id: str,
        event_details: dict[str, object] | None = None,
    ) -> None:
        nonlocal realized_net
        size = position.lots * config.contract_size_oz
        direction = 1.0 if position.side == "LONG" else -1.0
        gross = direction * size * (exit_mid - position.entry_mid)
        spread_cost = size * config.spread_usd
        slippage_cost = size * config.slippage_usd_per_side * 2.0
        commission = position.lots * config.commission_usd_per_lot_side * 2.0
        net = gross - spread_cost - slippage_cost - commission + position.swap
        realized_net += net
        r_multiple = net / position.initial_risk_usd if position.initial_risk_usd > 0 else None
        trade_row = {
            "position_id": position.position_id,
            "side": position.side,
            "lots": position.lots,
            "entry_time_utc": position.entry_time_utc.isoformat(),
            "entry_cycle_id": position.entry_cycle_id,
            "entry_bid": position.entry_bid,
            "entry_fill": position.entry_fill,
            "entry_mid": position.entry_mid,
            "initial_stop": position.initial_stop,
            "initial_risk_usd": position.initial_risk_usd,
            "entry_regime": position.entry_regime,
            "exit_time_utc": close_time.isoformat(),
            "stop_trigger_time_utc": trigger_time.isoformat(),
            "exit_fill": exit_fill,
            "exit_mid": exit_mid,
            "active_stop": position.active_stop,
            "close_reason": reason,
            "exit_cycle_id": close_cycle_id,
            "hold_minutes": (close_time - position.entry_time_utc).total_seconds() / 60.0,
            "m1_cross_id": position.m1_cross_id,
            "m5_cross_id": position.m5_cross_id,
            "stop_updates": position.stop_updates,
            "gross_pnl": gross,
            "spread_cost": spread_cost,
            "slippage_cost": slippage_cost,
            "commission": commission,
            "swap": position.swap,
            "net_pnl": net,
            "r_multiple": r_multiple,
            "mae_usd": position.mae_usd,
            "mfe_usd": position.mfe_usd,
            "mae_r": position.mae_usd / position.initial_risk_usd if position.initial_risk_usd else None,
            "mfe_r": position.mfe_usd / position.initial_risk_usd if position.initial_risk_usd else None,
            "cost_doubled_net_pnl": gross - 2.0 * (spread_cost + slippage_cost + commission) + position.swap,
        }
        trades.append(trade_row)
        if reason == "STOP_LOSS":
            next_id = _next_cycle_id(close_time.tz_convert(tz).to_pydatetime(), config.cycle_hours, cycle_anchor)
            outcome = record_stop_exit(
                cycle_id=close_cycle_id,
                next_cycle_id=next_id,
                net_pnl=net,
                losing_stops_by_cycle=losing_stops_by_cycle,
                all_stops_by_cycle=all_stops_by_cycle,
                halted_cycles=halted_cycles,
                max_losing_stops_per_cycle=config.max_losing_stops_per_cycle,
                stop_rule_scope=config.stop_rule_scope,
                count_profitable_stops=config.count_profitable_stops,
            )
            record_event(
                "STOP_COUNTS", close_time,
                cycle_id=close_cycle_id,
                losing_stop_count=outcome.losing_stops,
                all_stop_count=outcome.all_stops,
                count_toward_limit=outcome.counted_toward_limit,
                net_pnl=net,
            )
            for paused_id in outcome.halted_cycles:
                record_event(
                    "CYCLE_PAUSED", close_time,
                    cycle_id=close_cycle_id,
                    paused_cycle_id=paused_id,
                    stop_rule_scope=config.stop_rule_scope,
                    losing_stop_count=outcome.losing_stops,
                    all_stop_count=outcome.all_stops,
                )
        record_event(
            reason,
            close_time,
            position_id=position.position_id,
            side=position.side,
            lots=position.lots,
            exit_fill=exit_fill,
            active_stop=position.active_stop,
            cycle_id=close_cycle_id,
            net_pnl=net,
            swap=position.swap,
            **(event_details or {}),
        )
        positions.remove(position)

    def apply_swap_for_crossed_days(previous_day: date | None, current_day: date, at: pd.Timestamp) -> None:
        nonlocal cumulative_swap
        if previous_day is None or current_day <= previous_day:
            return
        day = previous_day + timedelta(days=1)
        while day <= current_day:
            rollover_day = day
            multiple = 3 if rollover_day.weekday() == config.triple_swap_weekday else 1
            for position in list(positions):
                if position.entry_time_utc.date() >= rollover_day:
                    continue
                rate = (
                    config.swap_long_usd_per_lot_per_night
                    if position.side == "LONG" else config.swap_short_usd_per_lot_per_night
                )
                charge = rate * position.lots * multiple
                position.swap += charge
                cumulative_swap += charge
                record_event(
                    "SWAP_CHARGED", at,
                    position_id=position.position_id,
                    server_date=rollover_day.isoformat(),
                    weekday_multiplier=multiple,
                    swap=charge,
                    cumulative_position_swap=position.swap,
                )
            day = rollover_day + timedelta(days=1)

    def check_margin(at: pd.Timestamp, bid: float, cycle_id: str) -> None:
        nonlocal margin_call_active, margin_stop_out_count
        mark = bid + config.spread_usd / 2.0
        while positions:
            equity_value = mark_equity(bid)
            margin = sum(
                margin_required(p.lots, mark, config.contract_size_oz, config.leverage)
                for p in positions
            )
            level = margin_level_pct(equity_value, margin)
            if level <= config.margin_call_level and not margin_call_active:
                margin_call_active = True
                record_event("MARGIN_CALL", at, equity=equity_value, used_margin=margin, margin_level_pct=level)
            elif level > config.margin_call_level:
                margin_call_active = False
            if level > config.stop_out_level:
                return
            worst: Position | None = None
            worst_pnl: float | None = None
            for position in positions:
                sign = 1.0 if position.side == "LONG" else -1.0
                floating = sign * position.lots * config.contract_size_oz * (mark - position.entry_mid)
                if worst_pnl is None or floating < worst_pnl:
                    worst, worst_pnl = position, floating
            assert worst is not None
            if worst.side == "LONG":
                exit_fill = bid - config.slippage_usd_per_side
                exit_mid = bid + config.spread_usd / 2.0
            else:
                ask = bid + config.spread_usd
                exit_fill = ask + config.slippage_usd_per_side
                exit_mid = ask - config.spread_usd / 2.0
            margin_stop_out_count += 1
            close_position(worst, at, at, exit_fill, exit_mid, "MARGIN_STOP_OUT", cycle_id)

    def update_risk_halt(at: pd.Timestamp, bid: float) -> float:
        nonlocal current_trading_day, day_start_equity, risk_halted_day, equity_peak
        day = at.date()
        equity_value = mark_equity(bid)
        if current_trading_day != day:
            if risk_halted_day is not None and risk_halted_day < day:
                record_event("RISK_HALT_RESET", at, previous_halt_day=risk_halted_day.isoformat())
                risk_halted_day = None
            current_trading_day = day
            day_start_equity = equity_value
        equity_peak = max(equity_peak, equity_value)
        daily_pnl = equity_value - day_start_equity
        reason = risk_halt_reason(
            equity=equity_value,
            equity_peak=equity_peak,
            daily_pnl=daily_pnl,
            day_start_equity=day_start_equity,
            max_daily_loss_pct=config.max_daily_loss_pct,
            max_drawdown_pct=config.max_drawdown_pct,
        )
        if reason and risk_halted_day != day:
            risk_halted_day = day
            record_event(
                "RISK_HALT", at,
                reason=reason,
                server_trading_day=day.isoformat(),
                equity=equity_value,
                daily_pnl=daily_pnl,
                current_drawdown_pct=max(0.0, (equity_peak - equity_value) / equity_peak) if equity_peak else None,
            )
        if current_trading_day is not None:
            daily_equity[day] = {
                "equity": equity_value,
                "daily_pnl": daily_pnl,
                "risk_halted": risk_halted_day == day,
                "equity_peak": equity_peak,
            }
        return equity_value

    start_index = int(np.searchsorted(m1_ns, start_utc.value, side="left"))
    end_index = int(np.searchsorted(m1_ns, end_utc.value, side="left"))
    coverage_start = pd.Timestamp(m1_ns[start_index], tz="UTC") if start_index < len(m1_ns) else None
    coverage_last = pd.Timestamp(m1_ns[end_index - 1], tz="UTC") if end_index > start_index else None
    coverage_ok = bool(
        coverage_start is not None and coverage_last is not None
        and coverage_start <= start_utc + pd.Timedelta(days=5)
        and coverage_last + pd.Timedelta(minutes=1) >= end_utc - pd.Timedelta(days=5)
    )
    m1_open = m1["open"].to_numpy(dtype=float)
    m1_high = m1["high"].to_numpy(dtype=float)
    m1_low = m1["low"].to_numpy(dtype=float)
    m1_close = m1["close"].to_numpy(dtype=float)
    m5_high = m5["high"].to_numpy(dtype=float)
    m5_low = m5["low"].to_numpy(dtype=float)
    h1_midline = h1_ranges["midline"].to_numpy(dtype=float)

    for index in range(start_index, end_index):
        bar_time = m1["timestamp_utc"].iloc[index]
        bar_ns = int(m1_ns[index])
        bar_close_time = bar_time + pd.Timedelta(minutes=1)
        local = bar_time.tz_convert(tz).to_pydatetime()
        cycle_id, _, _ = _cycle_for(local, config.cycle_hours, cycle_anchor)
        bid_open, bid_low = float(m1_open[index]), float(m1_low[index])
        bid_high, bid_close = float(m1_high[index]), float(m1_close[index])
        ask_open, ask_high, ask_close = (
            bid_open + config.spread_usd,
            bid_high + config.spread_usd,
            bid_close + config.spread_usd,
        )

        previous_day = m1["timestamp_utc"].iloc[index - 1].date() if index > 0 else None
        apply_swap_for_crossed_days(previous_day, bar_time.date(), bar_time)
        gap_minutes = (
            (bar_time - m1["timestamp_utc"].iloc[index - 1]).total_seconds() / 60.0
            if index > 0 else 0.0
        )
        is_session_resume = gap_minutes > 30.0
        if is_session_resume:
            if positions:
                session_breaks_with_position_count += 1
            record_event(
                "SESSION_RESUMED", bar_time,
                previous_bar_utc=m1["timestamp_utc"].iloc[index - 1].isoformat(),
                break_minutes=gap_minutes,
                positions_carried=len(positions),
            )
        elif gap_minutes > 1.0:
            data_gap_count += 1
            if positions:
                data_gap_with_position_count += 1
            record_event(
                "DATA_GAP", bar_time,
                missing_minutes=max(0, int(gap_minutes) - 1),
                open_positions=len(positions),
                stop_path_uncertain=bool(positions),
            )

        # A stop crossed at the current executable open takes precedence.
        for position in list(positions):
            if position.side == "LONG" and bid_open <= position.active_stop:
                close_position(position, bar_time, bar_time, bid_open - config.slippage_usd_per_side,
                               bid_open + config.spread_usd / 2.0, "STOP_LOSS", cycle_id)
            elif position.side == "SHORT" and ask_open >= position.active_stop:
                close_position(position, bar_time, bar_time, ask_open + config.slippage_usd_per_side,
                               ask_open - config.spread_usd / 2.0, "STOP_LOSS", cycle_id)

        current_equity = update_risk_halt(bar_time, bid_open)
        local_minute = local.minute
        if local_minute in (0, 30) and local.second == 0:
            m5_index = int(np.searchsorted(m5_close_ns, bar_ns, side="right") - 1)
            if m5_index >= 0:
                for position in positions:
                    candidate = float(m5_low[m5_index] if position.side == "LONG" else m5_high[m5_index])
                    tightened = tighten_stop(position.side, position.active_stop, candidate)
                    market_side = tightened < bid_open if position.side == "LONG" else tightened > ask_open
                    if tightened != position.active_stop and market_side:
                        old = position.active_stop
                        position.active_stop = tightened
                        position.stop_updates += 1
                        record_event(
                            "TRAIL_STOP_RAISED" if position.side == "LONG" else "TRAIL_STOP_LOWERED",
                            bar_time,
                            position_id=position.position_id,
                            old_stop=old,
                            new_stop=tightened,
                            source_m5_close_utc=pd.Timestamp(m5_close_ns[m5_index], tz="UTC").isoformat(),
                        )
                    elif tightened != position.active_stop:
                        record_event("TRAIL_UPDATE_SKIPPED_MARKET_SIDE", bar_time,
                                     position_id=position.position_id, candidate=tightened)

        upcoming_break_pos = bisect_left(break_indices, index)
        next_break = session_breaks[break_indices[upcoming_break_pos]] if upcoming_break_pos < len(break_indices) else None
        next_break_start = next_break.break_start_utc if next_break else None
        next_session_close = next_break.last_executable_close_utc if next_break else None

        if local_minute % config.check_minutes == 0 and local.second == 0:
            signal_checks += 1
            h1_index = int(np.searchsorted(h1_close_ns, bar_ns, side="right") - 1)
            if h1_index >= 0 and np.isfinite(h1_midline[h1_index]):
                midline = float(h1_midline[h1_index])
                direction = "LONG" if bid_open > midline else "SHORT" if bid_open < midline else None
                if direction:
                    start_ns = bar_ns - pd.Timedelta(minutes=5).value
                    cross_m1 = m1_crosses.latest_unused(
                        start_ns, bar_ns, direction, consumed_crosses,
                        require_latest=config.require_latest_m1_cross,
                    )
                    cross_m5 = m5_crosses.latest_unused(start_ns, bar_ns, direction, consumed_crosses)
                    if cross_m1 and cross_m5:
                        m5_index = int(np.searchsorted(m5_close_ns, bar_ns, side="right") - 1)
                        if m5_index < 0:
                            rejected_signals += 1
                            record_event("ENTRY_REJECTED", bar_time, reason="M5_STOP_DATA_UNAVAILABLE", direction=direction)
                        else:
                            stop = float(m5_low[m5_index] if direction == "LONG" else m5_high[m5_index])
                            entry_fill = (
                                bid_open + config.spread_usd + config.slippage_usd_per_side
                                if direction == "LONG" else bid_open - config.slippage_usd_per_side
                            )
                            entry_mid = bid_open + config.spread_usd / 2.0
                            stop_market_distance = bid_open - stop if direction == "LONG" else stop - ask_open
                            if stop_market_distance < config.min_stop_distance_usd - 1e-12:
                                rejected_signals += 1
                                record_event(
                                    "ENTRY_REJECTED", bar_time, reason="MIN_STOP_DISTANCE",
                                    direction=direction, stop_distance_usd=stop_market_distance,
                                    min_stop_distance_usd=config.min_stop_distance_usd,
                                    m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2],
                                )
                            elif (direction == "LONG" and stop >= bid_open) or (direction == "SHORT" and stop <= ask_open):
                                rejected_signals += 1
                                record_event(
                                    "ENTRY_REJECTED", bar_time, reason="INITIAL_STOP_INVALID_SIDE",
                                    direction=direction, stop=stop, decision_bid=bid_open,
                                )
                            else:
                                stop_distance = abs(entry_fill - stop)
                                sizing = size_for_stop(
                                    equity=current_equity,
                                    risk_per_trade_pct=config.risk_per_trade_pct,
                                    fixed_fallback_lots=config.entry_lots,
                                    stop_distance_usd=stop_distance,
                                    contract_size_oz=config.contract_size_oz,
                                    volume_min=config.volume_min_lots,
                                    volume_step=config.volume_step_lots,
                                    max_stop_distance_usd=config.max_stop_distance_usd,
                                )
                                if sizing.lots is None:
                                    skipped_risk_signals += 1
                                    record_event(
                                        "ENTRY_SKIPPED_RISK", bar_time,
                                        reason=sizing.reason,
                                        direction=direction,
                                        stop_distance_usd=sizing.stop_distance_usd,
                                        max_stop_distance_usd=config.max_stop_distance_usd,
                                        current_equity=current_equity,
                                        risk_budget_usd=sizing.risk_budget_usd,
                                        m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2],
                                        crosses_consumed=False,
                                    )
                                else:
                                    requested_lots = sizing.lots
                                    existing_lots = sum(p.lots for p in positions)
                                    required_margin = margin_required(
                                        requested_lots, entry_mid, config.contract_size_oz, config.leverage
                                    )
                                    current_margin = sum(
                                        margin_required(p.lots, entry_mid, config.contract_size_oz, config.leverage)
                                        for p in positions
                                    )
                                    before_close = within_entry_buffer(
                                        bar_time, next_session_close, config.no_new_entry_before_close_minutes
                                    )
                                    blocked = entry_block_reason(
                                        cycle_id=cycle_id,
                                        entries_this_cycle=entries_by_cycle.get(cycle_id, 0),
                                        max_entries_per_cycle=config.max_entries_per_cycle,
                                        halted_cycles=halted_cycles,
                                        risk_halted=risk_halted_day == bar_time.date(),
                                        entry_lockout=False,
                                        in_blackout=is_utc_blackout(bar_time.to_pydatetime(), config.blackout_windows),
                                        before_session_close=before_close,
                                        aggregate_lots=existing_lots,
                                        requested_lots=requested_lots,
                                        max_total_lots=config.max_total_lots,
                                    )
                                    if blocked is None and current_equity - current_margin < required_margin:
                                        blocked = "INSUFFICIENT_MARGIN"
                                    if blocked:
                                        rejected_signals += 1
                                        record_event(
                                            "ENTRY_REJECTED", bar_time,
                                            reason=blocked,
                                            direction=direction,
                                            cycle_id=cycle_id,
                                            m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2],
                                            next_session_break_utc=next_break_start.isoformat() if next_break_start is not None else None,
                                            next_session_close_utc=next_session_close.isoformat() if next_session_close is not None else None,
                                            crosses_consumed=False,
                                        )
                                    else:
                                        entry_counter += 1
                                        initial_risk = stop_distance * requested_lots * config.contract_size_oz
                                        position = Position(
                                            position_id=f"XAU-{entry_counter:08d}",
                                            side=direction,
                                            lots=requested_lots,
                                            entry_time_utc=bar_time,
                                            entry_cycle_id=cycle_id,
                                            entry_bid=bid_open,
                                            entry_fill=entry_fill,
                                            entry_mid=entry_mid,
                                            initial_stop=stop,
                                            active_stop=stop,
                                            initial_risk_usd=initial_risk,
                                            m1_cross_id=cross_m1[2],
                                            m5_cross_id=cross_m5[2],
                                            entry_regime=_market_regime(bar_ns, h1, h1_close_ns, config),
                                        )
                                        positions.append(position)
                                        entries_by_cycle[cycle_id] = entries_by_cycle.get(cycle_id, 0) + 1
                                        consumed_crosses.update((cross_m1[2], cross_m5[2]))
                                        record_event(
                                            "ENTRY_FILLED", bar_time,
                                            position_id=position.position_id,
                                            direction=direction,
                                            lots=requested_lots,
                                            risk_budget_usd=sizing.risk_budget_usd,
                                            stop_distance_usd=stop_distance,
                                            initial_risk_usd=initial_risk,
                                            entry_fill=entry_fill,
                                            initial_stop=stop,
                                            midline=midline,
                                            cycle_id=cycle_id,
                                            m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2],
                                        )

        # MAE/MFE are M1 OHLC estimates; bar-internal path ordering is unknowable.
        for position in positions:
            if position.side == "LONG":
                favorable = max(0.0, bid_high - position.entry_mid) * position.lots * config.contract_size_oz
                adverse = max(0.0, position.entry_mid - bid_low) * position.lots * config.contract_size_oz
            else:
                favorable = max(0.0, position.entry_mid - bid_low) * position.lots * config.contract_size_oz
                adverse = max(0.0, bid_high - position.entry_mid) * position.lots * config.contract_size_oz
            position.mfe_usd = max(position.mfe_usd, favorable)
            position.mae_usd = max(position.mae_usd, adverse)

        # Intrabar stops use the currently active stop; M1 OHLC cannot reveal exact path.
        for position in list(positions):
            if position.side == "LONG" and bid_low <= position.active_stop:
                stop_bid = position.active_stop
                close_position(position, bar_close_time, bar_time, stop_bid - config.slippage_usd_per_side,
                               stop_bid + config.spread_usd / 2.0, "STOP_LOSS", cycle_id)
            elif position.side == "SHORT" and ask_high >= position.active_stop:
                stop_ask = position.active_stop
                close_position(position, bar_close_time, bar_time, stop_ask + config.slippage_usd_per_side,
                               stop_ask - config.spread_usd / 2.0, "STOP_LOSS", cycle_id)

        # A >30-minute gap is an inferred product session break. Close on its last tradable M1 bar.
        session_break = session_breaks.get(index)
        if session_break and positions and not config.allow_hold_through_session_break:
            close_cycle, _, _ = _cycle_for(bar_close_time.tz_convert(tz).to_pydatetime(), config.cycle_hours, cycle_anchor)
            for position in list(positions):
                exit_mid = bid_close + config.spread_usd / 2.0
                exit_fill = (
                    bid_close - config.slippage_usd_per_side
                    if position.side == "LONG"
                    else ask_close + config.slippage_usd_per_side
                )
                close_position(
                    position,
                    bar_close_time,
                    bar_time,
                    exit_fill,
                    exit_mid,
                    "SESSION_CLOSE",
                    close_cycle,
                    event_details={
                        "last_executable_bar_utc": bar_time.isoformat(),
                        "inferred_break_start_utc": session_break.break_start_utc.isoformat(),
                        "close_buffer_minutes": config.session_close_buffer_minutes,
                        "stop_counted": False,
                    },
                )

        # Broker-style margin call / stop-out simulation on the latest executable close.
        check_margin(bar_close_time, bid_close, cycle_id)
        equity_close = update_risk_halt(bar_close_time, bid_close)
        if current_trading_day is not None:
            daily_equity[current_trading_day] = {
                "equity": equity_close,
                "daily_pnl": equity_close - day_start_equity,
                "risk_halted": risk_halted_day == current_trading_day,
                "equity_peak": equity_peak,
            }

    trade_frame = pd.DataFrame(trades)
    event_frame = pd.DataFrame(events)
    equity_frame = pd.DataFrame(
        [
            {"date_utc": day.isoformat(), **value}
            for day, value in sorted(daily_equity.items())
        ]
    )
    if not equity_frame.empty:
        first_day = date.fromisoformat(equity_frame.iloc[0]["date_utc"])
        last_day = date.fromisoformat(equity_frame.iloc[-1]["date_utc"])
        all_days = pd.date_range(first_day, last_day, freq="D")
        equity_frame = (
            equity_frame.set_index(pd.to_datetime(equity_frame["date_utc"]))
            .drop(columns=["date_utc"])
            .reindex(all_days)
            .ffill()
            .rename_axis("date_utc")
            .reset_index()
        )
    open_frame = pd.DataFrame(
        [
            {
                "position_id": p.position_id,
                "side": p.side,
                "lots": p.lots,
                "entry_time_utc": p.entry_time_utc.isoformat(),
                "entry_cycle_id": p.entry_cycle_id,
                "entry_fill": p.entry_fill,
                "entry_mid": p.entry_mid,
                "initial_stop": p.initial_stop,
                "active_stop": p.active_stop,
                "initial_risk_usd": p.initial_risk_usd,
                "swap": p.swap,
                "mae_usd": p.mae_usd,
                "mfe_usd": p.mfe_usd,
                "stop_updates": p.stop_updates,
            }
            for p in positions
        ]
    )
    metrics = calculate_metrics(
        trades=trade_frame,
        equity=equity_frame,
        initial_equity=config.initial_equity,
        contract_size_oz=config.contract_size_oz,
        open_positions=len(positions),
        open_position_frame=open_frame,
        rejected_signals=rejected_signals,
        signal_checks=signal_checks,
    )
    metrics["total_swap"] = cumulative_swap
    metrics["margin_stop_out_count"] = margin_stop_out_count
    metrics["session_close_count"] = int((trade_frame.get("close_reason", pd.Series(dtype=str)) == "SESSION_CLOSE").sum())
    metrics["risk_halt_count"] = int((event_frame.get("event_type", pd.Series(dtype=str)) == "RISK_HALT").sum())
    metrics["entry_skipped_risk_count"] = skipped_risk_signals

    cycle_ids = sorted(set(entries_by_cycle) | set(losing_stops_by_cycle) | set(all_stops_by_cycle))
    cycle_report = pd.DataFrame(
        [
            {
                "cycle_id": cycle_id,
                "entries": entries_by_cycle.get(cycle_id, 0),
                "losing_stops": losing_stops_by_cycle.get(cycle_id, 0),
                "all_stops": all_stops_by_cycle.get(cycle_id, 0),
                "halted": cycle_id in halted_cycles,
            }
            for cycle_id in cycle_ids
        ]
    )
    reports = {
        "by_side": _group_report(trade_frame, "side"),
        "by_year": _group_report(trade_frame.assign(year=trade_frame["exit_time_utc"].str[:4]) if not trade_frame.empty else trade_frame, "year"),
        "by_market_state": _group_report(trade_frame, "entry_regime"),
        "by_cycle": cycle_report,
        "mae_mfe": trade_frame[[col for col in ("position_id", "side", "initial_risk_usd", "mae_usd", "mfe_usd", "mae_r", "mfe_r") if col in trade_frame]].copy(),
    }
    monetary_available = True
    audit = {
        **audit_obj.as_dict(),
        "monetary_metrics_available": monetary_available,
        "contract_size_oz": config.contract_size_oz,
        "account_currency": config.account_currency,
        "ohlc_price_side": config.ohlc_price_side,
        "data_gaps": data_gap_count,
        "data_gaps_with_open_positions": data_gap_with_position_count,
        "session_breaks_inferred": len(session_breaks),
        "session_breaks_with_positions_at_resume": session_breaks_with_position_count,
        "session_close_policy": "M1 gaps > 30 minutes define a session break; close on the last observed executable M1 bar",
        "allow_hold_through_session_break": config.allow_hold_through_session_break,
        "weekend_close_unverifiable_count": 0,
        "signals_checked": signal_checks,
        "rejected_signals": rejected_signals,
        "entry_skipped_risk_count": skipped_risk_signals,
        "unconsumed_cross_policy": "crosses consumed only after successful simulated entry",
        "risk_rule": {
            "risk_per_trade_pct": config.risk_per_trade_pct,
            "max_stop_distance_usd": config.max_stop_distance_usd,
            "max_daily_loss_pct": config.max_daily_loss_pct,
            "max_drawdown_pct": config.max_drawdown_pct,
        },
        "cycle_stop_rule": {
            "scope": config.stop_rule_scope,
            "max_entries_per_cycle": config.max_entries_per_cycle,
            "max_losing_stops_per_cycle": config.max_losing_stops_per_cycle,
            "count_profitable_stops": config.count_profitable_stops,
        },
        "requested_start_utc": start_utc.isoformat(),
        "requested_end_utc_exclusive": end_utc.isoformat(),
        "first_m1_in_requested_window_utc": coverage_start.isoformat() if coverage_start is not None else None,
        "last_m1_in_requested_window_utc": coverage_last.isoformat() if coverage_last is not None else None,
        "requested_range_endpoint_coverage_ok_5d_tolerance": coverage_ok,
        "execution_path_audit_valid": not data_gap_with_position_count and (
            not session_breaks_with_position_count or config.allow_hold_through_session_break
        ) and coverage_ok,
        "valid_for_performance_review": not data_gap_with_position_count and coverage_ok,
        "performance_warning": "M1 OHLC cannot reveal intra-bar path; financing and margin use the configured simplified model.",
        "margin_model": "gross notional / leverage; hedged legs both consume margin; stop-out closes the worst floating leg first",
        "swap_model": "per-lot charge at each crossed UTC server date; configured weekday receives triple multiplier",
        "mae_mfe_warning": "M1 high/low estimates do not reveal whether the excursion preceded an intrabar stop.",
        "cost_doubling_warning": "Mechanical per-trade sensitivity; does not rerun sizing or strategy decisions under doubled costs.",
    }
    return BacktestResult(trade_frame, event_frame, equity_frame, open_frame, metrics, audit, reports)
