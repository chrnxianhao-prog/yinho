from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass
from datetime import date, datetime, time as wall_time, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from .indicators import h1_range_frame, macd_frame
from .models import BacktestConfig
from .cycles import cycle_window, resolve_cycle_anchor


UTC = timezone.utc


@dataclass(frozen=True)
class EntrySignal:
    side: str
    stop_loss: float
    midline: float
    m1_cross_id: str
    m5_cross_id: str


def cycle_id_for(
    at: datetime,
    timezone_name: str,
    cycle_hours: int = 5,
    anchor: datetime | str = "2020-01-01T09:00:00",
) -> str:
    return cycle_window(at, timezone_name, cycle_hours, anchor)[0]


def next_cycle_id(
    at: datetime,
    timezone_name: str,
    cycle_hours: int = 5,
    anchor: datetime | str = "2020-01-01T09:00:00",
) -> str:
    _, _, end = cycle_window(at, timezone_name, cycle_hours, anchor)
    return f"{end.date().isoformat()}T{end.hour:02d}:{end.minute:02d}"


def next_local_time(now: datetime, timezone_name: str, target: wall_time) -> datetime:
    tz = ZoneInfo(timezone_name)
    local = now.astimezone(tz)
    candidate = datetime.combine(local.date(), target, tzinfo=tz)
    if candidate <= local:
        candidate = datetime.combine(local.date() + timedelta(days=1), target, tzinfo=tz)
    return candidate


def realized_deal_net_pnl(deal: Any) -> float:
    """MT5 realized deal P&L including carrying charges and broker fees."""
    return sum(float(getattr(deal, field, 0.0) or 0.0) for field in ("profit", "commission", "swap", "fee"))


def rates_to_frame(rates: Any, timeframe_minutes: int, as_of: datetime) -> pd.DataFrame:
    columns = ["timestamp_utc", "open", "high", "low", "close"]
    if rates is None or len(rates) == 0:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rates)
    frame["timestamp_utc"] = pd.to_datetime(frame["time"], unit="s", utc=True)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame[columns].sort_values("timestamp_utc").drop_duplicates("timestamp_utc")
    cutoff = pd.Timestamp(as_of).tz_convert("UTC")
    closed = frame[frame["timestamp_utc"] + pd.Timedelta(minutes=timeframe_minutes) <= cutoff]
    return closed.reset_index(drop=True)


def _crosses(frame: pd.DataFrame, timeframe: str, minutes: int, config: BacktestConfig) -> list[tuple[pd.Timestamp, str, str]]:
    if frame.empty:
        return []
    features = macd_frame(frame["close"], config.macd_fast, config.macd_slow, config.macd_signal)
    close_times = frame["timestamp_utc"] + pd.Timedelta(minutes=minutes)
    output: list[tuple[pd.Timestamp, str, str]] = []
    for index, row in enumerate(features.itertuples(index=False)):
        side = "LONG" if row.golden_cross else "SHORT" if row.dead_cross else None
        if side is not None:
            stamp = close_times.iloc[index]
            output.append((stamp, side, f"{timeframe}:{stamp.isoformat()}:{side}"))
    return output


def evaluate_entry_signal(
    m1: pd.DataFrame,
    m5: pd.DataFrame,
    h1: pd.DataFrame,
    *,
    bid: float,
    ask: float,
    at: datetime,
    config: BacktestConfig,
    consumed_crosses: set[str] | None = None,
) -> EntrySignal | None:
    """Evaluate only completed bars at a scheduled decision time."""
    consumed = consumed_crosses or set()
    decision = pd.Timestamp(at).tz_convert("UTC")
    start = decision - pd.Timedelta(minutes=5)
    expected_m1 = pd.date_range(start, periods=5, freq="min", tz="UTC")
    recent_m1 = m1[(m1["timestamp_utc"] >= start) & (m1["timestamp_utc"] < decision)]
    if len(recent_m1) != 5 or not recent_m1["timestamp_utc"].reset_index(drop=True).equals(
        pd.Series(expected_m1, name="timestamp_utc")
    ):
        return None
    if len(h1) < config.range_bars or m5.empty:
        return None

    ranges = h1_range_frame(h1, config.range_bars)
    usable = ranges[ranges["bar_close_time_utc"] <= decision]
    if usable.empty or not pd.notna(usable.iloc[-1]["midline"]):
        return None
    midline = float(usable.iloc[-1]["midline"])
    side = "LONG" if bid > midline else "SHORT" if bid < midline else None
    if side is None:
        return None

    m1_events = _crosses(m1, "M1", 1, config)
    m5_events = _crosses(m5, "M5", 5, config)

    def latest_unused(events: list[tuple[pd.Timestamp, str, str]]) -> str | None:
        eligible = [
            event for event in events
            if start <= event[0] <= decision and event[1] == side and event[2] not in consumed
        ]
        return eligible[-1][2] if eligible else None

    m1_cross_id = latest_unused(m1_events)
    m5_cross_id = latest_unused(m5_events)
    if not m1_cross_id or not m5_cross_id:
        return None

    previous_m5 = m5.iloc[-1]
    stop = float(previous_m5["low"] if side == "LONG" else previous_m5["high"])
    if (side == "LONG" and stop >= bid) or (side == "SHORT" and stop <= ask):
        return None
    return EntrySignal(side, stop, midline, m1_cross_id, m5_cross_id)


