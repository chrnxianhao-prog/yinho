from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from research.xauusd_trailing.demo_runner import (
    DemoStrategyRunner,
    cycle_id_for,
    evaluate_entry_signal,
    next_cycle_id,
    next_local_time,
    realized_deal_net_pnl,
    rates_to_frame,
)
from research.xauusd_trailing.models import BacktestConfig


class DemoRunnerClockTests(unittest.TestCase):
    def test_cycle_rollover_keeps_open_positions_out_of_cycle_identity(self) -> None:
        tz = ZoneInfo("America/Mexico_City")
        at = datetime(2026, 9, 25, 3, 15, tzinfo=timezone.utc)  # Mexico City 21:15
        anchor = datetime(2026, 9, 25, 9, 0, tzinfo=tz)
        self.assertEqual(cycle_id_for(at, "America/Mexico_City", anchor=anchor), "2026-09-24T18:00")
        self.assertEqual(next_cycle_id(at, "America/Mexico_City", anchor=anchor), "2026-09-24T23:00")

    def test_nine_am_is_a_five_hour_cycle_boundary(self) -> None:
        tz = ZoneInfo("America/Mexico_City")
        at = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
        anchor = datetime(2026, 9, 25, 9, 0, tzinfo=tz)
        self.assertEqual(cycle_id_for(at, "America/Mexico_City", anchor=anchor), "2026-09-25T09:00")
        self.assertEqual(next_cycle_id(at, "America/Mexico_City", anchor=anchor), "2026-09-25T14:00")

    def test_next_local_nine_is_next_morning(self) -> None:
        at = datetime(2026, 9, 25, 2, 0, tzinfo=timezone.utc)
        result = next_local_time(at, "America/Mexico_City", datetime.min.time().replace(hour=9))
        self.assertEqual(result, datetime(2026, 9, 25, 9, 0, tzinfo=ZoneInfo("America/Mexico_City")))


