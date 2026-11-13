"""Reconnectable metadata-only preview status, cancellation and race coverage."""

from contextlib import closing
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_inventory import capacity
from utils.installation_backup_preview import PreviewStatuses, PreviewStatusError


class PreviewStatusTests(unittest.TestCase):
    def test_polling_does_not_fabricate_progress_and_terminal_elapsed_stops(self):
        now = [100.0]
        statuses = PreviewStatuses(clock=lambda: now[0])
        progress = statuses.begin("fixture")
        progress.checkpoint(force=True, stage="scanning", entries=12, logical_bytes=42)
        now[0] = 130.0
        polled = statuses.snapshot()
        self.assertEqual(polled["elapsed_seconds"], 30)
        self.assertEqual(polled["last_progress_at"], 100)
        self.assertEqual(polled["updated_at"], 100)
        statuses.seal(progress.identifier)
        statuses.finish(progress.identifier, "completed", result={"expires_at": 1000})
        now[0] = 170.0
        self.assertEqual(statuses.snapshot()["elapsed_seconds"], 30)

    def test_closed_cancel_window_cannot_cancel_a_later_operation(self):
        statuses = PreviewStatuses()
        first = statuses.begin("fixture")
        statuses.seal(first.identifier)
        self.assertFalse(statuses.cancel(first.identifier)["cancel_requested"])
        second = statuses.begin("other")
        statuses.finish(first.identifier, "completed", result={})
        self.assertEqual(statuses.snapshot()["preview_id"], second.identifier)
        self.assertEqual(statuses.snapshot(first.identifier)["status"], "completed")
        self.assertFalse(statuses.snapshot(second.identifier)["cancel_requested"])

    def test_finished_retention_is_bounded_and_expires_without_io(self):
        now = [100.0]
        statuses = PreviewStatuses(clock=lambda: now[0], ttl=20, maximum=2)
        identifiers = []
        for _ in range(4):
            progress = statuses.begin("fixture")
            identifiers.append(progress.identifier)
            statuses.seal(progress.identifier)
            statuses.finish(progress.identifier, "completed", result={})
        with self.assertRaises(PreviewStatusError):
            statuses.snapshot(identifiers[0])
        self.assertEqual(len(statuses._records), 2)
        now[0] = 121.0
        self.assertIsNone(statuses.snapshot())
        with self.assertRaises(PreviewStatusError):
            statuses.cancel(identifiers[-1])

    def test_throttled_checkpoint_still_acknowledges_cancellation(self):
        statuses = PreviewStatuses()
        progress = statuses.begin("fixture")
        with patch("utils.installation_backup_preview.time.monotonic", return_value=0):
            progress.checkpoint(entries=100)
        self.assertEqual(statuses.snapshot()["entries"], 0)
        statuses.cancel(progress.identifier)
        from utils.installation_backup_preview import PreviewCancelled

        with self.assertRaises(PreviewCancelled):
            progress.checkpoint(entries=101)