class DemoStrategyRunner:
    """Demo-only XAUUSD strategy runner with broker-side stops."""

    def __init__(
        self,
        adapter: Any,
        config: BacktestConfig,
        *,
        stop_new_entries_at: datetime | None,
        cycle_anchor_at: datetime,
        log_path: str | Path,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 60.0,
    ) -> None:
        if stop_new_entries_at is not None and stop_new_entries_at.tzinfo is None:
            raise ValueError("stop_new_entries_at must be timezone-aware")
        if cycle_anchor_at.tzinfo is None:
            raise ValueError("cycle_anchor_at must be timezone-aware")
        if poll_seconds <= 0 or poll_seconds > 10:
            raise ValueError("poll_seconds must be in (0, 10]")
        if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be a finite positive number")
        self.adapter = adapter
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.stop_new_entries_at = stop_new_entries_at.astimezone(UTC) if stop_new_entries_at else None
        self.cycle_anchor_at = cycle_anchor_at.astimezone(self.tz)
        self.log_path = Path(log_path)
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._last_heartbeat_monotonic: float | None = None
        self.started_at = datetime.now(UTC)
        self.entries_by_cycle: dict[str, int] = {}
        self.losing_stops_by_cycle: dict[str, int] = {}
        self.paused_cycles: set[str] = set()
        self.consumed_crosses: set[str] = set()
        self.counted_stop_positions: set[int] = set()
        self.seen_deals: set[int] = set()
        self.last_history_scan = self.started_at - timedelta(seconds=1)
        self.last_data_minute: datetime | None = None
        self.last_entry_minute: datetime | None = None
        self.last_entry_check_at: datetime | None = None
        self.last_entry_check_result = "NOT_CHECKED"
        self.last_quote_time_utc: datetime | None = None
        self.last_runtime_error_type: str | None = None
        self.last_trail_minute: datetime | None = None
        self.last_weekend_attempt: date | None = None
        self.last_weekend_attempt_at: datetime | None = None
        self.frames: dict[str, pd.DataFrame] = {}
        self.entry_lockout = False
        self._stale_logged = False

    def _log(self, event: str, **fields: Any) -> None:
        now = datetime.now(UTC)
        record = {
            "time_utc": now.isoformat(),
            "time_local": now.astimezone(self.tz).isoformat(),
            "event": event,
            **fields,
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        print(json.dumps(record, ensure_ascii=False, default=str), flush=True)

    def _maybe_log_heartbeat(self, *, force: bool = False) -> None:
        """Write a periodic liveness record without changing strategy decisions."""
        now_monotonic = time.monotonic()
        if (
            not force
            and self._last_heartbeat_monotonic is not None
            and now_monotonic - self._last_heartbeat_monotonic < self.heartbeat_seconds
        ):
            return

        now = datetime.now(UTC)
        terminal_connected: bool | None = None
        terminal_query_error: str | None = None
        try:
            terminal = self.adapter.mt5.terminal_info()
            if terminal is not None:
                value = getattr(terminal, "connected", None)
                terminal_connected = bool(value) if value is not None else None
            else:
                terminal_query_error = "NO_TERMINAL_INFO"
        except Exception as exc:
            terminal_query_error = type(exc).__name__

        position_count: int | None = None
        position_query_error: str | None = None
        try:
            position_count = len(self._own_positions())
        except Exception as exc:
            position_query_error = type(exc).__name__

        local_now = now.astimezone(self.tz)
        cycle_id = cycle_id_for(
            local_now, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at
        )
        quote_age = None
        if self.last_quote_time_utc is not None:
            quote_age = abs((now - self.last_quote_time_utc).total_seconds())

        if self.entry_lockout:
            runner_state = "RUNNING_ENTRY_LOCKOUT"
        elif terminal_connected is False:
            runner_state = "RUNNING_MT5_DISCONNECTED"
        elif quote_age is not None and quote_age > 120:
            runner_state = "RUNNING_STALE_QUOTE"
        elif self.stop_new_entries_at is not None and now >= self.stop_new_entries_at:
            runner_state = "MANAGING_ONLY"
        else:
            runner_state = "RUNNING"

        self._log(
            "HEARTBEAT",
            process_id=os.getpid(),
            runner_state=runner_state,
            terminal_connected=terminal_connected,
            terminal_query_error_type=terminal_query_error,
            quote_age_seconds=round(quote_age, 1) if quote_age is not None else None,
            last_quote_time_utc=(
                self.last_quote_time_utc.isoformat() if self.last_quote_time_utc else None
            ),
            last_entry_check_time_utc=(
                self.last_entry_check_at.isoformat() if self.last_entry_check_at else None
            ),
            last_entry_check_result=self.last_entry_check_result,
            strategy_open_positions=position_count,
            position_query_error_type=position_query_error,
            cycle_id=cycle_id,
            entries_opened_this_cycle=self.entries_by_cycle.get(cycle_id, 0),
            losing_stops_this_cycle=self.losing_stops_by_cycle.get(cycle_id, 0),
            entry_lockout=self.entry_lockout,
            last_runtime_error_type=self.last_runtime_error_type,
        )
        self._last_heartbeat_monotonic = now_monotonic

    def _own_positions(self) -> list[Any]:
        mt5 = self.adapter.mt5
        positions = mt5.positions_get(symbol=self.config.symbol)
        if positions is None:
            raise RuntimeError("MT5 position query failed")
        return [
            position for position in positions
            if int(position.magic) == self.adapter.ai_magic
        ]

    def _refresh_frames(self, as_of: datetime) -> None:
        mt5 = self.adapter.mt5
        timeframes = (("M1", mt5.TIMEFRAME_M1, 1), ("M5", mt5.TIMEFRAME_M5, 5), ("H1", mt5.TIMEFRAME_H1, 60))
        frames: dict[str, pd.DataFrame] = {}
        for name, timeframe, minutes in timeframes:
            rates = mt5.copy_rates_from_pos(self.config.symbol, timeframe, 0, 1500)
            frames[name] = rates_to_frame(rates, minutes, as_of)
        if any(frames[name].empty for name in ("M1", "M5", "H1")):
            raise RuntimeError("recent closed M1/M5/H1 bars are unavailable")
        self.frames = frames

    def _scan_stop_deals(self, now: datetime) -> None:
        if (now - self.last_history_scan).total_seconds() < 3:
            return
        mt5 = self.adapter.mt5
        deals = mt5.history_deals_get(self.last_history_scan, now + timedelta(seconds=1))
        if deals is None:
            raise RuntimeError("MT5 deal history is temporarily unavailable")
        self.last_history_scan = now - timedelta(seconds=1)
        sl_reason = int(getattr(mt5, "DEAL_REASON_SL", 4))
        out_entries = {int(getattr(mt5, "DEAL_ENTRY_OUT", 1)), int(getattr(mt5, "DEAL_ENTRY_OUT_BY", 3))}
        for deal in deals:
            ticket = int(deal.ticket)
            if ticket in self.seen_deals:
                continue
            self.seen_deals.add(ticket)
            if (
                int(getattr(deal, "magic", 0)) != self.adapter.ai_magic
                or str(getattr(deal, "symbol", "")) != self.config.symbol
                or int(getattr(deal, "reason", -1)) != sl_reason
                or int(getattr(deal, "entry", -1)) not in out_entries
            ):
                continue
            position_id = int(getattr(deal, "position_id", 0))
            if position_id in self.counted_stop_positions:
                continue
            self.counted_stop_positions.add(position_id)
            deal_time_msc = int(getattr(deal, "time_msc", 0) or 0)
            at = (
                datetime.fromtimestamp(deal_time_msc / 1000.0, UTC)
                if deal_time_msc
                else datetime.fromtimestamp(int(deal.time), UTC)
            )
            net_pnl = realized_deal_net_pnl(deal)
            cycle_id = cycle_id_for(
                at, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at
            )
            if net_pnl >= 0:
                self._log(
                    "NON_LOSING_STOP_NOT_COUNTED", position_id=position_id,
                    cycle_id=cycle_id, realized_net_pnl=net_pnl,
                )
                continue
            count = self.losing_stops_by_cycle.get(cycle_id, 0) + 1
            self.losing_stops_by_cycle[cycle_id] = count
            self._log(
                "LOSING_STOP_EXIT_CONFIRMED", position_id=position_id,
                cycle_id=cycle_id, losing_stop_count=count, realized_net_pnl=net_pnl,
            )
            if count == self.config.pause_after_stops:
                paused_id = next_cycle_id(
                    at, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at
                )
                self.paused_cycles.add(paused_id)
                self._log(
                    "NEXT_CYCLE_PAUSED", stopped_cycle=cycle_id,
                    paused_cycle=paused_id, losing_stop_count=count,
                )

    def _trail_positions(self, at: datetime, bid: float, ask: float) -> None:
        local = at.astimezone(self.tz)
        key = local.replace(second=0, microsecond=0)
        if local.minute not in (0, 30) or local.second > 5 or key == self.last_trail_minute:
            return
        self.last_trail_minute = key
        if "M5" not in self.frames or self.frames["M5"].empty:
            return
        previous = self.frames["M5"].iloc[-1]
        source_close = previous["timestamp_utc"] + pd.Timedelta(minutes=5)
        if source_close.to_pydatetime() > at.astimezone(UTC):
            return
        for position in self._own_positions():
            is_buy = int(position.type) == int(self.adapter.mt5.POSITION_TYPE_BUY)
            candidate = float(previous["low"] if is_buy else previous["high"])
            active = float(getattr(position, "sl", 0.0) or 0.0)
            tighter = candidate > active if is_buy else active <= 0 or candidate < active
            market_side = candidate < bid if is_buy else candidate > ask
            if not tighter or not market_side:
                continue
            try:
                self.adapter.update_strategy_stop(position, candidate)
                self._log(
                    "TRAIL_STOP_UPDATED", position_id=int(position.ticket), side="LONG" if is_buy else "SHORT",
                    old_stop=active, new_stop=candidate, source_m5_close_utc=source_close.isoformat(),
                )
            except Exception as exc:
                self._log("TRAIL_STOP_UPDATE_FAILED", position_id=int(position.ticket), error_type=type(exc).__name__)

    def _weekend_close_if_due(self, at: datetime, tick_age_seconds: float) -> None:
        local = at.astimezone(self.tz)
        cutoff = wall_time.fromisoformat(self.config.weekend_close_local)
        if local.weekday() != 4 or local.timetz().replace(tzinfo=None) < cutoff:
            return
        if self.last_weekend_attempt == local.date():
            return
        positions = self._own_positions()
        if not positions:
            self.last_weekend_attempt = local.date()
            return
        now = datetime.now(UTC)
        if self.last_weekend_attempt_at and (now - self.last_weekend_attempt_at).total_seconds() < 30:
            return
        if tick_age_seconds > self.config.weekend_max_quote_age_minutes * 60:
            self.last_weekend_attempt_at = now
            self._log("WEEKEND_CLOSE_SKIPPED_STALE_QUOTE", quote_age_seconds=round(tick_age_seconds, 1))
            return
        self.last_weekend_attempt_at = now
        for position in positions:
            try:
                self.adapter.close_strategy_position(position)
                self._log("WEEKEND_ACTIVE_CLOSE", position_id=int(position.ticket), stop_counted=False)
            except Exception as exc:
                self._log("WEEKEND_ACTIVE_CLOSE_FAILED", position_id=int(position.ticket), error_type=type(exc).__name__)
        if not self._own_positions():
            self.last_weekend_attempt = local.date()

    def _consider_entry(self, at: datetime, bid: float, ask: float) -> None:
        local = at.astimezone(self.tz)
        key = local.replace(second=0, microsecond=0)
        if local.minute % self.config.check_minutes or local.second > 5 or key == self.last_entry_minute:
            return
        self.last_entry_minute = key
        self.last_entry_check_at = at.astimezone(UTC)
        self.last_entry_check_result = "CHECKING"
        if (
            (self.stop_new_entries_at is not None and datetime.now(UTC) >= self.stop_new_entries_at)
            or (self.stop_new_entries_at is not None and at >= self.stop_new_entries_at)
        ):
            self.last_entry_check_result = "ENTRY_CUTOFF"
            return
        if self.entry_lockout:
            self.last_entry_check_result = "ENTRY_LOCKOUT"
            return
        if local.weekday() >= 5 or (
            local.weekday() == 4 and local.timetz().replace(tzinfo=None) >= wall_time.fromisoformat(self.config.weekend_close_local)
        ):
            self.last_entry_check_result = "WEEKEND_BLOCKED"
            return
        signal = evaluate_entry_signal(
            self.frames["M1"], self.frames["M5"], self.frames["H1"],
            bid=bid, ask=ask, at=at, config=self.config, consumed_crosses=self.consumed_crosses,
        )
        if signal is None:
            self.last_entry_check_result = "NO_SIGNAL"
            self._log("ENTRY_CHECK_NO_SIGNAL", at_utc=at.isoformat())
            return
        cycle_id = cycle_id_for(at, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at)
        entries = self.entries_by_cycle.get(cycle_id, 0)
        if cycle_id in self.paused_cycles:
            self.last_entry_check_result = "CYCLE_PAUSED"
            self._log("ENTRY_REJECTED", reason="CYCLE_PAUSED", cycle_id=cycle_id, side=signal.side)
            return
        if entries >= self.config.max_entries_per_cycle:
            self.last_entry_check_result = "CYCLE_ENTRY_LIMIT"
            self._log("ENTRY_REJECTED", reason="CYCLE_ENTRY_LIMIT", cycle_id=cycle_id, side=signal.side)
            return

        before = {int(p.ticket) for p in self._own_positions()}
        try:
            result = self.adapter.open_strategy_market(
                self.config.symbol, "BUY" if signal.side == "LONG" else "SELL",
                self.config.entry_lots, signal.stop_loss,
            )
        except Exception as exc:
            if not bool(getattr(exc, "order_was_sent", True)):
                self.last_entry_check_result = "ORDER_REJECTED"
                self._log("ENTRY_REJECTED", reason="ORDER_PREFLIGHT_OR_BROKER_REJECTED", cycle_id=cycle_id,
                          side=signal.side, error_type=type(exc).__name__)
                return
            after = {int(p.ticket) for p in self._own_positions()}
            if after - before:
                self.entries_by_cycle[cycle_id] = entries + 1
                self.consumed_crosses.update((signal.m1_cross_id, signal.m5_cross_id))
                self.last_entry_check_result = "ENTRY_ACCEPTED_AFTER_AMBIGUOUS_RESPONSE"
                self._log("ENTRY_ACCEPTED_AFTER_AMBIGUOUS_RESPONSE", cycle_id=cycle_id, side=signal.side, lots=self.config.entry_lots)
            else:
                self.entry_lockout = True
                self.last_entry_check_result = "ENTRY_SUBMISSION_UNCERTAIN"
                self._log("ENTRY_SUBMISSION_UNCERTAIN_LOCKOUT", cycle_id=cycle_id, side=signal.side, error_type=type(exc).__name__)
            return

        self.last_entry_check_result = "ORDER_SUBMITTED_WAITING_FOR_FILL"
        deadline = time.monotonic() + 8.0
        opened = False
        while time.monotonic() < deadline:
            if {int(p.ticket) for p in self._own_positions()} - before:
                opened = True
                break
            time.sleep(0.2)
        if not opened:
            self.entry_lockout = True
            self.last_entry_check_result = "ENTRY_FILL_NOT_VISIBLE"
            self._log("ENTRY_FILL_NOT_VISIBLE_LOCKOUT", cycle_id=cycle_id, side=signal.side)
            return
        self.entries_by_cycle[cycle_id] = entries + 1
        self.consumed_crosses.update((signal.m1_cross_id, signal.m5_cross_id))
        self.last_entry_check_result = "ENTRY_FILLED"
        self._log(
            "ENTRY_FILLED", cycle_id=cycle_id, side=signal.side, lots=self.config.entry_lots,
            broker_filled_lots=float(result.volume),
            total_open_lots=sum(float(p.volume) for p in self._own_positions()),
            initial_stop=signal.stop_loss, midline=signal.midline,
        )

    def run(self) -> None:
        account = self.adapter.connect_demo(require_trading=True)
        mt5 = self.adapter.mt5
        if int(account.trade_mode) != int(mt5.ACCOUNT_TRADE_MODE_DEMO):
            raise RuntimeError("Demo-only runner refused a non-Demo account.")
        hedge_mode = int(getattr(mt5, "ACCOUNT_MARGIN_MODE_RETAIL_HEDGING", 2))
        if int(account.margin_mode) != hedge_mode:
            raise RuntimeError("Demo XAUUSD strategy requires hedging account mode.")
        info = mt5.symbol_info(self.config.symbol)
        if info is None:
            raise RuntimeError("Configured XAUUSD symbol is unavailable in MT5.")
        if any(int(p.magic) == self.adapter.ai_magic for p in (mt5.positions_get(symbol=self.config.symbol) or ())):
            raise RuntimeError("An earlier strategy position exists; refusing to start without its cycle state.")
        quote = self.adapter.get_quote(self.config.symbol)
        self._log(
            "RUN_STARTED", mode="DEMO", symbol=self.config.symbol, account_currency=str(account.currency),
            margin_mode="HEDGING", contract_size_oz=float(info.trade_contract_size),
            volume_min=float(info.volume_min), volume_step=float(info.volume_step),
            stop_new_entries_at=self.stop_new_entries_at.isoformat() if self.stop_new_entries_at else None,
            cycle_anchor_local=self.cycle_anchor_at.isoformat(),
            entry_lots=self.config.entry_lots, max_entries_per_cycle=self.config.max_entries_per_cycle,
            aggregate_lot_cap=None, heartbeat_interval_seconds=self.heartbeat_seconds,
            initial_quote_spread=round(quote["ask"] - quote["bid"], int(info.digits)),
        )
        self.last_quote_time_utc = datetime.fromtimestamp(int(quote["time"]), UTC)
        try:
            self._maybe_log_heartbeat(force=True)
            while True:
                now = datetime.now(UTC)
                try:
                    tick = mt5.symbol_info_tick(self.config.symbol)
                    if tick is None:
                        self._log("TICK_UNAVAILABLE")
                        self._maybe_log_heartbeat()
                        time.sleep(self.poll_seconds)
                        continue
                    tick_at = datetime.fromtimestamp(int(tick.time), UTC)
                    self.last_quote_time_utc = tick_at
                    age = abs((now - tick_at).total_seconds())
                    if age > 120:
                        if not self._stale_logged:
                            self._log("STALE_TICK_NO_ACTION", quote_age_seconds=round(age, 1))
                            self._stale_logged = True
                        if self.stop_new_entries_at is not None and now >= self.stop_new_entries_at and not self._own_positions():
                            self._log("RUN_FINISHED", reason="ENTRY_CUTOFF_AND_NO_OPEN_STRATEGY_POSITIONS")
                            return
                        self._maybe_log_heartbeat()
                        time.sleep(self.poll_seconds)
                        continue
                    self._stale_logged = False
                    local_tick = tick_at.astimezone(self.tz)
                    minute_key = tick_at.replace(second=0, microsecond=0)
                    if minute_key != self.last_data_minute:
                        self._refresh_frames(tick_at)
                        self.last_data_minute = minute_key
                    self._scan_stop_deals(now)
                    self._weekend_close_if_due(tick_at, age)
                    bid, ask = float(tick.bid), float(tick.ask)
                    if local_tick.minute in (0, 30) and local_tick.second <= 5:
                        self._trail_positions(tick_at, bid, ask)
                    self._consider_entry(tick_at, bid, ask)

                    positions = self._own_positions()
                    if self.stop_new_entries_at is not None and now >= self.stop_new_entries_at and not positions:
                        self._log("RUN_FINISHED", reason="ENTRY_CUTOFF_AND_NO_OPEN_STRATEGY_POSITIONS")
                        return
                except Exception as exc:
                    self.entry_lockout = True
                    self.last_runtime_error_type = type(exc).__name__
                    if self.last_entry_check_result == "CHECKING":
                        self.last_entry_check_result = "CHECK_ERROR"
                    self._log("RUNTIME_ERROR_NEW_ENTRIES_DISABLED", error_type=type(exc).__name__)
                self._maybe_log_heartbeat()
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self._log("RUN_INTERRUPTED", open_strategy_positions=len(self._own_positions()),
                      note="Broker-side SLs remain; no positions were force-closed.")
            raise
        finally:
            self.adapter.close()
