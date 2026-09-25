from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from .indicators import h1_range_frame, macd_frame
from .metrics import calculate_metrics
from .models import BacktestConfig
from .data import timestamp_ns, validate_frames
from .cycles import cycle_window, resolve_cycle_anchor


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
    m1_cross_id: str
    m5_cross_id: str
    stop_updates: int = 0


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    events: pd.DataFrame
    equity: pd.DataFrame
    open_positions: pd.DataFrame
    metrics: dict[str, float | int | str | None]
    audit: dict[str, object]


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
    indicators = macd_frame(
        frame["close"], config.macd_fast, config.macd_slow, config.macd_signal
    )
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
        self, start_ns: int, end_ns: int, direction: str, consumed: set[str]
    ) -> tuple[int, str, str] | None:
        left = int(np.searchsorted(self.times, start_ns, side="left"))
        right = int(np.searchsorted(self.times, end_ns, side="right"))
        for item in reversed(self.events[left:right]):
            if item[1] == direction and item[2] not in consumed:
                return item
        return None


def _weekend_flatten_rows(m1: pd.DataFrame, config: BacktestConfig) -> dict[int, tuple[pd.Timestamp, float]]:
    tz = ZoneInfo(config.timezone)
    cutoff_time = time.fromisoformat(config.weekend_close_local)
    ends_utc = m1["timestamp_utc"] + pd.Timedelta(minutes=1)
    ends_local = ends_utc.dt.tz_convert(tz)
    candidates: dict[date, tuple[int, pd.Timestamp, float]] = {}
    for index, local_end in enumerate(ends_local):
        if local_end.weekday() != 4 or local_end.timetz().replace(tzinfo=None) > cutoff_time:
            continue
        cutoff_local = datetime.combine(local_end.date(), cutoff_time, tzinfo=tz)
        age = (cutoff_local - local_end.to_pydatetime()).total_seconds() / 60.0
        previous = candidates.get(local_end.date())
        if previous is None or index > previous[0]:
            candidates[local_end.date()] = (index, ends_utc.iloc[index], age)
    return {index: (cutoff, age) for index, cutoff, age in candidates.values()}