class ManagerPreviewStatusTests(unittest.TestCase):
    join_worker = fixtures.ManagerTests.join_worker

    def setUp(self):
        fixtures.ManagerTests.setUp(self)
        self.scan_release = threading.Event()
        self.scan_threads = []
        self.addCleanup(self.join_scans)

    def join_scans(self):
        self.scan_release.set()
        for thread in self.scan_threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def start_scan(self, action=None):
        results, errors = [], []

        def scan():
            try:
                results.append(
                    action()
                    if action is not None
                    else self.manager.preview({"destination_id": "fixture"})
                )
            except Exception as error:
                errors.append(error)

        thread = threading.Thread(target=scan)
        self.scan_threads.append(thread)
        thread.start()
        return thread, results, errors

    def test_real_completed_preview_reconnects_with_exact_counts_and_original_result(
        self,
    ):
        original = deepcopy(self.config.config)
        self.assertIsNone(self.manager.preview_status()["preview"])
        preview = self.manager.preview({"destination_id": "fixture"})
        operation = self.manager.preview_status()["preview"]
        self.assertEqual(operation["status"], "completed")
        self.assertEqual(operation["result"], preview)
        self.assertEqual(operation["entries"], preview["capacity"]["source"]["entries"])
        self.assertEqual(
            operation["logical_bytes"], preview["capacity"]["source"]["logical_bytes"]
        )
        self.assertEqual(operation["roots_completed"], operation["root_count"])
        self.assertIsNone(operation["current_root"])
        self.assertEqual(operation["expires_at"], preview["expires_at"])
        self.assertEqual(
            self.manager.preview_status()["controller_session"], self.manager._session
        )
        self.assertEqual(self.handler.events, [])
        self.assertEqual(self.config.config, original)
        self.assertFalse(self.manager.state_root.joinpath("active.json").exists())
        self.assertEqual(
            list(self.manager.workspace_root.glob("preview/.installation-manifest-*")),
            [],
        )
        late = self.manager.cancel_preview(operation["preview_id"])["preview"]
        self.assertEqual(late["status"], "completed")
        self.assertFalse(late["cancel_requested"])
        self.assertIn(preview["preview_token"], self.manager._previews)

    def test_live_status_and_cancelling_remain_available_until_scan_cleanup(self):
        entered = threading.Event()

        def held(*args, progress, **kwargs):
            progress(force=True, stage="scanning", entries=7, logical_bytes=123)
            entered.set()
            self.assertTrue(self.scan_release.wait(5))
            progress(entries=8)
            return capacity(*args, progress=progress, **kwargs)

        with patch("utils.installation_backup.capacity", held):
            thread, results, errors = self.start_scan()
            self.assertTrue(entered.wait(5))
            status = self.manager.preview_status()
            operation = status["preview"]
            self.assertEqual(operation["status"], "running")
            self.assertEqual(operation["entries"], 7)
            self.assertEqual(operation["logical_bytes"], 123)
            cancelling = self.manager.cancel_preview(operation["preview_id"])["preview"]
            self.assertEqual(cancelling["status"], "cancelling")
            self.assertIsNone(cancelling["completed_at"])
            self.assertIsNone(cancelling["result"])
            self.assertTrue(thread.is_alive())
            self.assertFalse(self.manager._preview_reader.acquire(blocking=False))
            self.scan_release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], InstallationBackupError)
        self.assertEqual(
            self.manager.preview_status()["preview"]["status"], "cancelled"
        )
        self.assertEqual(self.handler.events, [])
        self.assertEqual(self.manager._previews, {})
        self.assertFalse(self.manager.state_root.joinpath("active.json").exists())
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        self.manager._preview_reader.release()
        self.assertEqual(
            list(self.manager.workspace_root.glob("preview/.installation-manifest-*")),
            [],
        )

    def test_cancel_race_discards_a_token_not_yet_published_to_the_caller(self):
        entered = threading.Event()
        original = self.manager._preview_record
        token = []

        def held(*args):
            result = original(*args)
            token.append(result["preview_token"])
            entered.set()
            self.assertTrue(self.scan_release.wait(5))
            return result

        with patch.object(self.manager, "_preview_record", held):
            thread, results, errors = self.start_scan()
            self.assertTrue(entered.wait(5))
            identifier = self.manager.preview_status()["preview"]["preview_id"]
            self.manager.cancel_preview(identifier)
            self.scan_release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertIsInstance(errors[0], InstallationBackupError)
        self.assertNotIn(token[0], self.manager._previews)
        operation = self.manager.preview_status(identifier)["preview"]
        self.assertEqual(operation["status"], "cancelled")
        self.assertIsNone(operation["result"])
        self.assertEqual(self.handler.events, [])

    def test_consumed_review_cannot_be_resurrected_by_status_polling(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        identifier = self.manager.preview_status()["preview"]["preview_id"]
        self.manager.start(
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "secrets": True,
                    "external_state": True,
                },
            }
        )
        self.join_worker()
        operation = self.manager.preview_status(identifier)["preview"]
        self.assertEqual(operation["status"], "completed")
        self.assertIsNone(operation["result"])
        self.assertIn("used or expired", operation["message"])
        self.assertNotIn(preview["preview_token"], self.manager._previews)

    def test_failure_releases_the_reader_and_does_not_publish_private_exception(self):
        with patch.object(
            self.manager, "_backup_preview", side_effect=RuntimeError("private-marker")
        ):
            with self.assertRaises(RuntimeError):
                self.manager.preview({"destination_id": "fixture"})
        operation = self.manager.preview_status()["preview"]
        self.assertEqual(operation["status"], "failed")
        self.assertNotIn("private-marker", repr(operation))
        self.assertIsNone(operation["result"])
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        self.manager._preview_reader.release()
        self.assertEqual(self.handler.events, [])

    def test_duplicate_preview_does_not_replace_running_status(self):
        entered = threading.Event()

        def held(*args, progress, **kwargs):
            entered.set()
            self.assertTrue(self.scan_release.wait(5))
            return capacity(*args, progress=progress, **kwargs)

        with patch("utils.installation_backup.capacity", held):
            thread, _results, errors = self.start_scan()
            self.assertTrue(entered.wait(5))
            identifier = self.manager.preview_status()["preview"]["preview_id"]
            with self.assertRaisesRegex(InstallationBackupError, "already inspecting"):
                self.manager.preview({"destination_id": "fixture"})
            self.assertEqual(
                self.manager.preview_status()["preview"]["preview_id"], identifier
            )
            self.scan_release.set()
            thread.join(timeout=10)
        self.assertEqual(errors, [])

    def test_cancellation_removes_a_real_mid_census_ledger_before_terminal_status(self):
        from utils import installation_backup_inventory as census

        entered = threading.Event()
        paused = []
        original = census.source_rows

        def slow_rows(*args, **kwargs):
            with closing(original(*args, **kwargs)) as rows:
                for row in rows:
                    yield row
                    if not paused:
                        paused.append(True)
                        entered.set()
                        self.assertTrue(self.scan_release.wait(5))

        with patch.object(census, "source_rows", slow_rows):
            thread, results, errors = self.start_scan()
            self.assertTrue(entered.wait(5))
            ledgers = list(
                self.manager.workspace_root.glob("preview/.installation-manifest-*")
            )
            self.assertEqual(len(ledgers), 1)
            self.assertTrue(ledgers[0].joinpath("entries.sqlite").exists())
            identifier = self.manager.preview_status()["preview"]["preview_id"]
            self.manager.cancel_preview(identifier)
            self.assertEqual(
                self.manager.preview_status()["preview"]["status"], "cancelling"
            )
            self.assertTrue(ledgers[0].exists())
            self.scan_release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results, [])
        self.assertIsInstance(errors[0], InstallationBackupError)
        self.assertEqual(
            self.manager.preview_status()["preview"]["status"], "cancelled"
        )
        self.assertFalse(ledgers[0].exists())
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        self.manager._preview_reader.release()
        self.assertEqual(self.handler.events, [])

    def backup_request(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def test_confirmed_preparation_is_visible_until_durable_job_handoff(self):
        request = self.backup_request()
        entered = threading.Event()
        handoffs = []
        original = self.manager._admit

        def held(*args, **kwargs):
            entered.set()
            self.assertTrue(self.scan_release.wait(5))
            return capacity(*args, **kwargs)

        def observed_admit(*args, **kwargs):
            result = original(*args, **kwargs)
            handoffs.append(self.manager.status())
            return result

        with (
            patch("utils.installation_backup.capacity", held),
            patch.object(self.manager, "_admit", observed_admit),
        ):
            thread, results, errors = self.start_scan(
                lambda: self.manager.start(request)
            )
            self.assertTrue(entered.wait(5))
            status = self.manager.status()
            self.assertIsNone(status["job"])
            self.assertEqual(status["preparation"]["stage"], "checking_capacity")
            self.assertIsInstance(status["preparation"]["started_at"], (int, float))
            self.assertIn("before stopping services", status["preparation"]["message"])
            status["preparation"]["stage"] = "caller-must-not-mutate-controller"
            self.assertEqual(
                self.manager.status()["preparation"]["stage"], "checking_capacity"
            )
            self.assertEqual(self.handler.events, [])
            self.assertFalse(self.manager.state_root.joinpath("active.json").exists())
            self.scan_release.set()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)
        self.assertEqual(len(handoffs), 1)
        self.assertIsNotNone(handoffs[0]["preparation"])
        self.assertEqual(handoffs[0]["job"]["job_id"], results[0]["job"]["job_id"])
        self.assertEqual(handoffs[0]["job"]["job_type"], "installation_backup")
        self.assertIsNone(self.manager.status()["preparation"])
        self.join_worker()

    def test_preparation_clears_on_capacity_refusal_and_census_failure(self):
        from utils.installation_backup_archive import InstallationArchiveError

        request = self.backup_request()
        for failure in ("insufficient", "exception"):
            with self.subTest(failure=failure):

                def refused(*_args, **_kwargs):
                    self.assertIsNotNone(self.manager.status()["preparation"])
                    if failure == "exception":
                        raise InstallationArchiveError("fixture census failed")
                    return {"status": "insufficient"}, []

                expected = (
                    InstallationArchiveError
                    if failure == "exception"
                    else InstallationBackupError
                )
                with patch("utils.installation_backup.capacity", refused):
                    with self.assertRaises(expected):
                        self.manager.start(request)
                self.assertIsNone(self.manager.status()["preparation"])
                self.assertIsNone(self.manager.status()["job"])
                self.assertEqual(self.handler.events, [])
                self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
                self.manager._preview_reader.release()

    def test_invalid_confirmation_never_marks_preparation_or_scans(self):
        request = self.backup_request()
        request["confirmation"] = "invalid"
        with patch(
            "utils.installation_backup.capacity",
            side_effect=AssertionError("Invalid requests must not scan"),
        ):
            with self.assertRaises(InstallationBackupError):
                self.manager.start(request)
        self.assertIsNone(self.manager.status()["preparation"])
        self.assertEqual(self.handler.events, [])

    def test_unknown_preview_identity_requires_a_fresh_review(self):
        with self.assertRaisesRegex(InstallationBackupError, "unavailable or expired"):
            self.manager.preview_status("a" * 32)
        with self.assertRaisesRegex(InstallationBackupError, "unavailable or expired"):
            self.manager.cancel_preview("a" * 32)


if __name__ == "__main__":
    unittest.main()
