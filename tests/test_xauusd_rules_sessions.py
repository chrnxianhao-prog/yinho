from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

from research.xauusd_trailing.demo_runner import DemoStrategyRunner
from research.xauusd_trailing.engine import run_backtest
from research.xauusd_trailing.models import BacktestConfig
from research.xauusd_trailing.rules import (
    cross_is_eligible,
    entry_block_reason,
    is_utc_blackout,
    record_stop_exit,
    risk_halt_reason,
    size_for_stop,
)
from research.xauusd_trailing.sessions import (
    infer_session_breaks,
    next_close_from_observed_ticks,
    next_close_from_weekly_schedule,
    recent_daily_last_ticks,
    session_close_due,
    trade_session_end_times,
    within_entry_buffer,
)
from scripts.export_xauusd_mt5_history import export_history
from scripts.xauusd_watchdog import check_heartbeat


UTC = timezone.utc


class SharedRuleTests(unittest.TestCase):
    def test_risk_size_floors_to_volume_step_and_rejects_too_small(self) -> None:
        sized = size_for_stop(
            equity=2000,
            risk_per_trade_pct=0.005,
            fixed_fallback_lots=0.05,
            stop_distance_usd=2,
            contract_size_oz=100,
            volume_min=0.01,
            volume_step=0.01,
            max_stop_distance_usd=8,
        )
        self.assertEqual(sized.lots, 0.05)
        self.assertEqual(sized.risk_budget_usd, 10)
        too_small = size_for_stop(
            equity=100,
            risk_per_trade_pct=0.005,
            fixed_fallback_lots=0.05,
            stop_distance_usd=8,
            contract_size_oz=100,
            volume_min=0.01,
            volume_step=0.01,
            max_stop_distance_usd=8,
        )
        self.assertIsNone(too_small.lots)
        self.assertEqual(too_small.reason, "BELOW_MINIMUM_VOLUME")

    def test_fixed_size_is_used_only_for_null_risk_percentage(self) -> None:
        sized = size_for_stop(
            equity=2000,
            risk_per_trade_pct=None,
            fixed_fallback_lots=0.057,
            stop_distance_usd=2,
            contract_size_oz=100,
            volume_min=0.01,
            volume_step=0.01,
            max_stop_distance_usd=8,
        )
        self.assertEqual(sized.lots, 0.05)

    def test_contract_size_is_required_by_backtest(self) -> None:
        with self.assertRaisesRegex(ValueError, "contract_size_oz is required"):
            run_backtest(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), BacktestConfig())

    def test_three_stop_rule_scopes(self) -> None:
        expected = {
            "current_cycle": {"current"},
            "next_cycle": {"next"},
            "both": {"current", "next"},
        }
        for scope, targets in expected.items():
            with self.subTest(scope=scope):
                losses: dict[str, int] = {}
                all_stops: dict[str, int] = {}
                halted: set[str] = set()
                outcome = record_stop_exit(
                    cycle_id="current",
                    next_cycle_id="next",
                    net_pnl=-1.0,
                    losing_stops_by_cycle=losses,
                    all_stops_by_cycle=all_stops,
                    halted_cycles=halted,
                    max_losing_stops_per_cycle=1,
                    stop_rule_scope=scope,
                    count_profitable_stops=False,
                )
                self.assertEqual(halted, targets)
                self.assertEqual((outcome.losing_stops, outcome.all_stops), (1, 1))

    def test_profitable_stop_counts_only_when_enabled_but_all_stops_are_reported(self) -> None:
        losses: dict[str, int] = {}
        all_stops: dict[str, int] = {}
        halted: set[str] = set()
        outcome = record_stop_exit(
            cycle_id="c", next_cycle_id="n", net_pnl=2,
            losing_stops_by_cycle=losses, all_stops_by_cycle=all_stops, halted_cycles=halted,
            max_losing_stops_per_cycle=1, stop_rule_scope="current_cycle", count_profitable_stops=True,
        )
        self.assertEqual(outcome.losing_stops, 0)
        self.assertEqual(outcome.all_stops, 1)
        self.assertEqual(halted, {"c"})

    def test_current_cycle_entry_gate_and_lockout_are_shared(self) -> None:
        self.assertEqual(
            entry_block_reason(
                cycle_id="c", entries_this_cycle=0, max_entries_per_cycle=10,
                halted_cycles={"c"}, risk_halted=False, entry_lockout=False,
                in_blackout=False, before_session_close=False,
            ),
            "CYCLE_STOP_LIMIT",
        )
        self.assertEqual(
            entry_block_reason(
                cycle_id="c", entries_this_cycle=0, max_entries_per_cycle=10,
                halted_cycles=set(), risk_halted=False, entry_lockout=True,
                in_blackout=False, before_session_close=False,
            ),
            "ENTRY_LOCKOUT",
        )

    def test_daily_loss_and_peak_drawdown_circuit_breaker(self) -> None:
        self.assertEqual(
            risk_halt_reason(
                equity=1940, equity_peak=2000, daily_pnl=-60, day_start_equity=2000,
                max_daily_loss_pct=0.03, max_drawdown_pct=0.10,
            ),
            "MAX_DAILY_LOSS",
        )
        self.assertEqual(
            risk_halt_reason(
                equity=1799, equity_peak=2000, daily_pnl=-10, day_start_equity=2000,
                max_daily_loss_pct=0.03, max_drawdown_pct=0.10,
            ),
            "MAX_DRAWDOWN",
        )

    def test_latest_m1_cross_and_utc_blackout_boundaries(self) -> None:
        decision = datetime(2026, 9, 25, 14, 5, tzinfo=UTC)
        self.assertTrue(cross_is_eligible(decision, decision, lookback_minutes=5, require_latest=True))
        self.assertFalse(cross_is_eligible(decision - timedelta(minutes=1), decision, lookback_minutes=5, require_latest=True))
        self.assertTrue(is_utc_blackout(decision, ["14:00-14:10"]))
        self.assertFalse(is_utc_blackout(datetime(2026, 9, 25, 14, 10, tzinfo=UTC), ["14:00-14:10"]))
        self.assertTrue(is_utc_blackout(datetime(2026, 9, 25, 23, 30, tzinfo=UTC), ["23:00-01:00"]))


