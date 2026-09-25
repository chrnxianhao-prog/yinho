from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pandas as pd

from research.xauusd_trailing.causality import assert_prefix_causal
from research.xauusd_trailing.data import load_mt5_csv, validate_frames
from research.xauusd_trailing.engine import _cycle_for, _next_cycle_id, run_backtest
from research.xauusd_trailing.indicators import h1_range_frame, macd_frame
from research.xauusd_trailing.models import BacktestConfig


def synthetic_frames() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    start = pd.Timestamp("2024-01-08T06:00:00Z")
    times = pd.date_range(start, periods=360, freq="min")
    lows = np.full(len(times), 101.2)
    lows[:5] = 101.0
    m1 = pd.DataFrame(
        {
            "timestamp_utc": times,
            "open": np.full(len(times), 101.8),
            "high": np.full(len(times), 102.0),
            "low": lows,
            "close": np.full(len(times), 101.8),
        }
    )
    indexed = m1.set_index("timestamp_utc")
    m5 = indexed.resample("5min", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    h1_active = indexed.resample("1h", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    warmup_times = pd.date_range("2024-01-08T01:00:00Z", periods=5, freq="h")
    warmup = pd.DataFrame(
        {
            "timestamp_utc": warmup_times,
            "open": 101.8,
            "high": 102.0,
            "low": 101.0,
            "close": 101.8,
        }
    )
    h1 = pd.concat([warmup, h1_active], ignore_index=True).sort_values("timestamp_utc").reset_index(drop=True)
    return m1, m5, h1


def reaggregate(m1: pd.DataFrame, warmup_h1_low: float = 101.0) -> tuple[pd.DataFrame, pd.DataFrame]:
    indexed = m1.set_index("timestamp_utc")
    m5 = indexed.resample("5min", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    active_h1 = indexed.resample("1h", origin="epoch", label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    return m5, active_h1


class XauusdIndicatorTests(unittest.TestCase):
    def test_future_perturbation_does_not_change_past_macd(self) -> None:
        frame = pd.DataFrame({"close": 100 + np.sin(np.arange(150) / 5)})
        features = macd_frame(frame["close"], fast=3, slow=8, signal=4)
        self.assertTrue(features["golden_cross"].any())
        self.assertTrue(features["dead_cross"].any())
        assert_prefix_causal(frame, lambda data: macd_frame(data["close"], 3, 8, 4), 90)

    def test_h1_range_uses_closed_bar_timestamp(self) -> None:
        h1 = pd.DataFrame(
            {
                "timestamp_utc": pd.date_range("2024-01-01T00:00:00Z", periods=6, freq="h"),
                "high": [10, 11, 12, 13, 14, 100],
                "low": [1, 2, 3, 4, 5, -50],
            }
        )
        result = h1_range_frame(h1, 5)
        self.assertEqual(result.iloc[4]["bar_close_time_utc"], pd.Timestamp("2024-01-01T05:00:00Z"))
        self.assertEqual(result.iloc[4]["range_high"], 14)
        self.assertEqual(result.iloc[4]["range_low"], 1)

    def test_five_hour_cycle_is_anchored_to_local_nine(self) -> None:
        from zoneinfo import ZoneInfo

        local = pd.Timestamp("2024-01-08T22:00:00", tz=ZoneInfo("America/Mexico_City")).to_pydatetime()
        cycle_id, start, end = _cycle_for(local, 5)
        self.assertEqual(cycle_id, "2024-01-08T22:00")
        self.assertEqual(start.hour, 22)
        self.assertEqual(end.hour, 3)
        self.assertEqual(_next_cycle_id(local, 5), "2024-01-09T03:00")

    def test_mexico_historical_dst_uses_iana_offsets(self) -> None:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("America/Mexico_City")
        before = pd.Timestamp("2021-04-04T07:59:00Z").tz_convert(tz).to_pydatetime()
        after = pd.Timestamp("2021-04-04T08:00:00Z").tz_convert(tz).to_pydatetime()
        self.assertEqual(before.utcoffset().total_seconds(), -6 * 3600)
        self.assertEqual(after.utcoffset().total_seconds(), -5 * 3600)
        before_cycle = _cycle_for(before, 5)
        after_cycle = _cycle_for(after, 5)
        self.assertEqual(before_cycle[0], after_cycle[0])
        self.assertEqual(
            before_cycle[2].astimezone(ZoneInfo("UTC")) - before_cycle[1].astimezone(ZoneInfo("UTC")),
            pd.Timedelta(hours=5).to_pytimedelta(),
        )


class XauusdDataTests(unittest.TestCase):
    def test_mt5_split_date_time_and_bracketed_headers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bars.csv"
            pd.DataFrame(
                {
                    "<DATE>": ["2024.01.01"],
                    "<TIME>": ["00:00"],
                    "<OPEN>": [2000.0],
                    "<HIGH>": [2001.0],
                    "<LOW>": [1999.0],
                    "<CLOSE>": [2000.5],
                }
            ).to_csv(path, index=False)
            result = load_mt5_csv(path)
            self.assertEqual(result.iloc[0]["timestamp_utc"], pd.Timestamp("2024-01-01T00:00:00Z"))
            self.assertAlmostEqual(result.iloc[0]["close"], 2000.5)

    def test_aggregated_frames_match_m1(self) -> None:
        m1, m5, h1 = synthetic_frames()
        audit = validate_frames(m1, m5, h1)
        self.assertTrue(audit.ok, audit.as_dict())
        self.assertEqual(audit.aggregation_mismatches, {"M5": 0, "H1": 0})


class XauusdEngineTests(unittest.TestCase):
    def test_entry_uses_closed_crosses_once_and_emits_no_live_order(self) -> None:
        m1, m5, h1 = synthetic_frames()
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z",
            end="2024-01-08T06:10:00Z",
            contract_size_oz=100,
            risk_per_trade_pct=None,
            require_latest_m1_cross=False,
            macd_fast=1,
            macd_slow=2,
            macd_signal=2,
        )
        m1_cross = [
            (pd.Timestamp("2024-01-08T06:04:00Z").value, "LONG", "m1-test"),
            (pd.Timestamp("2024-01-08T06:09:00Z").value, "LONG", "m1-repeat"),
        ]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-test")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertFalse(result.events.empty, (result.metrics, result.audit))
        entries = result.events[result.events["event_type"] == "ENTRY_FILLED"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries.iloc[0]["event_time_utc"], "2024-01-08T06:05:00+00:00")
        self.assertEqual(result.metrics["status"], "MONETARY_WITH_CONFIGURED_COSTS")
        self.assertEqual(len(result.open_positions), 1)

    def test_no_aggregate_lot_cap_when_positions_remain_open(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1["open"] = [101.8 + index * 0.02 for index in range(len(m1))]
        m1["close"] = m1["open"]
        m1["high"] = m1["open"] + 0.1
        m1["low"] = m1["open"] - 0.1
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        check_times = pd.date_range("2024-01-08T06:30:00Z", "2024-01-08T07:20:00Z", freq="5min")
        m1_cross = [((stamp - pd.Timedelta(minutes=1)).value, "LONG", f"m1-cap-{stamp.value}") for stamp in check_times]
        m5_cross = [(stamp.value, "LONG", f"m5-cap-{stamp.value}") for stamp in check_times]
        config = BacktestConfig(
            start="2024-01-08T06:30:00Z", end="2024-01-08T07:25:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            max_entries_per_cycle=20, require_latest_m1_cross=False,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        total_lots = result.open_positions["lots"].abs().sum()
        self.assertEqual(len(result.open_positions), 11)
        self.assertAlmostEqual(total_lots, 0.55)
        rejects = result.events[result.events["event_type"] == "ENTRY_REJECTED"]
        self.assertTrue(rejects.empty)

    def test_stop_loss_costs_are_separate_and_count_as_stop(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1.loc[6, "low"] = 100.9
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:10:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            require_latest_m1_cross=False, macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:04:00Z").value, "LONG", "m1-stop")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-stop")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["close_reason"], "STOP_LOSS")
        self.assertAlmostEqual(trade["gross_pnl"], -4.0)
        self.assertAlmostEqual(trade["spread_cost"], 1.0)
        self.assertAlmostEqual(trade["slippage_cost"], 1.0)
        self.assertAlmostEqual(trade["net_pnl"], -6.0)
        self.assertEqual(result.metrics["stop_loss_count"], 1)
        self.assertEqual(result.metrics["status"], "MONETARY_WITH_CONFIGURED_COSTS")

    def test_trailing_stop_only_moves_toward_profit(self) -> None:
        m1, m5, h1 = synthetic_frames()
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:35:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            require_latest_m1_cross=False,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:04:00Z").value, "LONG", "m1-trail")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-trail")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        raised = result.events[result.events["event_type"] == "TRAIL_STOP_RAISED"]
        self.assertEqual(len(raised), 1)
        self.assertGreater(raised.iloc[0]["new_stop"], raised.iloc[0]["old_stop"])
        self.assertEqual(result.trades.iloc[0]["stop_updates"], 1)
        self.assertEqual(result.trades.iloc[0]["close_reason"], "STOP_LOSS")

    def test_profitable_trailing_stop_does_not_count_toward_pause(self) -> None:
        m1, m5, h1 = synthetic_frames()
        profitable = m1["timestamp_utc"] >= pd.Timestamp("2024-01-08T06:25:00Z")
        m1.loc[profitable, ["open", "high", "low", "close"]] = [103.0, 104.0, 102.5, 103.5]
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:40:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            max_losing_stops_per_cycle=1,
            macd_fast=1, macd_slow=2, macd_signal=2,
            require_latest_m1_cross=False,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:04:00Z").value, "LONG", "m1-profitable-stop")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-profitable-stop")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertEqual(result.trades.iloc[0]["close_reason"], "STOP_LOSS")
        self.assertGreater(result.trades.iloc[0]["net_pnl"], 0)
        stop_counts = result.events[result.events["event_type"] == "STOP_COUNTS"]
        self.assertEqual(stop_counts.iloc[0]["all_stop_count"], 1)
        self.assertEqual(stop_counts.iloc[0]["losing_stop_count"], 0)
        self.assertTrue(result.events[result.events["event_type"] == "CYCLE_PAUSED"].empty)

    def test_pause_after_threshold_stops_skips_next_local_cycle(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1["low"] = 99.0
        m5, active_h1 = reaggregate(m1)
        h1 = h1.iloc[:5].copy()
        h1.loc[:, "low"] = 99.0
        h1 = pd.concat([h1, active_h1], ignore_index=True)
        check_times = pd.date_range("2024-01-08T06:05:00Z", "2024-01-08T11:55:00Z", freq="5min")
        m1_cross = [((stamp - pd.Timedelta(minutes=1)).value, "LONG", f"m1-{stamp.value}") for stamp in check_times]
        m5_cross = [(stamp.value, "LONG", f"m5-{stamp.value}") for stamp in check_times]
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T12:00:00Z",
            max_entries_per_cycle=100, max_losing_stops_per_cycle=2,
            stop_rule_scope="next_cycle",
            risk_per_trade_pct=None,
            contract_size_oz=100, cycle_anchor_local="2024-01-08T00:00:00",
            require_latest_m1_cross=False,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        pause = result.events[result.events["event_type"] == "CYCLE_PAUSED"]
        self.assertEqual(len(pause), 1)
        self.assertEqual(pause.iloc[0]["paused_cycle_id"], "2024-01-08T05:00")
        late_entries = result.events[
            (result.events["event_type"] == "ENTRY_FILLED")
            & (pd.to_datetime(result.events["event_time_utc"], utc=True) >= pd.Timestamp("2024-01-08T11:00:00Z"))
        ]
        self.assertTrue(late_entries.empty)
        self.assertGreaterEqual(result.metrics["stop_loss_count"], 2)

    def test_prior_cycle_position_stop_halts_the_cycle_where_it_closes(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1.loc[5:, "low"] = 101.2 + np.arange(len(m1) - 5) * 0.001
        m1.loc[301, "low"] = 100.0
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T11:20:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            max_losing_stops_per_cycle=1, stop_rule_scope="current_cycle",
            cycle_anchor_local="2024-01-08T00:00:00",
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [
            (pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-old-cycle"),
            (pd.Timestamp("2024-01-08T11:05:00Z").value, "LONG", "m1-current-cycle"),
        ]
        m5_cross = [
            (pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-old-cycle"),
            (pd.Timestamp("2024-01-08T11:05:00Z").value, "LONG", "m5-current-cycle"),
        ]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        first_entry = result.events[result.events["event_type"] == "ENTRY_FILLED"].iloc[0]
        self.assertEqual(first_entry["cycle_id"], "2024-01-08T00:00")
        stop_count = result.events[result.events["event_type"] == "STOP_COUNTS"].iloc[0]
        self.assertEqual(stop_count["cycle_id"], "2024-01-08T05:00")
        blocked = result.events[
            (result.events["event_type"] == "ENTRY_REJECTED")
            & (result.events.get("reason") == "CYCLE_STOP_LIMIT")
        ]
        self.assertGreaterEqual(len(blocked), 1)

    def test_default_requires_m1_cross_on_latest_closed_minute(self) -> None:
        m1, m5, h1 = synthetic_frames()
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:10:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:04:00Z").value, "LONG", "m1-too-old")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-latest")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertTrue(result.trades.empty)

    def test_minimum_stop_distance_rejects_signal(self) -> None:
        m1, m5, h1 = synthetic_frames()
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:10:00Z",
            contract_size_oz=100, risk_per_trade_pct=None, min_stop_distance_usd=1.5,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-min-stop")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-min-stop")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        rejects = result.events[result.events["event_type"] == "ENTRY_REJECTED"]
        self.assertTrue((rejects["reason"] == "MIN_STOP_DISTANCE").any())

    def test_margin_stop_out_is_not_counted_as_a_stop_loss(self) -> None:
        m1, m5, h1 = synthetic_frames()
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:10:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            margin_call_level=50000, stop_out_level=40000,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-margin")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-margin")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertEqual(result.trades.iloc[0]["close_reason"], "MARGIN_STOP_OUT")
        self.assertEqual(result.metrics["margin_stop_out_count"], 1)
        self.assertEqual(result.metrics["stop_loss_count"], 0)

    def test_daily_loss_trips_close_only_account_halt(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1.loc[6, "low"] = 100.0
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:20:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            max_stop_distance_usd=30, max_daily_loss_pct=0.03,
            slippage_usd_per_side=20,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-halt")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-halt")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertTrue((result.events["event_type"] == "RISK_HALT").any())
        self.assertEqual(result.metrics["risk_halt_count"], 1)

    def test_triple_swap_is_charged_at_utc_server_day_rollover(self) -> None:
        start = pd.Timestamp("2024-01-09T23:40:00Z")
        times = pd.date_range(start, periods=30, freq="min")
        lows = 101.2 + np.arange(len(times)) * 0.001
        lows[:5] = 101.0
        lows[21] = 100.0  # 2024-01-10 00:01 UTC, after Wednesday rollover.
        m1 = pd.DataFrame({
            "timestamp_utc": times, "open": 101.8, "high": 102.0,
            "low": lows, "close": 101.8,
        })
        m5, _ = reaggregate(m1)
        warmup_times = pd.date_range("2024-01-09T18:00:00Z", periods=5, freq="h")
        h1 = pd.DataFrame({
            "timestamp_utc": warmup_times, "open": 101.8, "high": 102.0,
            "low": 99.0, "close": 101.8,
        })
        config = BacktestConfig(
            start="2024-01-09T23:40:00Z", end="2024-01-10T00:10:00Z",
            contract_size_oz=100, risk_per_trade_pct=None,
            swap_long_usd_per_lot_per_night=-2.0, triple_swap_weekday=2,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-09T23:45:00Z").value, "LONG", "m1-swap")]
        m5_cross = [(pd.Timestamp("2024-01-09T23:45:00Z").value, "LONG", "m5-swap")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertEqual(result.trades.iloc[0]["close_reason"], "STOP_LOSS")
        self.assertAlmostEqual(result.trades.iloc[0]["swap"], -0.3)
        self.assertAlmostEqual(result.metrics["total_swap"], -0.3)

    def test_inferred_session_break_closes_on_last_tradable_bar(self) -> None:
        first = pd.date_range("2024-01-08T06:00:00Z", periods=60, freq="min")
        resumed = pd.date_range("2024-01-08T07:30:00Z", periods=60, freq="min")
        times = first.append(resumed)
        lows = np.full(len(times), 101.2)
        lows[:5] = 101.0
        lows[5:60] = 101.2 + np.arange(55) * 0.001
        lows[60:] = 101.3
        m1 = pd.DataFrame({
            "timestamp_utc": times,
            "open": 101.8,
            "high": 102.0,
            "low": lows,
            "close": 101.8,
        })
        m5, active_h1 = reaggregate(m1)
        warmup_times = pd.date_range("2024-01-08T01:00:00Z", periods=5, freq="h")
        warmup = pd.DataFrame({
            "timestamp_utc": warmup_times, "open": 101.8, "high": 102.0,
            "low": 99.0, "close": 101.8,
        })
        h1 = pd.concat([warmup, active_h1], ignore_index=True).sort_values("timestamp_utc").reset_index(drop=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T08:30:00Z",
            contract_size_oz=100, risk_per_trade_pct=None, require_latest_m1_cross=False,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-session")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-session")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        self.assertEqual(len(result.trades), 1)
        trade = result.trades.iloc[0]
        self.assertEqual(trade["close_reason"], "SESSION_CLOSE")
        self.assertEqual(trade["exit_time_utc"], "2024-01-08T07:00:00+00:00")
        close_event = result.events[result.events["event_type"] == "SESSION_CLOSE"].iloc[0]
        self.assertEqual(close_event["last_executable_bar_utc"], "2024-01-08T06:59:00+00:00")
        self.assertEqual(result.audit["weekend_close_unverifiable_count"], 0)
        self.assertFalse((result.events["event_type"] == "WEEKEND_CLOSE_UNVERIFIABLE").any())

    def test_entry_risk_skip_does_not_consume_crosses(self) -> None:
        m1, _, h1 = synthetic_frames()
        m1.loc[:4, "low"] = 90.0
        m5, active_h1 = reaggregate(m1)
        h1 = pd.concat([h1.iloc[:5], active_h1], ignore_index=True)
        config = BacktestConfig(
            start="2024-01-08T06:00:00Z", end="2024-01-08T06:12:00Z",
            contract_size_oz=100, max_stop_distance_usd=8,
            macd_fast=1, macd_slow=2, macd_signal=2,
        )
        m1_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m1-risk")]
        m5_cross = [(pd.Timestamp("2024-01-08T06:05:00Z").value, "LONG", "m5-risk")]
        with patch("research.xauusd_trailing.engine._cross_events", side_effect=[m1_cross, m5_cross]):
            result = run_backtest(m1, m5, h1, config)
        skipped = result.events[result.events["event_type"] == "ENTRY_SKIPPED_RISK"]
        self.assertGreaterEqual(len(skipped), 1)
        self.assertEqual(skipped.iloc[0]["reason"], "MAX_STOP_DISTANCE_EXCEEDED")
        self.assertFalse(skipped.iloc[0]["crosses_consumed"])
        self.assertTrue(result.events[result.events["event_type"] == "ENTRY_FILLED"].empty)


if __name__ == "__main__":
    unittest.main()