class DemoRunnerDataTests(unittest.TestCase):
    def test_realized_deal_net_includes_commission_swap_and_fee(self) -> None:
        class Deal:
            profit = 3.0
            commission = -0.1
            swap = -0.4
            fee = -0.2

        self.assertAlmostEqual(realized_deal_net_pnl(Deal()), 2.3)

    def test_rates_frame_excludes_forming_bar(self) -> None:
        rates = [
            {"time": int(pd.Timestamp("2026-01-01T00:00:00Z").timestamp()), "open": 1, "high": 2, "low": 1, "close": 2},
            {"time": int(pd.Timestamp("2026-01-01T00:01:00Z").timestamp()), "open": 2, "high": 3, "low": 2, "close": 3},
        ]
        frame = rates_to_frame(rates, 1, datetime(2026, 1, 1, 0, 1, 30, tzinfo=timezone.utc))
        self.assertEqual(len(frame), 1)
        self.assertEqual(frame.iloc[0]["timestamp_utc"], pd.Timestamp("2026-01-01T00:00:00Z"))

    def test_signal_uses_two_unconsumed_closed_crosses(self) -> None:
        decision = pd.Timestamp("2024-01-08T06:05:00Z")
        m1 = pd.DataFrame({
            "timestamp_utc": pd.date_range(decision - pd.Timedelta(minutes=5), periods=5, freq="min"),
            "open": [119.0] * 5, "high": [120.0] * 5, "low": [118.0] * 5, "close": [119.0] * 5,
        })
        m5 = pd.DataFrame({
            "timestamp_utc": pd.date_range(decision - pd.Timedelta(hours=10), periods=120, freq="5min"),
            "open": [119.0] * 120, "high": [120.0] * 120, "low": [110.0] * 120, "close": [119.0] * 120,
        })
        h1 = pd.DataFrame({
            "timestamp_utc": pd.date_range(decision - pd.Timedelta(hours=10), periods=10, freq="h"),
            "open": [100.0] * 10, "high": [110.0] * 10, "low": [90.0] * 10, "close": [100.0] * 10,
        })
        config = BacktestConfig(contract_size_oz=100)
        m1_event = (decision, "LONG", "M1:test-cross")
        m5_event = (decision, "LONG", "M5:test-cross")
        # 同一已收盘信号在整分钟、带毫秒及几秒后的真实 tick 上结果一致。
        for delay in (timedelta(0), timedelta(milliseconds=478), timedelta(seconds=4, milliseconds=999)):
            with self.subTest(delay=delay):
                with patch("research.xauusd_trailing.demo_runner._crosses", side_effect=[[m1_event], [m5_event]]):
                    signal = evaluate_entry_signal(
                        m1, m5, h1, bid=120.0, ask=120.2,
                        at=decision.to_pydatetime() + delay, config=config,
                    )
                self.assertIsNotNone(signal)
                self.assertEqual(signal.side, "LONG")
                self.assertEqual(signal.stop_loss, 110.0)

        with patch("research.xauusd_trailing.demo_runner._crosses", side_effect=[[m1_event], [m5_event]]):
            consumed = evaluate_entry_signal(
                m1, m5, h1, bid=120.0, ask=120.2, at=decision.to_pydatetime(), config=config,
                consumed_crosses={"M1:test-cross"},
            )
        self.assertIsNone(consumed)

    def test_signal_rejects_incomplete_five_minute_window(self) -> None:
        decision = pd.Timestamp("2024-01-08T06:05:00Z")
        m1 = pd.DataFrame({
            "timestamp_utc": pd.date_range(decision - pd.Timedelta(minutes=4), periods=4, freq="min"),
            "open": [119.0] * 4, "high": [120.0] * 4, "low": [118.0] * 4, "close": [119.0] * 4,
        })
        m5 = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close"])
        h1 = pd.DataFrame(columns=["timestamp_utc", "open", "high", "low", "close"])
        for delay in (timedelta(0), timedelta(milliseconds=478)):
            with self.subTest(delay=delay):
                signal = evaluate_entry_signal(
                    m1, m5, h1, bid=120.0, ask=120.2,
                    at=decision.to_pydatetime() + delay, config=BacktestConfig(),
                )
                self.assertIsNone(signal)