class SessionRuleTests(unittest.TestCase):
    def test_gap_inference_and_entry_buffer_are_utc_and_strictly_scheduled(self) -> None:
        stamps = list(pd.date_range("2026-09-25T20:50:00Z", periods=5, freq="min"))
        stamps += list(pd.date_range("2026-09-25T22:00:00Z", periods=3, freq="min"))
        breaks = infer_session_breaks(stamps)
        self.assertEqual(list(breaks), [4])
        inferred = breaks[4]
        self.assertEqual(inferred.break_start_utc, pd.Timestamp("2026-09-25T22:00:00Z"))
        self.assertEqual(inferred.last_executable_close_utc, pd.Timestamp("2026-09-25T20:55:00Z"))
        self.assertTrue(within_entry_buffer(pd.Timestamp("2026-09-25T20:35:00Z"), inferred.last_executable_close_utc, 30))
        self.assertFalse(within_entry_buffer(pd.Timestamp("2026-09-25T20:24:00Z"), inferred.last_executable_close_utc, 30))
        self.assertTrue(session_close_due(datetime(2026, 9, 25, 20, 40, tzinfo=UTC), datetime(2026, 9, 25, 20, 55, tzinfo=UTC), 15))

    def test_symbol_session_windows_preferred_when_explicitly_exposed(self) -> None:
        info = SimpleNamespace(trade_sessions_utc={"fri": [("00:00", "22:00")]})
        schedule = trade_session_end_times(info)
        self.assertEqual(schedule, {4: [22 * 3600]})
        close = next_close_from_weekly_schedule(datetime(2026, 9, 25, 21, 0, tzinfo=UTC), schedule)
        self.assertEqual(close, datetime(2026, 9, 25, 22, 0, tzinfo=UTC))

    def test_recent_twenty_day_last_tick_fallback_and_next_close_estimate(self) -> None:
        class FakeMT5:
            COPY_TICKS_ALL = 0

            @staticmethod
            def copy_ticks_range(symbol, start, end, mode):
                if start.weekday() >= 5:
                    return []
                close = start.replace(hour=20, minute=57, second=0)
                return [{"time": int(close.timestamp()), "time_msc": int(close.timestamp() * 1000)}]

        now = datetime(2026, 9, 25, 20, 0, tzinfo=UTC)
        ticks = recent_daily_last_ticks(FakeMT5(), "XAUUSD", now)
        self.assertEqual(len(ticks), 20)
        close = next_close_from_observed_ticks(now, ticks)
        self.assertEqual(close, datetime(2026, 9, 25, 20, 57, tzinfo=UTC))


