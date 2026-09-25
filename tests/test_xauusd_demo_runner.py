from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
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
        with patch("research.xauusd_trailing.demo_runner._crosses", side_effect=[[m1_event], [m5_event]]):
            signal = evaluate_entry_signal(
                m1, m5, h1, bid=120.0, ask=120.2, at=decision.to_pydatetime(), config=config,
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
        signal = evaluate_entry_signal(
            m1, m5, h1, bid=120.0, ask=120.2, at=decision.to_pydatetime(), config=BacktestConfig(),
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


if __name__ == "__main__":
    unittest.main()