class DemoRunnerHeartbeatTests(unittest.TestCase):
    def make_runner(self, *, heartbeat_seconds: float = 60.0):
        class FakeMT5:
            @staticmethod
            def terminal_info():
                return SimpleNamespace(connected=True)

            @staticmethod
            def positions_get(symbol: str):
                return [SimpleNamespace(magic=7), SimpleNamespace(magic=99)]

        adapter = SimpleNamespace(mt5=FakeMT5(), ai_magic=7)
        runner = DemoStrategyRunner(
            adapter,
            BacktestConfig(),
            stop_new_entries_at=None,
            cycle_anchor_at=datetime(2026, 9, 25, 9, 0, tzinfo=ZoneInfo("America/Mexico_City")),
            log_path="unused.jsonl",
            heartbeat_seconds=heartbeat_seconds,
        )
        events = []
        runner._log = lambda event, **fields: events.append((event, fields))
        return runner, events

    def test_heartbeat_is_rate_limited_and_reports_runner_health(self) -> None:
        runner, events = self.make_runner()
        runner._last_heartbeat_monotonic = 100.0
        runner.last_quote_time_utc = datetime.now(timezone.utc) - timedelta(seconds=3)
        runner.last_entry_check_at = datetime(2026, 9, 25, 15, 0, tzinfo=timezone.utc)
        runner.last_entry_check_result = "NO_SIGNAL"

        with patch("research.xauusd_trailing.demo_runner.time.monotonic", return_value=159.0):
            runner._maybe_log_heartbeat()
        self.assertEqual(events, [])

        with patch("research.xauusd_trailing.demo_runner.time.monotonic", return_value=160.0):
            runner._maybe_log_heartbeat()

        self.assertEqual(len(events), 1)
        event, fields = events[0]
        self.assertEqual(event, "HEARTBEAT")
        self.assertTrue(fields["terminal_connected"])
        self.assertEqual(fields["strategy_open_positions"], 1)
        self.assertEqual(fields["last_entry_check_result"], "NO_SIGNAL")
        self.assertIsNotNone(fields["quote_age_seconds"])
        self.assertEqual(fields["runner_state"], "RUNNING")
        self.assertEqual(fields["check_minutes"], 1)
        self.assertTrue(fields["require_latest_m1_cross"])

    def test_heartbeat_interval_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "heartbeat_seconds must be a finite positive number"):
            self.make_runner(heartbeat_seconds=0)
        with self.assertRaisesRegex(ValueError, "heartbeat_seconds must be a finite positive number"):
            self.make_runner(heartbeat_seconds=float("nan"))

    def test_first_heartbeat_is_written_immediately(self) -> None:
        runner, events = self.make_runner()
        with patch("research.xauusd_trailing.demo_runner.time.monotonic", return_value=10.0):
            runner._maybe_log_heartbeat()
        self.assertEqual([event for event, _ in events], ["HEARTBEAT"])

    def test_heartbeat_marks_stale_quotes(self) -> None:
        runner, events = self.make_runner()
        runner.last_quote_time_utc = datetime.now(timezone.utc) - timedelta(seconds=121)
        with patch("research.xauusd_trailing.demo_runner.time.monotonic", return_value=10.0):
            runner._maybe_log_heartbeat()
        self.assertEqual(events[0][1]["runner_state"], "RUNNING_STALE_QUOTE")