class DemoStateTests(unittest.TestCase):
    def make_runner(self, state_path: Path, *, recovery_polls: int = 2):
        adapter = SimpleNamespace(mt5=SimpleNamespace(), ai_magic=7)
        runner = DemoStrategyRunner(
            adapter,
            BacktestConfig(),
            stop_new_entries_at=None,
            cycle_anchor_at=datetime(2026, 9, 25, 9, tzinfo=ZoneInfo("America/Mexico_City")),
            log_path=state_path.parent / "runner.jsonl",
            state_path=state_path,
            lockout_recovery_polls=recovery_polls,
        )
        events: list[tuple[str, dict[str, object]]] = []
        runner._log = lambda event, **fields: events.append((event, fields))
        return runner, events

    def test_cycle_and_cross_state_persist_and_restore(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            runner, _ = self.make_runner(path)
            runner.entries_by_cycle["cycle"] = 4
            runner.losing_stops_by_cycle["cycle"] = 2
            runner.all_stops_by_cycle["cycle"] = 3
            runner.consumed_crosses.add("M1:cross")
            runner.last_processed_deal_ticket = 123
            runner.risk_halt_day = datetime.now(UTC).date()
            runner._persist_state()
            restored, _ = self.make_runner(path)
            self.assertTrue(restored.state_loaded)
            self.assertEqual(restored.entries_by_cycle["cycle"], 4)
            self.assertEqual(restored.losing_stops_by_cycle["cycle"], 2)
            self.assertEqual(restored.all_stops_by_cycle["cycle"], 3)
            self.assertIn("M1:cross", restored.consumed_crosses)
            self.assertEqual(restored.last_processed_deal_ticket, 123)

    def test_lockout_recovers_after_configured_successful_polls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runner, events = self.make_runner(Path(directory) / "state.json", recovery_polls=2)
            runner._set_entry_lockout(True, reason="test")
            runner._poll_succeeded()
            self.assertTrue(runner.entry_lockout)
            runner._poll_succeeded()
            self.assertFalse(runner.entry_lockout)
            self.assertIn("ENTRY_LOCKOUT_AUTO_RECOVERED", [event for event, _ in events])


class WatchdogAndExportTests(unittest.TestCase):
    def test_watchdog_reports_missing_and_stale_heartbeat(self) -> None:
        now = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = check_heartbeat(root, now=now, max_age_seconds=60)
            self.assertEqual(missing["reason"], "NO_HEARTBEAT")
            log = root / "xauusd_demo_test.jsonl"
            log.write_text(json.dumps({"event": "HEARTBEAT", "time_utc": (now - timedelta(seconds=61)).isoformat()}) + "\n", encoding="utf-8")
            stale = check_heartbeat(root, now=now, max_age_seconds=60)
            self.assertEqual(stale["event"], "WATCHDOG_ALERT")
            self.assertEqual(stale["reason"], "HEARTBEAT_STALE")

    def test_export_keeps_spread_and_writes_coverage(self) -> None:
        class FakeMT5:
            TIMEFRAME_M1, TIMEFRAME_M5, TIMEFRAME_H1 = 1, 5, 60

            @staticmethod
            def symbol_select(symbol, selected):
                return True

            @staticmethod
            def copy_rates_range(symbol, timeframe, start, end):
                return [{
                    "time": int(start.timestamp()), "open": 1, "high": 2, "low": 1,
                    "close": 1.5, "tick_volume": 10, "spread": 20, "real_volume": 0,
                }]

        with tempfile.TemporaryDirectory() as directory:
            manifest = export_history(
                FakeMT5(), symbol="XAUUSD",
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 2, tzinfo=UTC),
                output_dir=Path(directory),
            )
            exported = pd.read_csv(Path(directory) / "XAUUSD_M1.csv")
            self.assertIn("spread_points", exported.columns)
            self.assertEqual(int(exported.iloc[0]["spread_points"]), 20)
            self.assertEqual(manifest["timeframes"]["H1"]["rows"], 1)
            self.assertTrue((Path(directory) / "coverage.json").exists())


if __name__ == "__main__":
    unittest.main()
