from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from statistics import median
from typing import Any, Iterable

import pandas as pd


UTC = timezone.utc


@dataclass(frozen=True)
class SessionBreak:
    last_bar_index: int
    break_start_utc: pd.Timestamp
    last_executable_close_utc: pd.Timestamp


def infer_session_breaks(
    timestamps_utc: Iterable[pd.Timestamp], *, threshold_minutes: int = 30
) -> dict[int, SessionBreak]:
    """Infer session breaks from M1 starts; map each break to its last observed bar."""
    stamps = list(pd.to_datetime(list(timestamps_utc), utc=True))
    breaks: dict[int, SessionBreak] = {}
    threshold = pd.Timedelta(minutes=threshold_minutes)
    for index in range(len(stamps) - 1):
        if stamps[index + 1] - stamps[index] > threshold:
            breaks[index] = SessionBreak(
                last_bar_index=index,
                break_start_utc=pd.Timestamp(stamps[index + 1]),
                last_executable_close_utc=pd.Timestamp(stamps[index] + pd.Timedelta(minutes=1)),
            )
    return breaks


def within_entry_buffer(
    decision_at_utc: pd.Timestamp, next_close_utc: pd.Timestamp | None, buffer_minutes: int
) -> bool:
    if next_close_utc is None:
        return False
    decision = pd.Timestamp(decision_at_utc)
    close = pd.Timestamp(next_close_utc)
    if decision.tzinfo is None or close.tzinfo is None:
        raise ValueError("session decision and close timestamps must be timezone-aware")
    return decision < close and close - decision <= pd.Timedelta(minutes=buffer_minutes)


def session_close_due(
    now_utc: datetime, next_close_utc: datetime | None, close_buffer_minutes: int
) -> bool:
    if next_close_utc is None:
        return False
    now = now_utc.astimezone(UTC)
    close = next_close_utc.astimezone(UTC)
    return close - timedelta(minutes=close_buffer_minutes) <= now < close


def _get_field(value: Any, names: tuple[str, ...]) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        # MT5 返回的结构化 numpy 行通过键访问字段，而非对象属性。
        try:
            indexed = value[name]
        except (KeyError, IndexError, TypeError, ValueError):
            pass
        else:
            if indexed is not None:
                return indexed
        candidate = getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def _seconds_after_midnight(value: Any) -> int | None:
    if isinstance(value, datetime):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, time):
        return value.hour * 3600 + value.minute * 60 + value.second
    if isinstance(value, str):
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            return None
        return parsed.hour * 3600 + parsed.minute * 60 + parsed.second
    if isinstance(value, (int, float)):
        seconds = int(value)
        return seconds if 0 <= seconds < 86400 else None
    return None


