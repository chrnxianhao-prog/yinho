from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from research.xauusd_trailing.runtime import RuntimeRecorder, classify_health, process_probe, read_record, write_record
from research.xauusd_trailing.supervision import RecoveryConfig, RecoverySupervisor, launch_runner
from scripts.xauusd_watchdog import check_heartbeat

UTC = timezone.utc


class RecoverySupervisorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.now = datetime(2026, 10, 2, 6, tzinfo=UTC)
        self.config = RecoveryConfig(
            workspace=str(self.root), python_executable=sys.executable, env_path=str(self.root / ".env"),
            terminal_path=str(self.root / "terminal64.exe"), strategy_config=str(self.root / "config.yaml"),
            log_dir=str(self.root / "logs"), state_file=str(self.root / "state.json"),
            runtime_dir=str(self.root / "runtime"), enabled=True, demo_only_confirmed=True,
        )
        self.runtime = RuntimeRecorder(Path(self.config.runtime_dir))
        self.ledger = {"run_id": "old", "process_id": 123, "process_start_token": "old-token", "phase": "RUNNING"}
        write_record(self.runtime.runtime_path, self.ledger)
        write_record(Path(self.config.state_file), {"entries_by_cycle": {"cycle": 4}, "entry_lockout": True})
        self.launch = Mock(return_value={"launcher_pid": 456, "launcher_start_token": "new-token"})
        self.terminal = Mock(return_value=True)
        self.health = Mock(return_value={"event": "WATCHDOG_ALERT", "reason": "PROCESS_NOT_RUNNING"})
        self.probe = Mock(return_value={"running": False, "start_token": None})
        self.supervisor = RecoverySupervisor(self.config, self.health, probe=self.probe,
                                             terminal_ready=self.terminal, launch=self.launch)

    def poll(self, seconds=0):
        return self.supervisor.poll_once(now=self.now + timedelta(seconds=seconds))

    def test_dead_runner_restarts_without_changing_strategy_state(self):
        before = Path(self.config.state_file).read_bytes()
        self.assertEqual(self.poll()["action"], "RESTART_REQUESTED")
        self.launch.assert_called_once_with(self.config)
        self.assertEqual(Path(self.config.state_file).read_bytes(), before)
        self.assertTrue(read_record(self.supervisor.status_path)["result"]["automatic_restart"])

    def test_recorded_abnormal_exit_and_zero_exit_are_recovered(self):
        write_record(self.runtime.runtime_path, {**self.ledger, "phase": "ABNORMAL_EXIT", "exit_code": 0})
        self.assertEqual(self.poll()["action"], "RESTART_REQUESTED")

    def test_manual_stop_blocks_recovery_before_any_process_or_terminal_access(self):
        self.runtime.request_manual_stop(confirmed=True)
        for seconds in (0, 900, 10000):
            self.assertEqual(self.poll(seconds)["action"], "MANUAL_STOP_HELD")
        self.launch.assert_not_called()
        self.probe.assert_not_called()
        self.terminal.assert_not_called()
        self.assertEqual(self.runtime.control()["desired_state"], "STOPPED")

    def test_explicit_resume_enables_recovery_but_never_clears_lockout(self):
        self.runtime.request_manual_stop(confirmed=True)
        self.poll()
        self.runtime.clear_manual_stop(confirmed=True)
        self.assertEqual(self.poll()["action"], "RESTART_REQUESTED")
        self.assertTrue(read_record(Path(self.config.state_file))["entry_lockout"])

    def test_live_healthy_runner_is_not_restarted_or_killed(self):
        self.probe.return_value = {"running": True, "start_token": "old-token"}
        self.health.return_value = {"event": "WATCHDOG_OK"}
        with patch("os.kill") as kill:
            self.assertEqual(self.poll()["action"], "HEALTHY")
        self.launch.assert_not_called()
        kill.assert_not_called()

    def test_live_stale_disconnected_and_locked_runner_is_alert_only(self):
        self.probe.return_value = {"running": True, "start_token": "old-token"}
        for reason in ("HEARTBEAT_STALE", "MT5_DISCONNECTED", "RUNTIME_ENTRY_LOCKOUT"):
            self.health.return_value = {"event": "WATCHDOG_ALERT", "reason": reason}
            self.assertEqual(self.poll()["action"], "LIVE_PROCESS_ALERT")
        self.launch.assert_not_called()

    def test_unverifiable_process_is_blocked(self):
        self.probe.return_value = {"running": None}
        self.assertEqual(self.poll()["action"], "BLOCKED")
        self.launch.assert_not_called()

    def test_verified_pid_reuse_recovers_old_runner_without_touching_new_owner(self):
        self.probe.return_value = {"running": True, "start_token": "unrelated-owner"}
        self.assertEqual(self.poll()["action"], "RESTART_REQUESTED")
        self.assertTrue(self.poll(1)["previous_pid_reused"])
        self.assertEqual(self.launch.call_count, 1)

    def test_missing_terminal_window_does_not_initialize_or_launch_hidden_mt5(self):
        self.terminal.return_value = False
        self.assertEqual(self.poll()["action"], "WAIT_MT5")
        self.launch.assert_not_called()

    def test_missing_or_corrupt_strategy_state_cannot_reset_counts(self):
        Path(self.config.state_file).unlink()
        self.assertEqual(self.poll()["action"], "BLOCKED")
        Path(self.config.state_file).write_text("invalid")
        with self.assertRaises(ValueError):
            self.poll()
        self.launch.assert_not_called()

    def test_launch_failure_backoff_is_persistent_across_supervisor_restart(self):
        self.launch.side_effect = OSError("do-not-log-secret")
        self.assertEqual(self.poll()["action"], "LAUNCH_FAILED")
        replacement = RecoverySupervisor(self.config, self.health, probe=self.probe,
                                         terminal_ready=self.terminal, launch=self.launch)
        self.assertEqual(replacement.poll_once(now=self.now + timedelta(seconds=59))["action"], "BACKOFF")
        self.assertEqual(self.launch.call_count, 1)
        self.assertEqual(replacement.poll_once(now=self.now + timedelta(seconds=60))["action"], "LAUNCH_FAILED")
        self.assertEqual(replacement.poll_once(now=self.now + timedelta(seconds=179))["action"], "BACKOFF")
        self.assertNotIn("do-not-log-secret", self.supervisor.events_path.read_text())

    def test_pending_launch_prevents_duplicate_and_reports_stalled_start(self):
        self.poll()
        self.probe.side_effect = lambda pid: {"running": pid == 456, "start_token": "new-token" if pid == 456 else None}
        self.assertEqual(self.poll(60)["action"], "STARTING")
        self.assertEqual(self.poll(181)["reason"], "LAUNCH_STALLED_NO_RUN_AUDIT")
        self.assertEqual(self.launch.call_count, 1)

    def test_manual_stop_in_restart_handoff_has_priority(self):
        def stop_and_guard(path):
            self.runtime.request_manual_stop(confirmed=True)
            return True
        self.supervisor.terminal_ready = stop_and_guard
        self.assertEqual(self.poll()["action"], "MANUAL_STOP_HELD")
        self.launch.assert_not_called()

    def test_global_stop_intent_is_accepted_by_new_run_during_handoff(self):
        request = self.runtime.request_manual_stop(confirmed=True)
        self.runtime.run_id = "new-run"
        write_record(self.runtime.runtime_path, {**self.ledger, "run_id": "new-run"})
        self.assertNotEqual(request["target_run_id"], self.runtime.run_id)
        self.assertTrue(self.runtime.manual_stop_requested())
        self.assertEqual(self.runtime.finish("USER_MANUAL_STOP", 0), 0)
        self.assertEqual(read_record(self.runtime.runtime_path)["phase"], "MANUAL_STOPPED")

    def test_pre_upgrade_manual_stop_acknowledgement_remains_valid(self):
        self.runtime.run_id = "old"
        self.runtime.request_manual_stop(confirmed=True)
        self.runtime.finish("USER_MANUAL_STOP", 0)
        saved = read_record(self.runtime.runtime_path)
        saved["manual_stop_acknowledgement"].pop("accepted_run_id")
        result = classify_health(saved, self.runtime.control(), None, now=self.now, max_age_seconds=180)
        self.assertEqual(result["event"], "WATCHDOG_MANUAL_STOP")

    def test_configuration_requires_explicit_demo_permission_and_valid_intervals(self):
        for changes in ({"enabled": False}, {"demo_only_confirmed": False}, {"poll_seconds": float("nan")},
                        {"retry_base_seconds": 0}, {"env_path": "relative.env"}):
            with self.assertRaises(ValueError):
                replace(self.config, **changes)

    def test_launcher_uses_safe_argument_list_no_shell_no_reset_and_retains_cutoff(self):
        config = replace(self.config, stop_new_entries_at="2026-10-02T09:00:00-06:00")
        with patch("subprocess.Popen", return_value=Mock(pid=456)) as popen, \
             patch("research.xauusd_trailing.supervision.process_probe", return_value={"start_token": "new"}):
            result = launch_runner(config)
        arguments = popen.call_args.args[0]
        self.assertIn("--confirm-demo-strategy", arguments)
        self.assertIn("--state-file", arguments)
        self.assertIn("--stop-new-entries-at", arguments)
        self.assertNotIn("--reset-lockout", arguments)
        self.assertNotIn("--reset-risk-halt", arguments)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(result["launcher_pid"], 456)

    def test_real_isolated_process_crash_restarts_and_manual_stop_stays_stopped(self):
        source_root = Path(__file__).resolve().parents[1]
        fake_script = self.root / "scripts/run_xauusd_demo_strategy.py"
        fake_script.parent.mkdir()
        shutil.copyfile(source_root / "tests/fixtures/xauusd_recovery_probe.py", fake_script)
        write_record(Path(self.config.env_path), {"crash": False})
        write_record(self.runtime.runtime_path, {**self.ledger, "process_id": 0})
        config = replace(self.config, retry_base_seconds=0.1, retry_max_seconds=0.2)
        before = Path(self.config.state_file).read_bytes()
        real_health = lambda: check_heartbeat(Path(config.log_dir), runtime_dir=Path(config.runtime_dir))
        supervisor = RecoverySupervisor(config, real_health, terminal_ready=lambda path: True)

        def wait_for(predicate):
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline:
                if predicate():
                    return
                time.sleep(0.05)
            self.fail("isolated recovery probe did not reach expected state")

        with patch.dict(os.environ, {"XAUUSD_TEST_CODE_ROOT": str(source_root)}):
            try:
                self.assertEqual(supervisor.poll_once()["action"], "RESTART_REQUESTED")
                wait_for(lambda: real_health()["event"] == "WATCHDOG_OK")
                first = read_record(self.runtime.runtime_path)
                self.assertEqual(supervisor.poll_once()["action"], "HEALTHY")
                write_record(Path(config.env_path), {"crash": True})
                wait_for(lambda: not process_probe(first["process_id"])["running"])
                self.assertEqual(read_record(self.runtime.runtime_path)["phase"], "ABNORMAL_EXIT")
                write_record(Path(config.env_path), {"crash": False})
                self.assertEqual(supervisor.poll_once()["action"], "RESTART_REQUESTED")
                wait_for(lambda: real_health()["event"] == "WATCHDOG_OK")
                second = read_record(self.runtime.runtime_path)
                self.assertNotEqual(first["run_id"], second["run_id"])
                self.runtime.request_manual_stop(confirmed=True)
                wait_for(lambda: not process_probe(second["process_id"])["running"])
                self.assertEqual(read_record(self.runtime.runtime_path)["phase"], "MANUAL_STOPPED")
                self.assertEqual(supervisor.poll_once()["action"], "MANUAL_STOP_HELD")
                self.assertEqual(Path(config.state_file).read_bytes(), before)
            finally:
                self.runtime.request_manual_stop(confirmed=True)
                saved = read_record(self.runtime.runtime_path)
                if saved and saved.get("process_id"):
                    wait_for(lambda: not process_probe(saved["process_id"])["running"])


if __name__ == "__main__":
    unittest.main()