class DemoRunnerEntryScheduleTests(unittest.TestCase):
    def make_runner(self, at: datetime, *, check_minutes: int = 1):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        runner = DemoStrategyRunner(
            SimpleNamespace(), BacktestConfig(check_minutes=check_minutes),
            stop_new_entries_at=None,
            cycle_anchor_at=datetime(2026, 10, 1, 9, tzinfo=ZoneInfo("America/Mexico_City")),
            log_path=Path(directory) / "demo.jsonl", state_path=Path(directory) / "state.json",
        )
        events = []
        runner._log = lambda event, **fields: events.append((event, fields))
        runner.frames = self.frames_at(at)
        return runner, events

    @staticmethod
    def frames_at(at: datetime):
        decision = pd.Timestamp(at).floor("min")
        def bars(stamps):
            return pd.DataFrame({
                "timestamp_utc": stamps, "open": 119.0, "high": 120.0,
                "low": 110.0, "close": 119.0,
            })
        return {
            "M1": bars(pd.date_range(decision - pd.Timedelta(minutes=5), periods=5, freq="min")),
            "M5": bars(pd.date_range(decision.floor("5min") - pd.Timedelta(minutes=5), periods=1, freq="5min")),
            "H1": bars(pd.date_range(decision.floor("h") - pd.Timedelta(hours=5), periods=5, freq="h")),
        }

    def test_every_minute_checks_once_even_if_first_tick_is_late(self) -> None:
        at = datetime(2026, 10, 1, 15, 41, 17, tzinfo=timezone.utc)
        runner, _ = self.make_runner(at)
        with patch("research.xauusd_trailing.demo_runner.evaluate_entry_signal", return_value=None) as evaluate:
            runner._consider_entry(at, 120, 120.2)
            runner._consider_entry(at + timedelta(seconds=3), 120, 120.2)
            self.assertEqual(evaluate.call_count, 1)
            next_tick = at + timedelta(minutes=1)
            runner.frames = self.frames_at(next_tick)
            runner._consider_entry(next_tick, 120, 120.2)
            self.assertEqual(evaluate.call_count, 2)
        self.assertEqual(runner.last_entry_minute.minute, 42)
        self.assertFalse(runner.consumed_crosses)

    def test_unready_data_is_refreshed_and_retried_without_duplicate_check(self) -> None:
        at = datetime(2026, 10, 1, 15, 41, 6, tzinfo=timezone.utc)
        runner, events = self.make_runner(at)
        ready = self.frames_at(at)
        incomplete = {**ready, "M1": ready["M1"].iloc[:-1].copy()}
        def publish_frames(stamp):
            runner.frames = incomplete if refresh.call_count == 1 else ready
        with patch.object(runner, "_refresh_frames", side_effect=publish_frames) as refresh:
            with patch("research.xauusd_trailing.demo_runner.evaluate_entry_signal", return_value=None) as evaluate:
                runner._refresh_frames_if_needed(at)
                runner._consider_entry(at, 120, 120.2)
                self.assertEqual(runner.last_entry_check_result, "DATA_NOT_READY")
                self.assertIsNone(runner.last_entry_minute)
                self.assertEqual(evaluate.call_count, 0)
                next_tick = at + timedelta(seconds=1)
                runner._refresh_frames_if_needed(next_tick)
                runner._consider_entry(next_tick, 120, 120.2)
                runner._refresh_frames_if_needed(next_tick + timedelta(seconds=1))
                runner._consider_entry(next_tick + timedelta(seconds=1), 120, 120.2)
                self.assertEqual(refresh.call_count, 2)
                self.assertEqual(evaluate.call_count, 1)
        self.assertEqual([event for event, _ in events], ["ENTRY_CHECK_DATA_NOT_READY", "ENTRY_CHECK_NO_SIGNAL"])
        self.assertFalse(runner.consumed_crosses)

    def test_next_minute_waits_for_new_bar_instead_of_backfilling_old_signal(self) -> None:
        at = datetime(2026, 10, 1, 15, 41, 6, tzinfo=timezone.utc)
        runner, _ = self.make_runner(at)
        old_ready = runner.frames
        runner.frames = {**old_ready, "M1": old_ready["M1"].iloc[:-1].copy()}
        with patch("research.xauusd_trailing.demo_runner.evaluate_entry_signal", return_value=None) as evaluate:
            runner._consider_entry(at, 120, 120.2)
            next_tick = at + timedelta(minutes=1)
            runner.frames = old_ready
            runner._consider_entry(next_tick, 120, 120.2)
            self.assertEqual(evaluate.call_count, 0)
            runner.frames = self.frames_at(next_tick)
            runner._consider_entry(next_tick + timedelta(seconds=1), 120, 120.2)
            self.assertEqual(evaluate.call_count, 1)
            self.assertEqual(evaluate.call_args.kwargs["at"].minute, 42)

    def test_explicit_five_minute_configuration_still_uses_its_schedule(self) -> None:
        at = datetime(2026, 10, 1, 15, 41, 17, tzinfo=timezone.utc)
        runner, _ = self.make_runner(at, check_minutes=5)
        with patch("research.xauusd_trailing.demo_runner.evaluate_entry_signal", return_value=None) as evaluate:
            runner._consider_entry(at, 120, 120.2)
            self.assertEqual(evaluate.call_count, 0)
            tick = at.replace(minute=45)
            runner.frames = self.frames_at(tick)
            runner._consider_entry(tick, 120, 120.2)
            self.assertEqual(evaluate.call_count, 1)

    def test_lockout_still_blocks_evaluation_and_orders(self) -> None:
        at = datetime(2026, 10, 1, 15, 41, 17, tzinfo=timezone.utc)
        runner, _ = self.make_runner(at)
        runner.entry_lockout = True
        with patch("research.xauusd_trailing.demo_runner.evaluate_entry_signal") as evaluate:
            runner._consider_entry(at, 120, 120.2)
            evaluate.assert_not_called()
        self.assertEqual(runner.last_entry_check_result, "ENTRY_LOCKOUT")

if __name__ == "__main__":
    unittest.main()