def _normalize_weekday(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        names = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
        return names.get(str(value).strip().lower()[:3])
    # Explicit mappings in symbol_info extensions use Python's Monday=0 convention.
    return number if 0 <= number <= 6 else None


def trade_session_end_times(info: Any, mt5: Any | None = None, symbol: str | None = None) -> dict[int, list[int]]:
    """Read explicit schedule extensions when a terminal/provider exposes them.

    Standard MetaTrader5 Python ``symbol_info`` does not promise weekly time
    windows. Never mistake its session_open/session_close *prices* for hours.
    """
    result: dict[int, list[int]] = {}
    raw = _get_field(info, ("trade_sessions_utc", "session_trade_windows", "trade_sessions"))
    if isinstance(raw, dict):
        for weekday_raw, windows in raw.items():
            weekday = _normalize_weekday(weekday_raw)
            if weekday is None:
                continue
            for window in windows if isinstance(windows, (list, tuple)) else (windows,):
                end = _get_field(window, ("end_utc", "end", "to", "close_time"))
                if end is None and isinstance(window, (tuple, list)) and len(window) >= 2:
                    end = window[1]
                seconds = _seconds_after_midnight(end)
                if seconds is not None:
                    result.setdefault(weekday, []).append(seconds)

    session_fn = getattr(mt5, "symbol_info_session_trade", None) if mt5 is not None else None
    if not result and callable(session_fn) and symbol:
        # This is optional: it is not exposed by every official Python package build.
        for weekday in range(7):
            mt5_weekday = (weekday + 1) % 7
            for session_index in range(16):
                try:
                    session = session_fn(symbol, mt5_weekday, session_index)
                except Exception:
                    break
                if session is None:
                    break
                end = _get_field(session, ("to", "end", "end_time"))
                if end is None and isinstance(session, (tuple, list)) and len(session) >= 2:
                    end = session[1]
                seconds = _seconds_after_midnight(end)
                if seconds is not None:
                    result.setdefault(weekday, []).append(seconds)
    return result


def next_close_from_weekly_schedule(
    now_utc: datetime, weekday_end_seconds: dict[int, list[int]]
) -> datetime | None:
    now = now_utc.astimezone(UTC)
    candidates: list[datetime] = []
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        for seconds in weekday_end_seconds.get(day.weekday(), []):
            candidate = datetime.combine(day, time.min, tzinfo=UTC) + timedelta(seconds=seconds)
            if candidate > now:
                candidates.append(candidate)
    return min(candidates) if candidates else None


def _tick_time_utc(tick: Any) -> datetime | None:
    time_msc = _get_field(tick, ("time_msc",))
    seconds = _get_field(tick, ("time",))
    try:
        if time_msc:
            return datetime.fromtimestamp(float(time_msc) / 1000.0, UTC)
        if seconds:
            return datetime.fromtimestamp(float(seconds), UTC)
    except (TypeError, ValueError, OSError):
        return None
    return None


def recent_daily_last_ticks(
    mt5: Any, symbol: str, now_utc: datetime, *, trading_days: int = 20, lookback_days: int = 40
) -> list[datetime]:
    """读取最近休市缺口前的最后 tick，避免把同日重开后的 tick 当作收盘。"""
    copy_rates = getattr(mt5, "copy_rates_range", None)
    copy_ticks = getattr(mt5, "copy_ticks_range", None)
    if not callable(copy_rates) or not callable(copy_ticks) or trading_days <= 0:
        return []
    now = now_utc.astimezone(UTC)
    try:
        bars = copy_rates(
            symbol, int(getattr(mt5, "TIMEFRAME_M1", 1)),
            now - timedelta(days=lookback_days), now,
        )
    except Exception:
        return []
    if bars is None or len(bars) < 2:
        return []

    bar_times: list[datetime] = []
    for bar in bars:
        raw = _get_field(bar, ("time",))
        try:
            stamp = datetime.fromtimestamp(int(raw), UTC)
        except (TypeError, ValueError, OSError):
            continue
        if stamp <= now:
            bar_times.append(stamp)
    bar_times = sorted(set(bar_times))

    # 每个 UTC 交易日取最长的 >30 分钟缺口；同日 22:00 重开后的晚间 tick
    # 不能覆盖 20:57 前的真实休市边界。周末缺口同样归于周五。
    breaks_by_day: dict[date, tuple[datetime, timedelta]] = {}
    for previous, following in zip(bar_times, bar_times[1:]):
        gap = following - previous
        if gap <= timedelta(minutes=30):
            continue
        day = previous.date()
        if day not in breaks_by_day or gap > breaks_by_day[day][1]:
            breaks_by_day[day] = (previous, gap)

    mode = int(getattr(mt5, "COPY_TICKS_ALL", 0))
    closes: list[datetime] = []
    for last_bar_start, _ in sorted(breaks_by_day.values(), reverse=True):
        bar_end = last_bar_start + timedelta(minutes=1)
        try:
            ticks = copy_ticks(symbol, last_bar_start, bar_end, mode)
        except Exception:
            continue
        if ticks is None or len(ticks) == 0:
            continue
        stamps = [
            stamp for tick in ticks
            if (stamp := _tick_time_utc(tick)) is not None
            and last_bar_start <= stamp < bar_end
        ]
        if stamps:
            closes.append(max(stamps))
            if len(closes) >= trading_days:
                break
    return sorted(closes)


def next_close_from_observed_ticks(now_utc: datetime, last_ticks: list[datetime]) -> datetime | None:
    """Estimate the next UTC close from weekday-specific historical last ticks."""
    if not last_ticks:
        return None
    by_weekday: dict[int, list[int]] = {}
    for stamp in last_ticks:
        utc = stamp.astimezone(UTC)
        seconds = utc.hour * 3600 + utc.minute * 60 + utc.second
        by_weekday.setdefault(utc.weekday(), []).append(seconds)
    medians = {day: int(median(values)) for day, values in by_weekday.items()}
    now = now_utc.astimezone(UTC)
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if day.weekday() not in medians:
            continue
        candidate = datetime.combine(day, time.min, tzinfo=UTC) + timedelta(seconds=medians[day.weekday()])
        if candidate > now:
            return candidate
    return None


def resolve_next_session_close(
    *, mt5: Any, symbol: str, info: Any, now_utc: datetime
) -> tuple[datetime | None, str]:
    """Prefer explicit broker session windows, otherwise infer from 20 close ticks."""
    schedule = trade_session_end_times(info, mt5, symbol)
    if schedule:
        close = next_close_from_weekly_schedule(now_utc, schedule)
        if close is not None:
            return close, "SYMBOL_INFO_TRADE_SESSIONS"
    ticks = recent_daily_last_ticks(mt5, symbol, now_utc)
    close = next_close_from_observed_ticks(now_utc, ticks)
    if close is not None:
        return close, "OBSERVED_LAST_TICK_20_TRADING_DAYS"
    return None, "UNAVAILABLE"
