from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from research.xauusd_trailing.demo_runner import DemoStrategyRunner
from research.xauusd_trailing.models import BacktestConfig
from research.xauusd_trailing.runtime import (
    ExclusiveRunnerLock, RuntimeRecorder, classify_health, process_probe,
    read_record, run_recorded, write_record,
)
from scripts.xauusd_watchdog import check_heartbeat, record_check

UTC = timezone.utc


class RuntimeAuditTests(unittest.TestCase):
    def make_runtime(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime = RuntimeRecorder(directory / "runtime")
        runtime.start(directory / "xauusd_demo_test.jsonl")
        return runtime

    def test_zero_exit_without_user_stop_is_abnormal(self):
        runtime = self.make_runtime()
        self.assertEqual(run_recorded(runtime, lambda: None), 1)
        saved = read_record(runtime.runtime_path)
        self.assertEqual(saved["phase"], "ABNORMAL_EXIT")
        self.assertEqual(saved["exit_reason"], "UNEXPECTED_CLEAN_EXIT")
        self.assertFalse(saved["automatic_restart"])

    def test_cutoff_exit_is_not_mistaken_for_manual_stop(self):
        runtime = self.make_runtime()
        self.assertEqual(run_recorded(runtime, lambda: "ENTRY_CUTOFF_AND_NO_OPEN_STRATEGY_POSITIONS"), 1)
        self.assertEqual(read_record(runtime.runtime_path)["classification"], "ANOMALY")

    def test_manual_stop_requires_confirmation_and_current_run(self):
        runtime = self.make_runtime()
        with self.assertRaises(ValueError):
            runtime.request_manual_stop(confirmed=False)
        runtime.request_manual_stop(confirmed=True)
        self.assertTrue(runtime.manual_stop_requested())
        self.assertEqual(run_recorded(runtime, lambda: "USER_MANUAL_STOP"), 0)
        saved = read_record(runtime.runtime_path)
        self.assertEqual(saved["phase"], "MANUAL_STOPPED")
        result = classify_health(saved, runtime.control(), None, now=datetime.now(UTC), max_age_seconds=180)
        self.assertEqual(result["event"], "WATCHDOG_MANUAL_STOP")
        self.assertFalse(runtime.start_allowed())
        replacement = RuntimeRecorder(runtime.directory)
        with self.assertRaises(RuntimeError):
            replacement.start(Path("unused.jsonl"))
        with self.assertRaises(ValueError):
            runtime.clear_manual_stop(confirmed=False)
        runtime.clear_manual_stop(confirmed=True)
        self.assertTrue(runtime.start_allowed())
        result = classify_health(saved, runtime.control(), None, now=datetime.now(UTC), max_age_seconds=180)
        self.assertEqual(result["event"], "WATCHDOG_MANUAL_STOP")

    def test_start_cannot_replace_live_runner_audit(self):
        runtime = self.make_runtime()
        before = read_record(runtime.runtime_path)
        replacement = RuntimeRecorder(runtime.directory)
        with self.assertRaises(RuntimeError):
            replacement.start(Path("unused.jsonl"))
        self.assertEqual(read_record(runtime.runtime_path), before)

    def test_explicit_start_records_previous_unrecorded_exit(self):
        runtime = self.make_runtime()
        previous = read_record(runtime.runtime_path)
        previous["process_id"] = 0
        write_record(runtime.runtime_path, previous)
        replacement = RuntimeRecorder(runtime.directory)
        replacement.start(runtime.directory / "new.jsonl")
        rows = [json.loads(line) for line in runtime.events_path.read_text().splitlines()]
        abandoned = [row for row in rows if row["event"] == "RUNTIME_PREVIOUS_EXIT_UNRECORDED"]
        self.assertEqual(len(abandoned), 1)
        self.assertEqual(abandoned[0]["run_id"], runtime.run_id)
        self.assertEqual(abandoned[0]["classification"], "ANOMALY")
        self.assertFalse(abandoned[0]["exit_time_known"])

    def test_forged_or_old_stop_reason_does_not_make_exit_normal(self):
        runtime = self.make_runtime()
        write_record(runtime.control_path, {
            "desired_state": "STOPPED", "manual_confirmation": True,
            "target_run_id": "old-run", "request_id": "old-request",
        })
        self.assertFalse(runtime.manual_stop_requested())
        self.assertEqual(runtime.finish("USER_MANUAL_STOP", 0), 1)

    def test_keyboard_interrupt_without_stop_record_is_abnormal(self):
        runtime = self.make_runtime()
        def interrupt():
            raise KeyboardInterrupt
        self.assertEqual(run_recorded(runtime, interrupt), 130)
        self.assertEqual(read_record(runtime.runtime_path)["classification"], "ANOMALY")

    def test_system_exit_zero_is_abnormal(self):
        runtime = self.make_runtime()
        def terminate():
            raise SystemExit(0)
        self.assertEqual(run_recorded(runtime, terminate), 1)

    def test_exception_records_call_sites_without_secret_message(self):
        runtime = self.make_runtime()
        def fail():
            raise RuntimeError("secret-api-key-must-not-appear")
        self.assertEqual(run_recorded(runtime, fail), 1)
        saved = read_record(runtime.runtime_path)
        self.assertEqual(saved["error_type"], "RuntimeError")
        self.assertTrue(saved["stack_locations"])
        self.assertNotIn("secret-api-key", runtime.runtime_path.read_text())
        self.assertNotIn("secret-api-key", runtime.events_path.read_text())

    def test_single_runner_lock_released_without_deleting_state(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        state = root / "state.json"
        state.write_text('{"entries": 4}')
        with ExclusiveRunnerLock(state):
            with self.assertRaises(RuntimeError):
                with ExclusiveRunnerLock(state):
                    self.fail("second runner accepted")
        with ExclusiveRunnerLock(state):
            self.assertEqual(json.loads(state.read_text())["entries"], 4)

    def test_native_read_only_process_probe_detects_self_and_invalid_pid(self):
        import os
        self.assertTrue(process_probe(os.getpid())["running"])
        self.assertFalse(process_probe(0)["running"])

    def test_runner_manual_stop_preserves_positions_and_cycle_counters(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runner = DemoStrategyRunner(
            SimpleNamespace(), BacktestConfig(), stop_new_entries_at=None,
            cycle_anchor_at=datetime(2026, 10, 1, 9, tzinfo=ZoneInfo("America/Mexico_City")),
            log_path=root / "demo.jsonl", state_path=root / "state.json",
            stop_requested=lambda: True, run_id="run",
        )
        runner._position_state = {"123": {"ticket": 123, "stop_loss": 4175.0}}
        runner.entries_by_cycle = {"cycle": 4}
        runner.consumed_crosses = {"M1:cross"}
        with patch.object(runner, "_log") as log:
            self.assertTrue(runner._manual_stop_if_requested())
        log.assert_called_once()
        self.assertEqual(runner.exit_reason, "USER_MANUAL_STOP")
        state = json.loads((root / "state.json").read_text())
        self.assertEqual(state["entries_by_cycle"]["cycle"], 4)
        self.assertEqual(state["positions"]["123"]["stop_loss"], 4175.0)
        self.assertEqual(state["consumed_crosses"], ["M1:cross"])

    def test_cli_manual_stop_gate_attempts_no_mt5_connection(self):
        from scripts import run_xauusd_demo_strategy as cli
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        runtime = RuntimeRecorder(root / "runtime")
        runtime.request_manual_stop(confirmed=True)
        argv = ["demo", "--confirm-demo-strategy", "--log-dir", str(root), "--state-file", str(root / "state.json")]
        with patch("sys.argv", argv), patch.object(cli, "Mt5DemoAdapter") as adapter, patch("builtins.print"):
            self.assertEqual(cli.main(), 0)
        adapter.assert_not_called()


class WatchdogClassificationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 2, 5, 0, tzinfo=UTC)
        self.runtime = {
            "run_id": "current", "process_id": 123, "process_start_token": "created-a",
            "phase": "RUNNING", "started_at_utc": (self.now - timedelta(minutes=10)).isoformat(),
        }
        self.control = {"desired_state": "RUNNING"}
        self.heartbeat = {"run_id": "current", "process_id": 123,
                          "time_utc": self.now.isoformat(), "terminal_connected": True}
        self.probe = lambda pid: {"running": True, "start_token": "created-a"}

    def check(self, **overrides):
        return classify_health(
            overrides.get("runtime", self.runtime), overrides.get("control", self.control),
            overrides.get("heartbeat", self.heartbeat), now=self.now, max_age_seconds=180,
            probe=overrides.get("probe", self.probe),
        )

    def test_dead_process_is_abnormal_even_with_fresh_heartbeat(self):
        result = self.check(probe=lambda pid: {"running": False})
        self.assertEqual(result["reason"], "PROCESS_NOT_RUNNING")

    def test_reused_pid_cannot_masquerade_as_running_strategy(self):
        result = self.check(probe=lambda pid: {"running": True, "start_token": "created-b"})
        self.assertEqual(result["reason"], "PROCESS_ID_REUSED_OR_UNVERIFIABLE")

    def test_missing_creation_identity_and_wrong_heartbeat_pid_are_alerts(self):
        self.assertEqual(self.check(runtime={**self.runtime, "process_start_token": None})["event"], "WATCHDOG_ALERT")
        result = self.check(heartbeat={**self.heartbeat, "process_id": 456})
        self.assertEqual(result["reason"], "HEARTBEAT_PROCESS_ID_MISMATCH")

    def test_stale_heartbeat_is_abnormal_when_process_exists(self):
        heartbeat = {**self.heartbeat, "time_utc": (self.now - timedelta(seconds=181)).isoformat()}
        self.assertEqual(self.check(heartbeat=heartbeat)["reason"], "HEARTBEAT_STALE")

    def test_new_run_cannot_use_old_runs_fresh_heartbeat(self):
        heartbeat = {**self.heartbeat, "run_id": "old"}
        self.assertEqual(self.check(heartbeat=heartbeat)["reason"], "NO_HEARTBEAT_FOR_CURRENT_RUN")
        startup = {**self.runtime, "started_at_utc": self.now.isoformat()}
        self.assertEqual(self.check(runtime=startup, heartbeat=heartbeat)["event"], "WATCHDOG_STARTING")

    def test_unconfirmed_force_majeure_is_not_automatically_exempt(self):
        self.assertEqual(self.check(runtime={**self.runtime, "phase": "FORCE_MAJEURE"})["event"], "WATCHDOG_ALERT")

    def test_manual_stop_requested_after_death_cannot_erase_anomaly(self):
        control = {"desired_state": "STOPPED", "manual_confirmation": True, "target_run_id": "current"}
        self.assertEqual(self.check(control=control, probe=lambda pid: {"running": False})["reason"], "PROCESS_NOT_RUNNING")

    def test_disconnect_and_lockout_are_alerts_not_no_signal(self):
        for fields, reason in (({"terminal_connected": False}, "MT5_DISCONNECTED"),
                               ({"entry_lockout": True}, "RUNTIME_ENTRY_LOCKOUT"),
                               ({"position_query_error_type": "RuntimeError"}, "MT5_QUERY_FAILED")):
            with self.subTest(reason=reason):
                self.assertEqual(self.check(heartbeat={**self.heartbeat, **fields})["reason"], reason)

    def test_risk_halt_and_session_break_do_not_stop_monitoring(self):
        for state in ("RISK_HALT_MANAGING_ONLY", "RUNNING_STALE_QUOTE", "MANAGING_ONLY"):
            with self.subTest(state=state):
                heartbeat = {**self.heartbeat, "runner_state": state, "risk_halt": True}
                self.assertEqual(self.check(heartbeat=heartbeat)["event"], "WATCHDOG_OK")

    def test_future_heartbeat_and_unacknowledged_stop_are_alerts(self):
        heartbeat = {**self.heartbeat, "time_utc": (self.now + timedelta(seconds=1)).isoformat()}
        self.assertEqual(self.check(heartbeat=heartbeat)["reason"], "HEARTBEAT_IN_FUTURE")
        control = {"desired_state": "STOPPED", "target_run_id": "current",
                   "requested_at_utc": (self.now - timedelta(seconds=181)).isoformat()}
        self.assertEqual(self.check(control=control)["reason"], "MANUAL_STOP_NOT_ACKNOWLEDGED")

    def test_corrupt_runtime_is_alert_and_never_starts_process(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "runtime").mkdir()
        (root / "runtime/runtime.json").write_text("invalid")
        with patch("subprocess.Popen") as launch:
            result = check_heartbeat(root, now=self.now)
        launch.assert_not_called()
        self.assertEqual(result["reason"], "RUNTIME_AUDIT_UNREADABLE")

    def test_legacy_dead_pid_is_alert_and_repeated_alert_is_not_new_incident(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        log = root / "xauusd_demo_old.jsonl"
        log.write_text(json.dumps({"event": "HEARTBEAT", "time_utc": self.now.isoformat(), "process_id": 123}) + "\n")
        result = check_heartbeat(root, now=self.now, probe=lambda pid: {"running": False})
        self.assertEqual(result["reason"], "PROCESS_NOT_RUNNING")
        first = record_check(result, root / "alert.jsonl", root / "watchdog_status.json")
        second = record_check(result, root / "alert.jsonl", root / "watchdog_status.json")
        self.assertTrue(first["new_incident"])
        self.assertFalse(second["new_incident"])
        rows = [json.loads(line) for line in (root / "alert.jsonl").read_text().splitlines()]
        self.assertEqual(sum(row["event"] == "RUNTIME_ANOMALY_DETECTED" for row in rows), 1)
        self.assertFalse(first["automatic_restart"])

    def test_legacy_unknown_identity_and_malformed_lines_do_not_report_healthy(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        log = root / "xauusd_demo_old.jsonl"
        log.write_text('[]\nnull\n' + json.dumps({
            "event": "HEARTBEAT", "time_utc": self.now.isoformat(), "process_id": 123,
        }) + "\n")
        result = check_heartbeat(root, now=self.now, probe=self.probe)
        self.assertEqual(result["reason"], "LEGACY_RUNTIME_UNVERIFIABLE")
        log.write_text(json.dumps({"event": "HEARTBEAT", "time_utc": self.now.isoformat(), "process_id": "invalid"}) + "\n")
        self.assertEqual(check_heartbeat(root, now=self.now)["reason"], "PROCESS_STATE_UNVERIFIABLE")

    def test_invalid_legacy_log_encoding_is_alert_not_checker_crash(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "xauusd_demo_old.jsonl").write_bytes(b"\xff\xfe")
        self.assertEqual(check_heartbeat(root, now=self.now)["reason"], "HEARTBEAT_AUDIT_UNREADABLE")

    def test_corrupt_watchdog_status_still_leaves_alert_and_nonzero_exit(self):
        from scripts import xauusd_watchdog as cli
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        (root / "runtime").mkdir()
        (root / "runtime/watchdog_status.json").write_text("invalid")
        argv = ["watchdog", "--log-dir", str(root)]
        with patch("sys.argv", argv), patch("builtins.print"):
            self.assertEqual(cli.main(), 1)
        rows = [json.loads(line) for line in (root / "watchdog.jsonl").read_text().splitlines()]
        self.assertEqual(rows[-1]["reason"], "ALERT_WRITE_FAILED")
        self.assertFalse(rows[-1]["automatic_restart"])


if __name__ == "__main__":
    unittest.main()
