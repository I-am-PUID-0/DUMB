import threading
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

import schedule

from api.api_state import APIState
from utils.auto_update import Update


class AutoUpdateSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.threads = []
        self.releases = []
        self.handlers = []
        self.patches = []
        for name, value in {
            "_scheduler": schedule.Scheduler(),
            "_jobs": {},
            "_next_check_at": {},
            "_symlink_backup_jobs": {},
            "_symlink_backup_next_at": {},
            "_api_update_job": None,
            "_api_next_check_at": None,
            "_active_scheduled_work": {},
            "_schedule_thread_started": False,
            "_schedule_thread_count": 0,
        }.items():
            item = patch.object(Update, name, value)
            item.start()
            self.patches.append(item)
        item = patch("utils.auto_update.Downloader")
        item.start()
        self.patches.append(item)
        original_thread = threading.Thread

        def new_thread(*args, **kwargs):
            thread = original_thread(*args, **kwargs)
            self.threads.append(thread)
            return thread

        item = patch("utils.auto_update.threading.Thread", side_effect=new_thread)
        item.start()
        self.patches.append(item)

    def tearDown(self):
        try:
            for handler in self.handlers:
                handler.shutting_down = True
            for event in self.releases:
                event.set()
            for thread in self.threads:
                if thread.ident is not None:
                    thread.join(timeout=3)
                    self.assertFalse(thread.is_alive(), thread.name)
            self.assertFalse(Update._active_scheduled_work)
        finally:
            for item in reversed(self.patches):
                item.stop()

    def updater(self):
        handler = SimpleNamespace(logger=Mock(), shutting_down=False)
        self.handlers.append(handler)
        updater = Update(handler)
        updater._write_update_status = Mock()
        updater._safe_record_update_status = Mock()
        updater._safe_record_symlink_backup_status = Mock()
        return updater

    def blocked_callback(self):
        started = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def callback():
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("Test callback was not released")

        return started, release, callback

    def join(self, thread):
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive(), thread.name)

    def test_instances_share_dedicated_scheduler_and_only_one_runner(self):
        first = self.updater()
        second = self.updater()
        self.assertIs(first.scheduler, second.scheduler)
        self.assertIsNot(first.scheduler, schedule.default_scheduler)
        ticked = threading.Event()
        first.scheduler.run_pending = Mock(side_effect=ticked.set)

        first._ensure_scheduler_running("First")
        self.assertTrue(ticked.wait(timeout=1))
        second._ensure_scheduler_running("Second")

        self.assertEqual(1, len(self.threads))
        self.assertEqual(1, Update._schedule_thread_count)

    def test_runner_start_failure_allows_later_retry(self):
        updater = self.updater()
        with patch("utils.auto_update.threading.Thread") as thread:
            thread.return_value.start.side_effect = OSError("cannot start")
            with self.assertRaises(OSError):
                updater._ensure_scheduler_running("First")
        self.assertFalse(Update._schedule_thread_started)
        self.assertEqual(0, Update._schedule_thread_count)
        ticked = threading.Event()
        updater.scheduler.run_pending = Mock(side_effect=ticked.set)
        updater._ensure_scheduler_running("Retry")
        self.assertTrue(ticked.wait(timeout=1))
        self.assertEqual(1, Update._schedule_thread_count)

    def test_update_registration_from_both_instances_uses_dispatch_wrappers(self):
        first = self.updater()
        second = self.updater()
        for updater, name in ((first, "First"), (second, "Second")):
            updater._ensure_scheduler_running = Mock()
            updater._calculate_next_check_at = Mock(return_value=200)
            updater.update_schedule(name, {"auto_update": True}, "radarr", None)

        self.assertEqual(2, len(first.scheduler.jobs))
        self.assertEqual({"First", "Second"}, set(Update._jobs))
        for job in first.scheduler.jobs:
            self.assertEqual(
                "_dispatch_scheduled_update_if_due", job.job_func.func.__name__
            )

    def test_blocked_update_leaves_api_and_backup_jobs_progressing_without_queue(self):
        updater = self.updater()
        started, release, callback = self.blocked_callback()
        api_ran = threading.Event()
        backup_ran = threading.Event()
        jobs = [
            updater.scheduler.every(1).seconds.do(
                updater._dispatch_scheduled_work, "update", "Slow", callback
            ),
            updater.scheduler.every(1).seconds.do(
                updater._dispatch_scheduled_work, "api", "DUMB API", api_ran.set
            ),
            updater.scheduler.every(1).seconds.do(
                updater._dispatch_scheduled_work,
                "symlink_backup",
                "Backup",
                backup_ran.set,
            ),
        ]
        for job in jobs:
            job.next_run = datetime.now() - timedelta(seconds=1)
        tick_finished = threading.Event()

        def tick():
            updater.scheduler.run_pending()
            tick_finished.set()

        tick_thread = threading.Thread(target=tick)
        tick_thread.start()
        self.assertTrue(tick_finished.wait(timeout=1))
        self.assertTrue(started.wait(timeout=1))
        self.assertTrue(api_ran.wait(timeout=1))
        self.assertTrue(backup_ran.wait(timeout=1))
        worker = Update._active_scheduled_work[("update", "Slow")]["thread"]
        thread_count = len(self.threads)

        for _ in range(50):
            self.assertFalse(
                updater._dispatch_scheduled_work("update", "Slow", callback)
            )
            self.assertFalse(
                updater._dispatch_scheduled_work("update", "Other", Mock())
            )

        self.assertEqual(thread_count, len(self.threads))
        self.assertTrue(worker.is_alive())
        payload = updater._write_update_status.call_args.args[1]
        self.assertEqual("deferred", payload["status"])
        self.assertEqual(60, payload["retry_after_seconds"])
        release.set()
        self.join(worker)
        self.join(tick_thread)
        self.assertNotIn(("update", "Slow"), Update._active_scheduled_work)

    def test_backup_lane_serializes_colliding_custom_destinations_without_queue(self):
        updater = self.updater()
        started, release, first = self.blocked_callback()
        second = Mock()
        self.assertTrue(
            updater._dispatch_scheduled_work("symlink_backup", "One", first)
        )
        self.assertTrue(started.wait(timeout=1))
        self.assertFalse(
            updater._dispatch_scheduled_work("symlink_backup", "Two", second)
        )
        self.assertEqual(1, len(self.threads))
        self.assertEqual(1, len(Update._active_scheduled_work))
        second.assert_not_called()
        release.set()
        self.join(self.threads[0])
        self.assertTrue(
            updater._dispatch_scheduled_work("symlink_backup", "Two", second)
        )
        self.join(self.threads[-1])
        second.assert_called_once()

    def test_unexpected_worker_error_releases_slot_and_reports_failure(self):
        updater = self.updater()
        callback = Mock(side_effect=RuntimeError("failure"))
        self.assertTrue(updater._dispatch_scheduled_work("update", "Failed", callback))
        self.join(self.threads[-1])
        self.assertFalse(Update._active_scheduled_work)
        self.assertEqual(
            "error", updater._safe_record_update_status.call_args.args[1]["status"]
        )
        self.assertTrue(updater._dispatch_scheduled_work("update", "Following", Mock()))
        self.join(self.threads[-1])

    def test_thread_start_failure_stops_reporting_before_registry_removal(self):
        updater = self.updater()
        captured = []

        def failed_start():
            captured.append(Update._active_scheduled_work[("update", "Failed")])
            raise OSError("cannot start")

        with patch("utils.auto_update.threading.Thread") as thread:
            thread.return_value.start.side_effect = failed_start
            self.assertFalse(
                updater._dispatch_scheduled_work("update", "Failed", Mock())
            )

        self.assertFalse(captured[0]["reporting"])
        self.assertFalse(Update._active_scheduled_work)
        self.assertEqual(
            "error", updater._safe_record_update_status.call_args.args[1]["status"]
        )

    def test_tick_and_progress_report_errors_do_not_stop_runner(self):
        updater = self.updater()
        calls = []

        def tick():
            calls.append(True)
            if len(calls) == 1:
                raise RuntimeError("bad scheduled callback")
            updater.process_handler.shutting_down = True

        updater.scheduler.run_pending = Mock(side_effect=tick)
        updater._report_running_scheduled_work = Mock(
            side_effect=RuntimeError("bad report")
        )
        with patch("utils.auto_update.time.sleep"):
            updater._run_scheduler_loop()

        self.assertEqual(2, len(calls))
        self.assertEqual(2, updater._report_running_scheduled_work.call_count)
        self.assertGreaterEqual(updater.logger.error.call_count, 3)

    def test_busy_admission_is_deferred_without_entering_install(self):
        from utils.infinidysk_migration_admission import (
            INFINIDYSK_MIGRATION_ADMISSION_LOCK,
        )

        updater = self.updater()
        updater._scheduled_update_check_admitted = Mock()
        acquired = threading.Event()
        release = threading.Event()
        self.releases.append(release)

        def hold_admission():
            with INFINIDYSK_MIGRATION_ADMISSION_LOCK:
                acquired.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_admission)
        holder.start()
        self.assertTrue(acquired.wait(timeout=1))
        with patch("utils.auto_update.INSTALL_CACHE") as cache:
            result = updater.scheduled_update_check("Radarr", {}, "radarr", None)
        self.assertEqual("deferred", result["status"])
        self.assertEqual("update_admission_busy", result["reason"])
        updater._scheduled_update_check_admitted.assert_not_called()
        cache.begin_operation.assert_not_called()
        release.set()
        self.join(holder)

    def test_busy_updater_creates_no_operation_and_preserves_active_id(self):
        updater = self.updater()
        updater._active_install_operation = "existing-operation"
        updater.updating.acquire()
        try:
            with patch("utils.auto_update.INSTALL_CACHE") as cache:
                result = updater._scheduled_update_check_unprotected(
                    "Radarr", {}, "radarr", None
                )
            self.assertEqual("deferred", result["status"])
            self.assertEqual("existing-operation", updater._active_install_operation)
            cache.begin_operation.assert_not_called()
        finally:
            updater.updating.release()

    def test_busy_updater_does_not_begin_planned_media_protection(self):
        updater = self.updater()
        updater._manual_update_check_internal = Mock(
            return_value={"status": "update_available"}
        )
        updater.media_protection_manager = Mock()
        updater.updating.acquire()
        try:
            result = updater._scheduled_update_check_admitted(
                "Radarr", {}, "radarr", None
            )
            self.assertEqual("deferred", result["status"])
            updater.media_protection_manager.begin_planned.assert_not_called()
        finally:
            updater.updating.release()

    def test_lock_contender_after_media_preflight_does_not_report_success(self):
        updater = self.updater()
        updater._manual_update_check_internal = Mock(
            return_value={"status": "update_available"}
        )
        manager = updater.media_protection_manager = Mock()

        def begin(*args):
            self.assertTrue(updater.updating.acquire(blocking=False))
            return {"status": "allowed", "token": "protection-token"}

        manager.begin_planned.side_effect = begin
        try:
            with patch("utils.auto_update.INSTALL_CACHE") as cache:
                result = updater._scheduled_update_check_admitted(
                    "Radarr", {}, "radarr", None
                )
            self.assertEqual("deferred", result["status"])
            manager.complete_planned.assert_called_once_with(
                "protection-token", success=False
            )
            cache.begin_operation.assert_not_called()
        finally:
            updater.updating.release()

    def test_deferred_result_remains_visible_and_retries_after_one_minute(self):
        updater = self.updater()
        updater._calculate_next_check_at = Mock(return_value=1000)
        updater.scheduled_update_check = Mock(
            return_value={"status": "deferred", "reason": "update_admission_busy"}
        )
        config = {"auto_update": True}
        Update._next_check_at["Radarr"] = 100
        with (
            patch("utils.auto_update.CONFIG_MANAGER.get_instance", return_value=config),
            patch("utils.auto_update.time.time", return_value=100),
        ):
            updater._run_scheduled_update_if_due("Radarr", config, "radarr", None)
        payload = updater._safe_record_update_status.call_args.args[1]
        self.assertEqual("deferred", payload["status"])
        self.assertEqual("deferred", payload["scheduler_state"])
        self.assertEqual(160, payload["next_check_at"])
        self.assertEqual(160, Update._next_check_at["Radarr"])

    def test_worker_rereads_disabled_config_and_cancels_job(self):
        updater = self.updater()
        updater.scheduled_update_check = Mock()
        job = updater.scheduler.every(1).minutes.do(Mock())
        Update._jobs["Radarr"] = job
        Update._next_check_at["Radarr"] = 100
        with patch(
            "utils.auto_update.CONFIG_MANAGER.get_instance",
            return_value={"auto_update": False},
        ):
            updater._run_scheduled_update_if_due(
                "Radarr", {"auto_update": True}, "radarr", None
            )
        updater.scheduled_update_check.assert_not_called()
        self.assertNotIn("Radarr", Update._jobs)
        self.assertNotIn("Radarr", Update._next_check_at)
        self.assertEqual(
            "disabled", updater._safe_record_update_status.call_args.args[1]["status"]
        )

    def test_due_time_change_ends_all_three_running_statuses_truthfully(self):
        updater = self.updater()
        updater.scheduled_update_check = Mock()
        updater.check_api_update = Mock()
        updater._run_symlink_backup = Mock()
        updater.symlink_backup_enabled = Mock(return_value=True)
        Update._next_check_at["Radarr"] = 200
        Update._api_next_check_at = 200
        Update._symlink_backup_next_at["Backup"] = 200
        with (
            patch(
                "utils.auto_update.CONFIG_MANAGER.get_instance",
                return_value={"auto_update": True},
            ),
            patch("utils.auto_update.time.time", return_value=100),
        ):
            updater._run_scheduled_update_if_due("Radarr", {}, "radarr", None)
            updater._run_api_update_check_if_due("DUMB API")
            updater._run_scheduled_symlink_backup_if_due("Backup", "riven", None)
        updater.scheduled_update_check.assert_not_called()
        updater.check_api_update.assert_not_called()
        updater._run_symlink_backup.assert_not_called()
        for call in updater._safe_record_update_status.call_args_list:
            self.assertEqual("rescheduled", call.args[1]["last_run_status"])
            self.assertEqual("completed", call.args[1]["scheduler_state"])
        self.assertEqual(
            "rescheduled",
            updater._safe_record_symlink_backup_status.call_args.args[1][
                "last_run_status"
            ],
        )

    def test_disabled_backup_cancels_obsolete_job_and_due_metadata(self):
        updater = self.updater()
        job = updater.scheduler.every(1).minutes.do(Mock())
        Update._symlink_backup_jobs["Backup"] = job
        Update._symlink_backup_next_at["Backup"] = 100
        with patch("utils.auto_update.CONFIG_MANAGER.get_instance", return_value={}):
            updater._run_scheduled_symlink_backup_if_due("Backup", "riven", None)
        self.assertNotIn("Backup", Update._symlink_backup_jobs)
        self.assertNotIn("Backup", Update._symlink_backup_next_at)
        self.assertNotIn(job, updater.scheduler.jobs)

    def test_heartbeat_bypasses_timing_buffer_and_cannot_overwrite_terminal_status(
        self,
    ):
        updater = self.updater()
        updater._safe_record_update_status = Update._safe_record_update_status.__get__(
            updater
        )
        updater._begin_update_timing("Slow")
        pending = {"status": "updated", "message": "real result"}
        updater._safe_record_update_status("Slow", pending)
        started, release, callback = self.blocked_callback()
        self.assertTrue(updater._dispatch_scheduled_work("update", "Slow", callback))
        self.assertTrue(started.wait(timeout=1))
        record = Update._active_scheduled_work[("update", "Slow")]
        record["started_monotonic"] = 100
        record["last_report_at"] = 100
        with patch("utils.auto_update.time.monotonic", return_value=460):
            updater._report_running_scheduled_work()
        payload = updater._write_update_status.call_args.args[1]
        self.assertEqual("running", payload["scheduler_state"])
        self.assertEqual(360, payload["scheduler_elapsed_seconds"])
        self.assertTrue(payload["scheduler_long_running"])
        self.assertIs(pending, updater._update_timings["Slow"]["pending_payload"])
        self.assertTrue(record["thread"].is_alive())
        updater._finish_update_timing("Slow")
        updater._finish_scheduled_work_status(
            "update", "Slow", {"status": "scheduled", "last_run_status": "updated"}
        )
        updater._write_update_status.reset_mock()
        with patch("utils.auto_update.time.monotonic", return_value=600):
            updater._report_running_scheduled_work()
        updater._write_update_status.assert_not_called()
        release.set()
        self.join(record["thread"])

    def project_api_state(self, updater):
        state = object.__new__(APIState)
        state._update_cache = {}
        state._update_cache_lock = threading.Lock()
        state._persist_project_update_status = Mock()
        updater._write_update_status = state.set_update_status
        updater._safe_record_update_status = Update._safe_record_update_status.__get__(
            updater
        )
        return state

    @patch("api.api_state.notify_event")
    def test_project_running_then_failure_retains_known_availability(self, notify):
        updater = self.updater()
        state = self.project_api_state(updater)
        for name in ("DUMB API", "DUMB Frontend"):
            state.set_update_status(
                name,
                {
                    "status": "update_available",
                    "current_version": "1.0",
                    "available_version": "1.1",
                    "message": "Version 1.1 available.",
                    "checked_at": 100,
                },
            )
            updater._publish_scheduled_work_status(
                "api",
                name,
                updater._scheduled_work_payload(
                    "api", name, "running", message="Checking releases."
                ),
            )
            running = state.get_update_status(name)
            self.assertEqual("update_available", running["status"])
            self.assertEqual("running", running["scheduler_state"])
            self.assertEqual("Checking releases.", running["scheduler_message"])
            self.assertEqual(100, running["checked_at"])
            updater._finish_scheduled_work_status(
                "api", name, {"status": "error", "message": "Network unavailable."}
            )
            result = state.get_update_status(name)
            self.assertEqual("update_available", result["status"])
            self.assertEqual("1.1", result["available_version"])
            self.assertEqual("Network unavailable.", result["last_check_error"])
            self.assertEqual("error", result["scheduler_state"])
            self.assertEqual("Version 1.1 available.", result["message"])
        self.assertEqual(
            2, sum(call.args[0] == "update.available" for call in notify.call_args_list)
        )

    @patch("api.api_state.notify_event")
    def test_project_progress_and_recheck_do_not_repeat_availability_notification(
        self, notify
    ):
        updater = self.updater()
        state = self.project_api_state(updater)
        for name in ("DUMB API", "DUMB Frontend"):
            available = {"status": "update_available", "available_version": "1.1"}
            state.set_update_status(name, available)
            for progress in ("running", "deferred"):
                updater._publish_scheduled_work_status(
                    "api",
                    name,
                    updater._scheduled_work_payload(
                        "api", name, progress, message="Scheduled progress."
                    ),
                )
                self.assertEqual(
                    "update_available", state.get_update_status(name)["status"]
                )
            updater._finish_scheduled_work_status("api", name, available)
            self.assertEqual(
                "completed", state.get_update_status(name)["scheduler_state"]
            )
        self.assertEqual(2, notify.call_count)

    @patch("api.api_state.notify_event")
    def test_service_running_then_unchanged_available_deduplicates_notification(
        self, notify
    ):
        updater = self.updater()
        state = self.project_api_state(updater)
        available = {"status": "update_available", "available_version": "1.1"}
        state.set_update_status("Pulsarr", available)
        for _ in range(2):
            updater._publish_scheduled_work_status(
                "update",
                "Pulsarr",
                updater._scheduled_work_payload("update", "Pulsarr", "running"),
            )
            updater._finish_scheduled_work_status("update", "Pulsarr", available)
        self.assertEqual(1, notify.call_count)
        self.assertEqual({}, state._scheduler_previous_update_statuses)
        state.set_update_status("Pulsarr", {**available, "available_version": "1.2"})
        self.assertEqual(2, notify.call_count)

    @patch("api.api_state.notify_event")
    def test_reschedule_during_running_check_keeps_notification_baseline(self, notify):
        updater = self.updater()
        state = self.project_api_state(updater)
        available = {"status": "update_available", "available_version": "1.1"}
        state.set_update_status("Pulsarr", available)
        updater._publish_scheduled_work_status(
            "update",
            "Pulsarr",
            updater._scheduled_work_payload("update", "Pulsarr", "running"),
        )
        updater._ensure_scheduler_running = Mock()
        updater._calculate_next_check_at = Mock(return_value=200)
        updater.update_schedule("Pulsarr", {"auto_update": True}, "pulsarr", None)
        self.assertIn("pulsarr", state._scheduler_previous_update_statuses)
        state.set_update_status("Pulsarr", {"status": "ok"})
        self.assertIn("pulsarr", state._scheduler_previous_update_statuses)
        # The admitted preflight publishes a result without scheduler metadata.
        state.set_update_status("Pulsarr", available)
        self.assertFalse(state._scheduler_previous_update_statuses)
        updater._finish_scheduled_work_status("update", "Pulsarr", available)
        self.assertEqual(1, notify.call_count)

    @patch("api.api_state.notify_event")
    def test_project_reschedule_ack_retains_availability_on_running_check_failure(
        self, notify
    ):
        updater = self.updater()
        state = self.project_api_state(updater)
        state.set_update_status(
            "DUMB Frontend",
            {
                "status": "update_available",
                "current_version": "1.0",
                "available_version": "1.1",
                "message": "Version 1.1 available.",
            },
        )
        updater._publish_scheduled_work_status(
            "update",
            "DUMB Frontend",
            updater._scheduled_work_payload("update", "DUMB Frontend", "running"),
        )
        updater._ensure_scheduler_running = Mock()
        updater._calculate_next_check_at = Mock(return_value=200)
        updater.update_schedule(
            "DUMB Frontend", {"auto_update": True}, "dumb_frontend", None
        )
        state.set_update_status("DUMB Frontend", {"status": "ok"})
        self.assertEqual(
            "update_available", state.get_update_status("DUMB Frontend")["status"]
        )
        updater._finish_scheduled_work_status(
            "update", "DUMB Frontend", {"status": "error", "message": "Check failed."}
        )
        result = state.get_update_status("DUMB Frontend")
        self.assertEqual("update_available", result["status"])
        self.assertEqual("1.1", result["available_version"])
        self.assertEqual("Check failed.", result["last_check_error"])
        self.assertEqual("error", result["scheduler_state"])
        self.assertEqual(
            1, sum(call.args[0] == "update.available" for call in notify.call_args_list)
        )
        self.assertFalse(state._scheduler_previous_update_statuses)

    @patch("api.api_state.notify_event")
    def test_project_schedule_completion_clears_running_telemetry(self, notify):
        updater = self.updater()
        state = self.project_api_state(updater)
        state.set_update_status(
            "DUMB Frontend", {"status": "blocked", "reason": "commit"}
        )
        updater._publish_scheduled_work_status(
            "update",
            "DUMB Frontend",
            updater._scheduled_work_payload("update", "DUMB Frontend", "running"),
        )
        updater._finish_scheduled_work_status(
            "update",
            "DUMB Frontend",
            {"status": "scheduled", "next_check_at": 200, "last_run_status": "blocked"},
        )
        result = state.get_update_status("DUMB Frontend")
        self.assertEqual("blocked", result["status"])
        self.assertEqual("completed", result["scheduler_state"])
        self.assertEqual(200, result["next_check_at"])

    @patch("api.api_state.notify_event")
    def test_service_heartbeat_keeps_existing_version_and_install_stage(self, notify):
        updater = self.updater()
        state = self.project_api_state(updater)
        state.set_update_status(
            "Radarr",
            {
                "status": "update_available",
                "current_version": "1.0",
                "available_version": "1.1",
                "stage": "health_check",
                "checked_at": 100,
            },
        )
        updater._publish_scheduled_work_status(
            "update",
            "Radarr",
            updater._scheduled_work_payload("update", "Radarr", "running"),
        )
        result = state.get_update_status("Radarr")
        self.assertEqual("checking", result["status"])
        self.assertEqual("health_check", result["stage"])
        self.assertEqual("1.1", result["available_version"])
        self.assertEqual(100, result["checked_at"])

    def test_shutdown_prevents_new_dispatch_and_rechecks_before_callback(self):
        updater = self.updater()
        callback = Mock()
        updater.process_handler.shutting_down = True
        self.assertFalse(updater._dispatch_scheduled_work("update", "Stop", callback))
        self.assertFalse(self.threads)
        updater.process_handler.shutting_down = False

        def begin_shutdown(*args):
            updater.process_handler.shutting_down = True

        with patch.object(
            updater, "_publish_scheduled_work_status", side_effect=begin_shutdown
        ):
            self.assertTrue(
                updater._dispatch_scheduled_work("update", "Stop", callback)
            )
            self.join(self.threads[-1])
        callback.assert_not_called()
        self.assertEqual(
            "shutting_down",
            updater._safe_record_update_status.call_args.args[1]["reason"],
        )


if __name__ == "__main__":
    unittest.main()
