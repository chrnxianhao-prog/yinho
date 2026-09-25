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

from .cycles import cycle_window, resolve_cycle_anchor
from .indicators import h1_range_frame, macd_frame
from .models import BacktestConfig
from .rules import (
    cross_is_eligible,
    entry_block_reason,
    is_utc_blackout,
    record_stop_exit,
    risk_halt_reason,
    size_for_stop,
    tighten_stop,
)
from .sessions import resolve_next_session_close, session_close_due, within_entry_buffer


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
    """Evaluate only completed bars and the same cross-age rule as the backtest."""
    consumed = consumed_crosses if consumed_crosses is not None else set()
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

    def latest_unused(
        events: list[tuple[pd.Timestamp, str, str]], *, latest_only: bool
    ) -> str | None:
        eligible = [
            event for event in events
            if event[1] == side
            and event[2] not in consumed
            and cross_is_eligible(
                event[0].to_pydatetime(), decision.to_pydatetime(),
                lookback_minutes=5, require_latest=latest_only,
            )
            and start <= event[0] <= decision
        ]
        return eligible[-1][2] if eligible else None

    m1_cross_id = latest_unused(_crosses(m1, "M1", 1, config), latest_only=config.require_latest_m1_cross)
    m5_cross_id = latest_unused(_crosses(m5, "M5", 5, config), latest_only=False)
    if not m1_cross_id or not m5_cross_id:
        return None

    previous_m5 = m5.iloc[-1]
    stop = float(previous_m5["low"] if side == "LONG" else previous_m5["high"])
    if (side == "LONG" and stop >= bid) or (side == "SHORT" and stop <= ask):
        return None
    return EntrySignal(side, stop, midline, m1_cross_id, m5_cross_id)