def run_backtest(m1: pd.DataFrame, m5: pd.DataFrame, h1: pd.DataFrame, config: BacktestConfig) -> BacktestResult:
    """Run a deterministic M1 event simulation. This module never connects to a broker."""
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
    flatten_rows = _weekend_flatten_rows(m1, config)

    positions: list[Position] = []
    trades: list[dict[str, object]] = []
    events: list[dict[str, object]] = []
    consumed_crosses: set[str] = set()
    entries_by_cycle: dict[str, int] = {}
    losing_stops_by_cycle: dict[str, int] = {}
    paused_cycles: set[str] = set()
    daily_equity: dict[date, float] = {}
    realized_net = 0.0
    entry_counter = 0
    data_gap_count = 0
    data_gap_with_position_count = 0
    signal_checks = 0
    rejected_signals = 0
    weekend_unverifiable_count = 0

    def record_event(event_type: str, at: pd.Timestamp, **details: object) -> None:
        events.append({"event_type": event_type, "event_time_utc": at.isoformat(), **details})

    def close_position(
        position: Position,
        close_time: pd.Timestamp,
        trigger_time: pd.Timestamp,
        exit_fill: float,
        exit_mid: float,
        reason: str,
        cycle_id: str,
    ) -> None:
        nonlocal realized_net
        gross: float | None = None
        spread_cost: float | None = None
        slippage_cost: float | None = None
        commission = position.lots * config.commission_usd_per_lot_side * 2.0
        net: float | None = None
        if config.contract_size_oz is not None:
            size = position.lots * config.contract_size_oz
            sign = 1.0 if position.side == "LONG" else -1.0
            gross = sign * size * (exit_mid - position.entry_mid)
            spread_cost = size * config.spread_usd
            slippage_cost = size * config.slippage_usd_per_side * 2.0
            net = gross - spread_cost - slippage_cost - commission
            realized_net += net
        trades.append(
            {
                "position_id": position.position_id,
                "side": position.side,
                "lots": position.lots,
                "entry_time_utc": position.entry_time_utc.isoformat(),
                "entry_cycle_id": position.entry_cycle_id,
                "entry_bid": position.entry_bid,
                "entry_fill": position.entry_fill,
                "entry_mid": position.entry_mid,
                "initial_stop": position.initial_stop,
                "exit_time_utc": close_time.isoformat(),
                "stop_trigger_time_utc": trigger_time.isoformat(),
                "exit_fill": exit_fill,
                "exit_mid": exit_mid,
                "active_stop": position.active_stop,
                "close_reason": reason,
                "exit_cycle_id": cycle_id,
                "hold_minutes": (close_time - position.entry_time_utc).total_seconds() / 60.0,
                "m1_cross_id": position.m1_cross_id,
                "m5_cross_id": position.m5_cross_id,
                "stop_updates": position.stop_updates,
                "gross_pnl": gross,
                "spread_cost": spread_cost,
                "slippage_cost": slippage_cost,
                "commission": commission,
                "swap": None,
                "net_pnl": net,
            }
        )
        if reason == "STOP_LOSS" and net is not None and net < 0:
            losing_stops_by_cycle[cycle_id] = losing_stops_by_cycle.get(cycle_id, 0) + 1
            loss_count = losing_stops_by_cycle[cycle_id]
            record_event(
                "LOSING_STOP_COUNTED", trigger_time,
                cycle_id=cycle_id, losing_stop_count=loss_count, net_pnl=net,
            )
            if loss_count == config.pause_after_stops:
                paused_id = _next_cycle_id(
                    trigger_time.tz_convert(tz).to_pydatetime(), config.cycle_hours, cycle_anchor
                )
                paused_cycles.add(paused_id)
                record_event(
                    "CYCLE_PAUSED",
                    trigger_time,
                    cycle_id=cycle_id,
                    paused_cycle_id=paused_id,
                    losing_stop_count=loss_count,
                )
        elif reason == "STOP_LOSS":
            record_event(
                "NON_LOSING_STOP_NOT_COUNTED", trigger_time,
                cycle_id=cycle_id, net_pnl=net, pnl_available=net is not None,
            )
        record_event(
            reason,
            close_time,
            position_id=position.position_id,
            side=position.side,
            lots=position.lots,
            exit_fill=exit_fill,
            active_stop=position.active_stop,
            cycle_id=cycle_id,
            net_pnl=net,
        )
        positions.remove(position)

    def mark_equity(bid: float, at_local_date: date) -> float:
        mark_mid = bid + config.spread_usd / 2.0
        open_net = 0.0
        if config.contract_size_oz is not None:
            for position in positions:
                size = position.lots * config.contract_size_oz
                direction = 1.0 if position.side == "LONG" else -1.0
                open_net += direction * size * (mark_mid - position.entry_mid)
                open_net -= size * config.spread_usd
                open_net -= size * config.slippage_usd_per_side * 2.0
                open_net -= position.lots * config.commission_usd_per_lot_side * 2.0
        value = config.initial_equity + realized_net + open_net
        daily_equity[at_local_date] = value
        return value

    start_index = int(np.searchsorted(m1_ns, start_utc.value, side="left"))
    end_index = int(np.searchsorted(m1_ns, end_utc.value, side="left"))
    coverage_start = pd.Timestamp(m1_ns[start_index], tz="UTC") if start_index < len(m1_ns) else None
    coverage_last = pd.Timestamp(m1_ns[end_index - 1], tz="UTC") if end_index > start_index else None
    coverage_ok = bool(
        coverage_start is not None
        and coverage_last is not None
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
        local = bar_time.tz_convert(tz).to_pydatetime()
        cycle_id, _, _ = _cycle_for(local, config.cycle_hours, cycle_anchor)
        bid_open = float(m1_open[index])
        bid_low = float(m1_low[index])
        bid_high = float(m1_high[index])
        bid_close = float(m1_close[index])
        ask_open = bid_open + config.spread_usd
        ask_high = bid_high + config.spread_usd
        bar_close_time = bar_time + pd.Timedelta(minutes=1)

        gap = index > 0 and m1_ns[index] - m1_ns[index - 1] > pd.Timedelta(minutes=1).value
        if gap:
            data_gap_count += 1
            if positions:
                data_gap_with_position_count += 1
            record_event(
                "DATA_GAP",
                bar_time,
                missing_minutes=max(0, int((m1_ns[index] - m1_ns[index - 1]) // pd.Timedelta(minutes=1).value) - 1),
                open_positions=len(positions),
                stop_path_uncertain=bool(positions),
            )

        # A stop crossed at the current executable open takes precedence over stop updates.
        for position in list(positions):
            if position.side == "LONG" and bid_open <= position.active_stop:
                close_position(
                    position, bar_time, bar_time, bid_open - config.slippage_usd_per_side,
                    bid_open + config.spread_usd / 2.0, "STOP_LOSS", cycle_id
                )
            elif position.side == "SHORT" and ask_open >= position.active_stop:
                close_position(
                    position, bar_time, bar_time, ask_open + config.slippage_usd_per_side,
                    ask_open - config.spread_usd / 2.0, "STOP_LOSS", cycle_id
                )

        # Fixed local :00/:30 trailing update using the last M5 candle closed by this instant.
        if local.minute in (0, 30) and local.second == 0:
            m5_index = int(np.searchsorted(m5_close_ns, bar_ns, side="right") - 1)
            if m5_index >= 0:
                for position in positions:
                    if position.side == "LONG":
                        candidate = float(m5_low[m5_index])
                        if candidate > position.active_stop and candidate < bid_open:
                            old_stop = position.active_stop
                            position.active_stop = candidate
                            position.stop_updates += 1
                            record_event(
                                "TRAIL_STOP_RAISED", bar_time, position_id=position.position_id,
                                old_stop=old_stop, new_stop=candidate, source_m5_close_utc=pd.Timestamp(m5_close_ns[m5_index], tz="UTC").isoformat()
                            )
                        elif candidate >= bid_open:
                            record_event("TRAIL_UPDATE_SKIPPED_MARKET_SIDE", bar_time, position_id=position.position_id, candidate=candidate)
                    else:
                        candidate = float(m5_high[m5_index])
                        if candidate < position.active_stop and candidate > ask_open:
                            old_stop = position.active_stop
                            position.active_stop = candidate
                            position.stop_updates += 1
                            record_event(
                                "TRAIL_STOP_LOWERED", bar_time, position_id=position.position_id,
                                old_stop=old_stop, new_stop=candidate, source_m5_close_utc=pd.Timestamp(m5_close_ns[m5_index], tz="UTC").isoformat()
                            )
                        elif candidate <= ask_open:
                            record_event("TRAIL_UPDATE_SKIPPED_MARKET_SIDE", bar_time, position_id=position.position_id, candidate=candidate)

        # Entry decision uses only bars whose close time is <= current M1 open time.
        if local.minute % config.check_minutes == 0 and local.second == 0:
            signal_checks += 1
            h1_index = int(np.searchsorted(h1_close_ns, bar_ns, side="right") - 1)
            if h1_index >= 0 and np.isfinite(h1_midline[h1_index]):
                midline = float(h1_midline[h1_index])
                direction = "LONG" if bid_open > midline else "SHORT" if bid_open < midline else None
                if direction and not gap:
                    start_ns = bar_ns - pd.Timedelta(minutes=5).value
                    cross_m1 = m1_crosses.latest_unused(start_ns, bar_ns, direction, consumed_crosses)
                    cross_m5 = m5_crosses.latest_unused(start_ns, bar_ns, direction, consumed_crosses)
                    if cross_m1 and cross_m5:
                        entries = entries_by_cycle.get(cycle_id, 0)
                        total_lots = sum(abs(position.lots) for position in positions)
                        reason: str | None = None
                        cutoff_time = time.fromisoformat(config.weekend_close_local)
                        weekend_blackout = local.weekday() >= 5 or (
                            local.weekday() == 4 and local.timetz().replace(tzinfo=None) >= cutoff_time
                        )
                        if weekend_blackout:
                            reason = "WEEKEND_ENTRY_BLOCKED"
                        elif cycle_id in paused_cycles:
                            reason = "CYCLE_PAUSED"
                        elif entries >= config.max_entries_per_cycle:
                            reason = "CYCLE_ENTRY_LIMIT"
                        elif (
                            config.max_total_lots is not None
                            and total_lots + config.entry_lots > config.max_total_lots + 1e-12
                        ):
                            reason = "MAX_TOTAL_LOTS"
                        else:
                            m5_index = int(np.searchsorted(m5_close_ns, bar_ns, side="right") - 1)
                            if m5_index < 0:
                                reason = "M5_STOP_DATA_UNAVAILABLE"
                            else:
                                stop = float(m5_low[m5_index] if direction == "LONG" else m5_high[m5_index])
                                if (direction == "LONG" and stop >= bid_open) or (direction == "SHORT" and stop <= ask_open):
                                    reason = "INITIAL_STOP_INVALID_SIDE"
                        if reason:
                            rejected_signals += 1
                            record_event(
                                "ENTRY_REJECTED", bar_time, direction=direction, reason=reason,
                                cycle_id=cycle_id, m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2],
                                midline=midline, decision_bid=bid_open
                            )
                        else:
                            entry_counter += 1
                            entry_mid = bid_open + config.spread_usd / 2.0
                            entry_fill = (
                                bid_open + config.spread_usd + config.slippage_usd_per_side
                                if direction == "LONG"
                                else bid_open - config.slippage_usd_per_side
                            )
                            position = Position(
                                position_id=f"XAU-{entry_counter:08d}", side=direction,
                                lots=config.entry_lots, entry_time_utc=bar_time,
                                entry_cycle_id=cycle_id, entry_bid=bid_open,
                                entry_fill=entry_fill, entry_mid=entry_mid,
                                initial_stop=stop, active_stop=stop,
                                m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2]
                            )
                            positions.append(position)
                            entries_by_cycle[cycle_id] = entries_by_cycle.get(cycle_id, 0) + 1
                            consumed_crosses.update((cross_m1[2], cross_m5[2]))
                            record_event(
                                "ENTRY_FILLED", bar_time, position_id=position.position_id,
                                direction=direction, lots=config.entry_lots, entry_fill=entry_fill,
                                initial_stop=stop, midline=midline, cycle_id=cycle_id,
                                m1_cross_id=cross_m1[2], m5_cross_id=cross_m5[2]
                            )

        # Intrabar stops: M1 OHLC cannot reveal exact path; fills use adverse stop price plus slippage.
        for position in list(positions):
            if position.side == "LONG" and bid_low <= position.active_stop:
                stop_bid = position.active_stop
                close_position(
                    position, bar_close_time, bar_time, stop_bid - config.slippage_usd_per_side,
                    stop_bid + config.spread_usd / 2.0, "STOP_LOSS", cycle_id
                )
            elif position.side == "SHORT" and ask_high >= position.active_stop:
                stop_ask = position.active_stop
                close_position(
                    position, bar_close_time, bar_time, stop_ask + config.slippage_usd_per_side,
                    stop_ask - config.spread_usd / 2.0, "STOP_LOSS", cycle_id
                )

        # The last observed executable M1 close before the local Friday cutoff is an active close,
        # not a stop. Its observed quote time is recorded separately from the scheduled deadline.
        if index in flatten_rows and positions:
            deadline_utc, age_minutes = flatten_rows[index]
            local_close = bar_close_time.tz_convert(tz).to_pydatetime()
            close_cycle, _, _ = _cycle_for(local_close, config.cycle_hours, cycle_anchor)
            for position in list(positions):
                if age_minutes > config.weekend_max_quote_age_minutes:
                    weekend_unverifiable_count += 1
                    record_event(
                        "WEEKEND_CLOSE_UNVERIFIABLE", bar_close_time,
                        position_id=position.position_id,
                        scheduled_deadline_utc=deadline_utc.isoformat(),
                        quote_age_minutes=age_minutes,
                        allowed_quote_age_minutes=config.weekend_max_quote_age_minutes,
                    )
                    continue
                exit_mid = bid_close + config.spread_usd / 2.0
                exit_fill = (
                    bid_close - config.slippage_usd_per_side
                    if position.side == "LONG"
                    else bid_close + config.spread_usd + config.slippage_usd_per_side
                )
                close_position(
                    position, bar_close_time, bar_time, exit_fill, exit_mid,
                    "FORCED_WEEKEND_CLOSE", close_cycle
                )
                record_event(
                    "WEEKEND_CLOSE_TIMING", bar_close_time,
                    position_id=position.position_id,
                    scheduled_deadline_utc=deadline_utc.isoformat(),
                    quote_age_minutes=age_minutes,
                )

        mark_equity(bid_close, bar_close_time.tz_convert(tz).date())

    trade_frame = pd.DataFrame(trades)
    event_frame = pd.DataFrame(events)
    equity_frame = pd.DataFrame(
        [{"date_local": day.isoformat(), "equity": value} for day, value in sorted(daily_equity.items())]
    )
    if not equity_frame.empty:
        first_day = date.fromisoformat(equity_frame.iloc[0]["date_local"])
        last_day = date.fromisoformat(equity_frame.iloc[-1]["date_local"])
        all_days = pd.date_range(first_day, last_day, freq="D")
        equity_frame = (
            equity_frame.set_index(pd.to_datetime(equity_frame["date_local"]))["equity"]
            .reindex(all_days)
            .ffill()
            .rename("equity")
            .rename_axis("date_local")
            .reset_index()
        )
    open_frame = pd.DataFrame(
        [
            {
                "position_id": p.position_id, "side": p.side, "lots": p.lots,
                "entry_time_utc": p.entry_time_utc.isoformat(), "entry_cycle_id": p.entry_cycle_id,
                "entry_fill": p.entry_fill, "entry_mid": p.entry_mid, "initial_stop": p.initial_stop,
                "active_stop": p.active_stop, "stop_updates": p.stop_updates,
            }
            for p in positions
        ]
    )
    monetary_available = config.contract_size_oz is not None
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
    execution_path_audit_valid = (
        not data_gap_with_position_count and not weekend_unverifiable_count and coverage_ok
    )
    if not execution_path_audit_valid:
        metrics["status"] = "AUDIT_WARNING_UNVERIFIABLE_EXECUTION"
    audit = {
        **audit_obj.as_dict(),
        "monetary_metrics_available": monetary_available,
        "contract_size_oz": config.contract_size_oz,
        "account_currency": config.account_currency,
        "ohlc_price_side": config.ohlc_price_side,
        "data_gaps": data_gap_count,
        "data_gaps_with_open_positions": data_gap_with_position_count,
        "signals_checked": signal_checks,
        "rejected_signals": rejected_signals,
        "unconsumed_cross_policy": "crosses consumed only after successful simulated entry",
        "weekend_close_policy": (
            "last observed Friday M1 close at/before local cutoff; stale quotes beyond tolerance are not filled"
        ),
        "unverified_data_gap_stop_paths": data_gap_with_position_count,
        "weekend_close_unverifiable_positions": weekend_unverifiable_count,
        "requested_start_utc": start_utc.isoformat(),
        "requested_end_utc_exclusive": end_utc.isoformat(),
        "first_m1_in_requested_window_utc": coverage_start.isoformat() if coverage_start is not None else None,
        "last_m1_in_requested_window_utc": coverage_last.isoformat() if coverage_last is not None else None,
        "requested_range_endpoint_coverage_ok_5d_tolerance": coverage_ok,
        "execution_path_audit_valid": execution_path_audit_valid,
        "valid_for_performance_review": execution_path_audit_valid and monetary_available,
        "performance_warning": (
            "USD performance is withheld until contract_size_oz is configured."
            if not monetary_available else "Historical swap is excluded; monetary results are pre-financing."
        ),
        "data_gap_warning": (
            "At least one open position crossed a data gap; stop path and fill are unverifiable."
            if data_gap_with_position_count else None
        ),
    }
    return BacktestResult(trade_frame, event_frame, equity_frame, open_frame, metrics, audit)