class DemoStrategyRunner:
    """Demo-only XAUUSD runner; decisions share pure rules with historical simulation."""

    STATE_VERSION = 1

    def __init__(
        self,
        adapter: Any,
        config: BacktestConfig,
        *,
        stop_new_entries_at: datetime | None,
        cycle_anchor_at: datetime,
        log_path: str | Path,
        state_path: str | Path | None = None,
        poll_seconds: float = 1.0,
        heartbeat_seconds: float = 60.0,
        lockout_recovery_polls: int = 60,
        reset_lockout: bool = False,
        reset_risk_halt: bool = False,
        cycle_anchor_explicit: bool = False,
    ) -> None:
        if stop_new_entries_at is not None and stop_new_entries_at.tzinfo is None:
            raise ValueError("stop_new_entries_at must be timezone-aware")
        if cycle_anchor_at.tzinfo is None:
            raise ValueError("cycle_anchor_at must be timezone-aware")
        if poll_seconds <= 0 or poll_seconds > 10:
            raise ValueError("poll_seconds must be in (0, 10]")
        if not math.isfinite(heartbeat_seconds) or heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be a finite positive number")
        if lockout_recovery_polls < 1:
            raise ValueError("lockout_recovery_polls must be a positive integer")
        self.adapter = adapter
        self.config = config
        self.tz = ZoneInfo(config.timezone)
        self.stop_new_entries_at = stop_new_entries_at.astimezone(UTC) if stop_new_entries_at else None
        self.cycle_anchor_at = cycle_anchor_at.astimezone(self.tz)
        self.log_path = Path(log_path)
        self.state_path = Path(state_path) if state_path else Path("artifacts/xauusd_demo/state.json")
        self.poll_seconds = poll_seconds
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.lockout_recovery_polls = int(lockout_recovery_polls)
        self.reset_lockout_requested = reset_lockout
        self.reset_risk_halt_requested = reset_risk_halt
        self.cycle_anchor_explicit = cycle_anchor_explicit
        self._last_heartbeat_monotonic: float | None = None
        self.started_at = datetime.now(UTC)
        self.entries_by_cycle: dict[str, int] = {}
        self.losing_stops_by_cycle: dict[str, int] = {}
        self.all_stops_by_cycle: dict[str, int] = {}
        self.halted_cycles: set[str] = set()
        self.consumed_crosses: set[str] = set()
        self.counted_stop_positions: set[int] = set()
        self.last_processed_deal_ticket = 0
        self.last_history_scan = self.started_at - timedelta(days=30)
        self.last_data_minute: datetime | None = None
        self.last_entry_minute: datetime | None = None
        self.last_entry_check_at: datetime | None = None
        self.last_entry_check_result = "NOT_CHECKED"
        self.last_quote_time_utc: datetime | None = None
        self.last_runtime_error_type: str | None = None
        self.last_trail_minute: datetime | None = None
        self.frames: dict[str, pd.DataFrame] = {}
        self.entry_lockout = False
        self.lockout_recovery_success_polls = 0
        self.lockout_state_error: str | None = None
        self.risk_halt_day: date | None = None
        self.risk_halt_reason: str | None = None
        self.current_trading_day: date | None = None
        self.daily_realized_pnl = 0.0
        self.daily_pnl = 0.0
        self.day_start_equity = 0.0
        self.current_equity = 0.0
        self.equity_peak = 0.0
        self.current_drawdown_pct = 0.0
        self.next_session_close_utc: datetime | None = None
        self.session_schedule_source = "UNAVAILABLE"
        self._session_schedule_checked_monotonic: float | None = None
        self._session_schedule_refresh_seconds = 900.0
        self._last_session_close_attempt: datetime | None = None
        self._position_state: dict[str, dict[str, Any]] = {}
        self.state_loaded = False
        self._load_state()

    def _load_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if int(state.get("version", 0)) != self.STATE_VERSION:
                raise ValueError("unsupported state version")
            saved_anchor = state.get("cycle_anchor_local")
            if saved_anchor and not self.cycle_anchor_explicit:
                parsed = datetime.fromisoformat(saved_anchor)
                self.cycle_anchor_at = resolve_cycle_anchor(parsed, self.config.timezone)
            self.entries_by_cycle = {str(k): int(v) for k, v in state.get("entries_by_cycle", {}).items()}
            self.losing_stops_by_cycle = {str(k): int(v) for k, v in state.get("losing_stops_by_cycle", {}).items()}
            self.all_stops_by_cycle = {str(k): int(v) for k, v in state.get("all_stops_by_cycle", {}).items()}
            self.halted_cycles = set(map(str, state.get("halted_cycles", [])))
            self.consumed_crosses = set(map(str, state.get("consumed_crosses", [])))
            self.counted_stop_positions = set(map(int, state.get("counted_stop_positions", [])))
            self.last_processed_deal_ticket = int(state.get("last_processed_deal_ticket", 0))
            self.risk_halt_day = date.fromisoformat(state["risk_halt_day"]) if state.get("risk_halt_day") else None
            self.risk_halt_reason = state.get("risk_halt_reason")
            self.current_trading_day = date.fromisoformat(state["current_trading_day"]) if state.get("current_trading_day") else None
            self.daily_realized_pnl = float(state.get("daily_realized_pnl", 0.0))
            self.daily_pnl = float(state.get("daily_pnl", 0.0))
            self.day_start_equity = float(state.get("day_start_equity", 0.0))
            self.current_equity = float(state.get("current_equity", 0.0))
            self.equity_peak = float(state.get("equity_peak", 0.0))
            self.entry_lockout = bool(state.get("entry_lockout", False))
            self._position_state = {str(k): dict(v) for k, v in state.get("positions", {}).items()}
            saved_scan = state.get("last_history_scan_utc")
            if saved_scan:
                self.last_history_scan = datetime.fromisoformat(saved_scan).astimezone(UTC)
            self.state_loaded = True
        except Exception as exc:
            self.lockout_state_error = type(exc).__name__
            self.entry_lockout = True

    def _state_payload(self) -> dict[str, Any]:
        return {
            "version": self.STATE_VERSION,
            "updated_at_utc": datetime.now(UTC).isoformat(),
            "cycle_anchor_local": self.cycle_anchor_at.isoformat(),
            "entries_by_cycle": self.entries_by_cycle,
            "losing_stops_by_cycle": self.losing_stops_by_cycle,
            "all_stops_by_cycle": self.all_stops_by_cycle,
            "halted_cycles": sorted(self.halted_cycles),
            "consumed_crosses": sorted(self.consumed_crosses),
            "counted_stop_positions": sorted(self.counted_stop_positions),
            "last_processed_deal_ticket": self.last_processed_deal_ticket,
            "last_history_scan_utc": self.last_history_scan.isoformat(),
            "risk_halt_day": self.risk_halt_day.isoformat() if self.risk_halt_day else None,
            "risk_halt_reason": self.risk_halt_reason,
            "current_trading_day": self.current_trading_day.isoformat() if self.current_trading_day else None,
            "daily_realized_pnl": self.daily_realized_pnl,
            "daily_pnl": self.daily_pnl,
            "day_start_equity": self.day_start_equity,
            "current_equity": self.current_equity,
            "equity_peak": self.equity_peak,
            "entry_lockout": self.entry_lockout,
            "positions": self._position_state,
        }

    def _persist_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.state_path.with_name(self.state_path.name + ".tmp")
        temp.write_text(json.dumps(self._state_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.state_path)
        self.state_loaded = True

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

    def _set_entry_lockout(self, value: bool, *, reason: str) -> None:
        changed = self.entry_lockout != value
        self.entry_lockout = value
        self.lockout_recovery_success_polls = 0
        if changed:
            self._persist_state()
            self._log("ENTRY_LOCKOUT_CHANGED", entry_lockout=value, reason=reason)

    def _poll_succeeded(self) -> None:
        if not self.entry_lockout or self.lockout_state_error:
            return
        self.lockout_recovery_success_polls += 1
        if self.lockout_recovery_success_polls >= self.lockout_recovery_polls:
            self.entry_lockout = False
            self.lockout_recovery_success_polls = 0
            self._persist_state()
            self._log("ENTRY_LOCKOUT_AUTO_RECOVERED", successful_polls=self.lockout_recovery_polls)

    def _snapshot_owned_positions(self) -> list[Any]:
        positions = self._own_positions()
        current: dict[str, dict[str, Any]] = {}
        for position in positions:
            key = str(int(position.ticket))
            current[key] = {
                "ticket": int(position.ticket),
                "side": "LONG" if int(position.type) == int(self.adapter.mt5.POSITION_TYPE_BUY) else "SHORT",
                "volume": float(position.volume),
                "price_open": float(position.price_open),
                "stop_loss": float(getattr(position, "sl", 0.0) or 0.0),
            }
        if current != self._position_state:
            self._position_state = current
            self._persist_state()
        return positions

    def _maybe_log_heartbeat(self, *, force: bool = False) -> None:
        now_monotonic = time.monotonic()
        if (
            not force and self._last_heartbeat_monotonic is not None
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
        cycle_id = cycle_id_for(local_now, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at)
        quote_age = abs((now - self.last_quote_time_utc).total_seconds()) if self.last_quote_time_utc else None

        if self.entry_lockout:
            runner_state = "RUNNING_ENTRY_LOCKOUT"
        elif self.risk_halt_day == now.date():
            runner_state = "RISK_HALT_MANAGING_ONLY"
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
            last_quote_time_utc=self.last_quote_time_utc.isoformat() if self.last_quote_time_utc else None,
            last_entry_check_time_utc=self.last_entry_check_at.isoformat() if self.last_entry_check_at else None,
            last_entry_check_result=self.last_entry_check_result,
            strategy_open_positions=position_count,
            position_query_error_type=position_query_error,
            cycle_id=cycle_id,
            entries_opened_this_cycle=self.entries_by_cycle.get(cycle_id, 0),
            losing_stops_this_cycle=self.losing_stops_by_cycle.get(cycle_id, 0),
            all_stops_this_cycle=self.all_stops_by_cycle.get(cycle_id, 0),
            daily_realized_pnl=self.daily_realized_pnl,
            daily_pnl_realized_plus_floating=self.daily_pnl,
            current_drawdown_pct=self.current_drawdown_pct,
            risk_halt=self.risk_halt_day == now.date(),
            risk_halt_reason=self.risk_halt_reason,
            entry_lockout=self.entry_lockout,
            lockout_success_polls=self.lockout_recovery_success_polls,
            lockout_recovery_polls_required=self.lockout_recovery_polls,
            next_session_close_utc=(self.next_session_close_utc.isoformat() if self.next_session_close_utc else None),
            session_schedule_source=self.session_schedule_source,
            last_runtime_error_type=self.last_runtime_error_type,
        )
        self._last_heartbeat_monotonic = now_monotonic

    def _own_positions(self) -> list[Any]:
        mt5 = self.adapter.mt5
        positions = mt5.positions_get(symbol=self.config.symbol)
        if positions is None:
            raise RuntimeError("MT5 position query failed")
        return [position for position in positions if int(position.magic) == self.adapter.ai_magic]

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
        ordered = sorted(deals, key=lambda deal: (int(getattr(deal, "time_msc", 0) or 0), int(deal.ticket)))
        for deal in ordered:
            ticket = int(deal.ticket)
            if ticket <= self.last_processed_deal_ticket:
                continue
            self.last_processed_deal_ticket = ticket
            is_strategy_stop = (
                int(getattr(deal, "magic", 0)) == self.adapter.ai_magic
                and str(getattr(deal, "symbol", "")) == self.config.symbol
                and int(getattr(deal, "reason", -1)) == sl_reason
                and int(getattr(deal, "entry", -1)) in out_entries
            )
            if not is_strategy_stop:
                self._persist_state()
                continue
            position_id = int(getattr(deal, "position_id", 0))
            if position_id in self.counted_stop_positions:
                self._persist_state()
                continue
            self.counted_stop_positions.add(position_id)
            deal_time_msc = int(getattr(deal, "time_msc", 0) or 0)
            at = datetime.fromtimestamp(deal_time_msc / 1000.0, UTC) if deal_time_msc else datetime.fromtimestamp(int(deal.time), UTC)
            net_pnl = realized_deal_net_pnl(deal)
            cycle_id = cycle_id_for(at, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at)
            following_cycle = next_cycle_id(at, self.config.timezone, self.config.cycle_hours, self.cycle_anchor_at)
            outcome = record_stop_exit(
                cycle_id=cycle_id,
                next_cycle_id=following_cycle,
                net_pnl=net_pnl,
                losing_stops_by_cycle=self.losing_stops_by_cycle,
                all_stops_by_cycle=self.all_stops_by_cycle,
                halted_cycles=self.halted_cycles,
                max_losing_stops_per_cycle=self.config.max_losing_stops_per_cycle,
                stop_rule_scope=self.config.stop_rule_scope,
                count_profitable_stops=self.config.count_profitable_stops,
            )
            self._log(
                "STOP_EXIT_COUNTED",
                position_id=position_id,
                cycle_id=cycle_id,
                losing_stop_count=outcome.losing_stops,
                all_stop_count=outcome.all_stops,
                count_toward_limit=outcome.counted_toward_limit,
                realized_net_pnl=net_pnl,
                stop_rule_scope=self.config.stop_rule_scope,
            )
            for halted in outcome.halted_cycles:
                self._log(
                    "CYCLE_PAUSED",
                    stopped_cycle=cycle_id,
                    paused_cycle=halted,
                    losing_stop_count=outcome.losing_stops,
                    all_stop_count=outcome.all_stops,
                    stop_rule_scope=self.config.stop_rule_scope,
                )
            self._persist_state()

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
            side = "LONG" if is_buy else "SHORT"
            candidate = float(previous["low"] if is_buy else previous["high"])
            active = float(getattr(position, "sl", 0.0) or 0.0)
            tightened = tighten_stop(side, active, candidate)
            market_side = tightened < bid if is_buy else tightened > ask
            if tightened == active or not market_side:
                continue
            try:
                self.adapter.update_strategy_stop(position, tightened)
                self._log(
                    "TRAIL_STOP_UPDATED",
                    position_id=int(position.ticket), side=side,
                    old_stop=active, new_stop=tightened,
                    source_m5_close_utc=source_close.isoformat(),
                )
                self._snapshot_owned_positions()
            except Exception as exc:
                self._log("TRAIL_STOP_UPDATE_FAILED", position_id=int(position.ticket), error_type=type(exc).__name__)

    def _refresh_next_session_close(self, at: datetime, info: Any | None = None, *, force: bool = False) -> None:
        mono = time.monotonic()
        stale = self._session_schedule_checked_monotonic is None or (
            mono - self._session_schedule_checked_monotonic >= self._session_schedule_refresh_seconds
        )
        if not force and not stale and self.next_session_close_utc and self.next_session_close_utc > at:
            return
        info = info or self.adapter.mt5.symbol_info(self.config.symbol)
        if info is None:
            self.next_session_close_utc, self.session_schedule_source = None, "UNAVAILABLE"
        else:
            self.next_session_close_utc, self.session_schedule_source = resolve_next_session_close(
                mt5=self.adapter.mt5,
                symbol=self.config.symbol,
                info=info,
                now_utc=at.astimezone(UTC),
            )
        self._session_schedule_checked_monotonic = mono
        self._log(
            "SESSION_CLOSE_SCHEDULE",
            next_session_close_utc=self.next_session_close_utc.isoformat() if self.next_session_close_utc else None,
            source=self.session_schedule_source,
            close_buffer_minutes=self.config.session_close_buffer_minutes,
        )

    def _session_close_if_due(self, at: datetime, tick_age_seconds: float) -> None:
        if self.config.allow_hold_through_session_break:
            return
        if not session_close_due(at, self.next_session_close_utc, self.config.session_close_buffer_minutes):
            return
        if self._last_session_close_attempt and (at - self._last_session_close_attempt).total_seconds() < 10:
            return
        positions = self._own_positions()
        if not positions:
            return
        if tick_age_seconds > 120:
            self._last_session_close_attempt = at
            self._log(
                "SESSION_CLOSE_SKIPPED_STALE_QUOTE",
                quote_age_seconds=round(tick_age_seconds, 1),
                next_session_close_utc=self.next_session_close_utc.isoformat() if self.next_session_close_utc else None,
                schedule_source=self.session_schedule_source,
            )
            return
        self._last_session_close_attempt = at
        for position in positions:
            try:
                self.adapter.close_strategy_position(position)
                self._log(
                    "SESSION_CLOSE",
                    position_id=int(position.ticket),
                    side="LONG" if int(position.type) == int(self.adapter.mt5.POSITION_TYPE_BUY) else "SHORT",
                    stop_counted=False,
                    scheduled_close_utc=self.next_session_close_utc.isoformat() if self.next_session_close_utc else None,
                    schedule_source=self.session_schedule_source,
                )
            except Exception as exc:
                self._log("SESSION_CLOSE_FAILED", position_id=int(position.ticket), error_type=type(exc).__name__)
        self._snapshot_owned_positions()

    def _update_account_risk(self, now: datetime) -> None:
        mt5 = self.adapter.mt5
        account = mt5.account_info()
        if account is None:
            raise RuntimeError("MT5 account risk snapshot is unavailable")
        day = now.astimezone(UTC).date()
        start = datetime.combine(day, wall_time.min, tzinfo=UTC)
        deals = mt5.history_deals_get(start, now.astimezone(UTC) + timedelta(seconds=1))
        if deals is None:
            raise RuntimeError("MT5 daily deal history is unavailable for account risk checks")
        realized_today = sum(realized_deal_net_pnl(deal) for deal in deals)
        equity = float(account.equity)
        balance = float(account.balance)
        floating = equity - balance
        day_start_estimate = balance - realized_today
        if self.current_trading_day != day:
            if self.risk_halt_day is not None and self.risk_halt_day < day:
                prior = self.risk_halt_day.isoformat()
                self.risk_halt_day = None
                self.risk_halt_reason = None
                self._log("RISK_HALT_RESET", previous_halt_day=prior, server_trading_day=day.isoformat())
            self.current_trading_day = day
            self.day_start_equity = day_start_estimate
        elif self.day_start_equity <= 0:
            self.day_start_equity = day_start_estimate

        self.daily_realized_pnl = realized_today
        self.daily_pnl = realized_today + floating
        self.current_equity = equity
        prior_peak = self.equity_peak
        self.equity_peak = max(self.equity_peak, equity)
        self.current_drawdown_pct = max(0.0, (self.equity_peak - equity) / self.equity_peak) if self.equity_peak else 0.0
        reason = risk_halt_reason(
            equity=equity,
            equity_peak=self.equity_peak,
            daily_pnl=self.daily_pnl,
            day_start_equity=self.day_start_equity,
            max_daily_loss_pct=self.config.max_daily_loss_pct,
            max_drawdown_pct=self.config.max_drawdown_pct,
        )
        changed = prior_peak != self.equity_peak
        if reason and self.risk_halt_day != day:
            self.risk_halt_day = day
            self.risk_halt_reason = reason
            self._log(
                "RISK_HALT",
                reason=reason,
                server_trading_day=day.isoformat(),
                daily_realized_pnl=realized_today,
                floating_pnl=floating,
                daily_pnl=realized_today + floating,
                equity=equity,
                equity_peak=self.equity_peak,
                current_drawdown_pct=self.current_drawdown_pct,
            )
            changed = True
        if changed:
            self._persist_state()

    def _consider_entry(self, at: datetime, bid: float, ask: float) -> None:
        local = at.astimezone(self.tz)
        key = local.replace(second=0, microsecond=0)
        if local.minute % self.config.check_minutes or local.second > 5 or key == self.last_entry_minute:
            return
        self.last_entry_minute = key
        self.last_entry_check_at = at.astimezone(UTC)
        self.last_entry_check_result = "CHECKING"
        if (self.stop_new_entries_at is not None and (datetime.now(UTC) >= self.stop_new_entries_at or at >= self.stop_new_entries_at)):
            self.last_entry_check_result = "ENTRY_CUTOFF"
            return
        if self.lockout_state_error:
            self.last_entry_check_result = "STATE_UNREADABLE"
            return
        if self.entry_lockout:
            self.last_entry_check_result = "ENTRY_LOCKOUT"
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
        info = self.adapter.mt5.symbol_info(self.config.symbol)
        if info is None:
            self.last_entry_check_result = "SYMBOL_INFO_UNAVAILABLE"
            raise RuntimeError("MT5 symbol specification is unavailable during entry sizing")
        contract_size = float(getattr(info, "trade_contract_size", 0.0) or 0.0)
        entry_price = (
            ask + self.config.slippage_usd_per_side
            if signal.side == "LONG"
            else bid - self.config.slippage_usd_per_side
        )
        stop_distance = abs(entry_price - signal.stop_loss)
        min_distance = max(
            self.config.min_stop_distance_usd,
            float(getattr(info, "trade_stops_level", 0) or 0) * float(getattr(info, "point", 0.0) or 0.0),
        )
        market_distance = bid - signal.stop_loss if signal.side == "LONG" else signal.stop_loss - ask
        if market_distance < min_distance - 1e-12:
            self.last_entry_check_result = "MIN_STOP_DISTANCE"
            self._log(
                "ENTRY_REJECTED", reason="MIN_STOP_DISTANCE", cycle_id=cycle_id,
                side=signal.side, stop_distance_usd=market_distance,
                min_stop_distance_usd=min_distance,
                m1_cross_id=signal.m1_cross_id, m5_cross_id=signal.m5_cross_id,
                crosses_consumed=False,
            )
            return

        sizing = size_for_stop(
            equity=self.current_equity,
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            fixed_fallback_lots=self.config.entry_lots,
            stop_distance_usd=stop_distance,
            contract_size_oz=contract_size,
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            max_stop_distance_usd=self.config.max_stop_distance_usd,
        )
        if sizing.lots is None:
            self.last_entry_check_result = "ENTRY_SKIPPED_RISK"
            self._log(
                "ENTRY_SKIPPED_RISK",
                reason=sizing.reason,
                side=signal.side,
                stop_distance_usd=sizing.stop_distance_usd,
                max_stop_distance_usd=self.config.max_stop_distance_usd,
                current_equity=self.current_equity,
                risk_budget_usd=sizing.risk_budget_usd,
                volume_min=float(info.volume_min), volume_step=float(info.volume_step),
                m1_cross_id=signal.m1_cross_id, m5_cross_id=signal.m5_cross_id,
                crosses_consumed=False,
            )
            return

        positions = self._own_positions()
        aggregate_lots = sum(float(position.volume) for position in positions)
        now_utc = at.astimezone(UTC)
        if self.next_session_close_utc is None:
            before_close = True
        else:
            before_close = within_entry_buffer(
                now_utc,
                self.next_session_close_utc,
                self.config.no_new_entry_before_close_minutes,
            ) or now_utc >= self.next_session_close_utc
        blocked = entry_block_reason(
            cycle_id=cycle_id,
            entries_this_cycle=self.entries_by_cycle.get(cycle_id, 0),
            max_entries_per_cycle=self.config.max_entries_per_cycle,
            halted_cycles=self.halted_cycles,
            risk_halted=self.risk_halt_day == now_utc.date(),
            entry_lockout=self.entry_lockout,
            in_blackout=is_utc_blackout(now_utc, self.config.blackout_windows),
            before_session_close=before_close,
            aggregate_lots=aggregate_lots,
            requested_lots=sizing.lots,
            max_total_lots=self.config.max_total_lots,
        )
        if blocked:
            self.last_entry_check_result = blocked
            self._log(
                "ENTRY_REJECTED", reason=blocked, cycle_id=cycle_id, side=signal.side,
                lots=sizing.lots,
                next_session_close_utc=self.next_session_close_utc.isoformat() if self.next_session_close_utc else None,
                schedule_source=self.session_schedule_source,
                losing_stops_this_cycle=self.losing_stops_by_cycle.get(cycle_id, 0),
                all_stops_this_cycle=self.all_stops_by_cycle.get(cycle_id, 0),
                crosses_consumed=False,
            )
            return

        # When MT5 can calculate margin, reject insufficient free margin before submission;
        # the adapter's mandatory order_check remains the final preflight.
        margin_fn = getattr(self.adapter.mt5, "order_calc_margin", None)
        if callable(margin_fn):
            order_type = self.adapter.mt5.ORDER_TYPE_BUY if signal.side == "LONG" else self.adapter.mt5.ORDER_TYPE_SELL
            required_margin = margin_fn(order_type, self.config.symbol, sizing.lots, entry_price)
            account = self.adapter.mt5.account_info()
            if required_margin is not None and account is not None and float(required_margin) > float(account.margin_free):
                self.last_entry_check_result = "INSUFFICIENT_MARGIN"
                self._log(
                    "ENTRY_REJECTED", reason="INSUFFICIENT_MARGIN", cycle_id=cycle_id,
                    required_margin=float(required_margin), free_margin=float(account.margin_free),
                    crosses_consumed=False,
                )
                return

        before = {int(position.ticket) for position in positions}
        try:
            result = self.adapter.open_strategy_market(
                self.config.symbol,
                "BUY" if signal.side == "LONG" else "SELL",
                sizing.lots,
                signal.stop_loss,
            )
        except Exception as exc:
            if not bool(getattr(exc, "order_was_sent", True)):
                self.last_entry_check_result = "ORDER_REJECTED"
                self._log(
                    "ENTRY_REJECTED", reason="ORDER_PREFLIGHT_OR_BROKER_REJECTED",
                    cycle_id=cycle_id, side=signal.side, lots=sizing.lots,
                    error_type=type(exc).__name__, crosses_consumed=False,
                )
                return
            after = {int(position.ticket) for position in self._own_positions()}
            if after - before:
                self.entries_by_cycle[cycle_id] = self.entries_by_cycle.get(cycle_id, 0) + 1
                self.consumed_crosses.update((signal.m1_cross_id, signal.m5_cross_id))
                self.last_entry_check_result = "ENTRY_ACCEPTED_AFTER_AMBIGUOUS_RESPONSE"
                self._snapshot_owned_positions()
                self._persist_state()
                self._log("ENTRY_ACCEPTED_AFTER_AMBIGUOUS_RESPONSE", cycle_id=cycle_id, side=signal.side, lots=sizing.lots)
            else:
                self._set_entry_lockout(True, reason="ORDER_SUBMISSION_UNCERTAIN")
                self.last_entry_check_result = "ENTRY_SUBMISSION_UNCERTAIN"
                self._log("ENTRY_SUBMISSION_UNCERTAIN_LOCKOUT", cycle_id=cycle_id, side=signal.side, error_type=type(exc).__name__)
            return

        self.last_entry_check_result = "ORDER_SUBMITTED_WAITING_FOR_FILL"
        deadline = time.monotonic() + 8.0
        opened = False
        while time.monotonic() < deadline:
            if {int(position.ticket) for position in self._own_positions()} - before:
                opened = True
                break
            time.sleep(0.2)
        if not opened:
            self._set_entry_lockout(True, reason="ENTRY_FILL_NOT_VISIBLE")
            self.last_entry_check_result = "ENTRY_FILL_NOT_VISIBLE"
            self._log("ENTRY_FILL_NOT_VISIBLE_LOCKOUT", cycle_id=cycle_id, side=signal.side)
            return
        self.entries_by_cycle[cycle_id] = self.entries_by_cycle.get(cycle_id, 0) + 1
        self.consumed_crosses.update((signal.m1_cross_id, signal.m5_cross_id))
        self.last_entry_check_result = "ENTRY_FILLED"
        self._snapshot_owned_positions()
        self._persist_state()
        self._log(
            "ENTRY_FILLED",
            cycle_id=cycle_id,
            side=signal.side,
            lots=sizing.lots,
            risk_budget_usd=sizing.risk_budget_usd,
            stop_distance_usd=stop_distance,
            broker_filled_lots=float(result.volume),
            total_open_lots=sum(float(position.volume) for position in self._own_positions()),
            initial_stop=signal.stop_loss,
            midline=signal.midline,
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
        existing = self._own_positions()
        if existing and not self.state_loaded:
            raise RuntimeError(
                "Strategy-owned positions exist but artifacts/xauusd_demo/state.json is missing or unreadable. "
                "Keep the broker-side SLs in place, stop this runner, manually record each ticket/side/volume/SL "
                "and cycle counts, restore a valid state file, then restart; do not open additional positions."
            )
        if self.lockout_state_error:
            self._log("STATE_FILE_UNREADABLE_NEW_ENTRIES_LOCKED", error_type=self.lockout_state_error)
        if existing:
            unprotected = [int(p.ticket) for p in existing if float(getattr(p, "sl", 0.0) or 0.0) <= 0]
            if unprotected:
                raise RuntimeError(f"Cannot adopt positions without broker-side SL; tickets={unprotected}")
            self._log("EXISTING_POSITIONS_ADOPTED", tickets=[int(p.ticket) for p in existing], state_file=str(self.state_path))
        if self.reset_lockout_requested:
            self.entry_lockout = False
            self.lockout_recovery_success_polls = 0
            self._persist_state()
            self._log("ENTRY_LOCKOUT_MANUALLY_RESET")
        if self.reset_risk_halt_requested:
            previous_halt_day = self.risk_halt_day.isoformat() if self.risk_halt_day else None
            self.risk_halt_day = None
            self.risk_halt_reason = None
            self._persist_state()
            self._log("RISK_HALT_MANUALLY_RESET", previous_halt_day=previous_halt_day)
        quote = self.adapter.get_quote(self.config.symbol)
        self._refresh_next_session_close(datetime.fromtimestamp(int(quote["time"]), UTC), info, force=True)
        self._update_account_risk(datetime.now(UTC))
        self._snapshot_owned_positions()
        self._persist_state()
        self._log(
            "RUN_STARTED",
            mode="DEMO",
            symbol=self.config.symbol,
            account_currency=str(account.currency),
            margin_mode="HEDGING",
            contract_size_oz=float(info.trade_contract_size),
            volume_min=float(info.volume_min),
            volume_step=float(info.volume_step),
            stop_new_entries_at=self.stop_new_entries_at.isoformat() if self.stop_new_entries_at else None,
            cycle_anchor_local=self.cycle_anchor_at.isoformat(),
            risk_per_trade_pct=self.config.risk_per_trade_pct,
            max_stop_distance_usd=self.config.max_stop_distance_usd,
            max_entries_per_cycle=self.config.max_entries_per_cycle,
            max_losing_stops_per_cycle=self.config.max_losing_stops_per_cycle,
            stop_rule_scope=self.config.stop_rule_scope,
            state_file=str(self.state_path),
            aggregate_lot_cap=self.config.max_total_lots,
            heartbeat_interval_seconds=self.heartbeat_seconds,
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
                    tick_seconds = int(getattr(tick, "time", 0) or 0)
                    tick_msc = int(getattr(tick, "time_msc", 0) or 0)
                    tick_at = datetime.fromtimestamp(tick_msc / 1000.0 if tick_msc else tick_seconds, UTC)
                    self.last_quote_time_utc = tick_at
                    age = abs((now - tick_at).total_seconds())
                    if age > 120:
                        if not getattr(self, "_stale_logged", False):
                            self._log("STALE_TICK_NO_ACTION", quote_age_seconds=round(age, 1))
                            self._stale_logged = True
                        self._maybe_log_heartbeat()
                        time.sleep(self.poll_seconds)
                        continue
                    self._stale_logged = False
                    minute_key = tick_at.replace(second=0, microsecond=0)
                    if minute_key != self.last_data_minute:
                        self._refresh_frames(tick_at)
                        self.last_data_minute = minute_key
                    self._scan_stop_deals(now)
                    self._update_account_risk(now)
                    self._refresh_next_session_close(tick_at, info)
                    bid, ask = float(tick.bid), float(tick.ask)
                    if tick_at.astimezone(self.tz).minute in (0, 30) and tick_at.second <= 5:
                        self._trail_positions(tick_at, bid, ask)
                    self._session_close_if_due(tick_at, age)
                    self._consider_entry(tick_at, bid, ask)
                    positions = self._snapshot_owned_positions()
                    if self.stop_new_entries_at is not None and now >= self.stop_new_entries_at and not positions:
                        self._log("RUN_FINISHED", reason="ENTRY_CUTOFF_AND_NO_OPEN_STRATEGY_POSITIONS")
                        return
                    self.last_runtime_error_type = None
                    self._poll_succeeded()
                except Exception as exc:
                    self.lockout_recovery_success_polls = 0
                    self.entry_lockout = True
                    self.last_runtime_error_type = type(exc).__name__
                    if self.last_entry_check_result == "CHECKING":
                        self.last_entry_check_result = "CHECK_ERROR"
                    self._persist_state()
                    self._log("RUNTIME_ERROR_NEW_ENTRIES_DISABLED", error_type=type(exc).__name__)
                self._maybe_log_heartbeat()
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            self._log(
                "RUN_INTERRUPTED",
                open_strategy_positions=len(self._own_positions()),
                note="Broker-side SLs remain; no positions were force-closed.",
            )
            raise
        finally:
            self.adapter.close()
