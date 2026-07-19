import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch

from utils.notifications import (
    NotificationManager,
    NotificationStorageUnavailableError,
    notify_event,
)


class FakeConfigManager:
    def __init__(self, notification_config):
        self.config = {"dumb": {"notifications": notification_config}}
        self.saved = 0

    def save_config(self):
        self.saved += 1


class FakeMetricsCollector:
    def snapshot(self, **kwargs):
        return {
            "system": {
                "cpu_percent": 10,
                "mem": {"percent": 20},
                "disk": {"percent": 30},
                "inode": {"percent": 40},
            },
            "database_health": {"services": []},
        }


class MutableMetricsCollector(FakeMetricsCollector):
    def __init__(self, services):
        self.services = services

    def snapshot(self, **kwargs):
        snapshot = super().snapshot(**kwargs)
        snapshot["database_health"]["services"] = self.services
        return snapshot


class NotificationManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.logger = Mock()
        self.config = {
            "enabled": True,
            "monitor_interval_sec": 30,
            "history_retention_days": 30,
            "max_attempts": 3,
            "retry_base_sec": 1,
            "destinations": [
                {
                    "id": "ops",
                    "name": "Operations",
                    "enabled": True,
                    "provider": "webhook",
                    "url": "https://example.invalid/hook",
                    "verify_tls": True,
                    "headers": {"Authorization": "secret"},
                    "minimum_severity": "warning",
                    "event_types": ["service.start.failed", "recovery"],
                    "service_names": [],
                    "cooldown_sec": 0,
                    "send_recovery": True,
                }
            ],
            "thresholds": {
                "cpu_percent": 85,
                "memory_percent": 85,
                "disk_percent": 90,
                "inode_percent": 90,
                "database_pressure": "high",
                "duration_sec": 0,
            },
        }
        self.config_manager = FakeConfigManager(self.config)
        self.config_patch = patch(
            "utils.notifications.CONFIG_MANAGER", self.config_manager
        )
        self.config_patch.start()
        self.manager = NotificationManager(
            process_handler=Mock(),
            metrics_collector=FakeMetricsCollector(),
            logger=self.logger,
            base_dir=self.temp_dir.name,
        )

    def tearDown(self):
        self.manager.shutdown()
        self.config_patch.stop()
        self.temp_dir.cleanup()

    def test_redacted_config_hides_destination_secrets(self):
        result = self.manager.get_config(redact=True)
        destination = result["destinations"][0]

        self.assertEqual(destination["url"], "")
        self.assertEqual(destination["headers"], {})
        self.assertTrue(destination["url_configured"])
        self.assertTrue(destination["headers_configured"])

    def test_update_config_preserves_blank_existing_secrets(self):
        payload = self.manager.get_config(redact=True)
        payload["destinations"][0]["name"] = "Renamed"

        result = self.manager.update_config(payload)

        persisted = self.config_manager.config["dumb"]["notifications"]
        self.assertEqual(
            persisted["destinations"][0]["url"],
            "https://example.invalid/hook",
        )
        self.assertEqual(
            persisted["destinations"][0]["headers"],
            {"Authorization": "secret"},
        )
        self.assertEqual(result["destinations"][0]["name"], "Renamed")
        self.assertEqual(self.config_manager.saved, 1)

    def test_service_filters_reject_services_that_are_not_enabled(self):
        self.config_manager.config["radarr"] = {
            "enabled": True,
            "process_name": "Radarr",
        }
        self.config_manager.config["sonarr"] = {
            "enabled": False,
            "process_name": "Sonarr",
        }
        payload = self.manager.get_config(redact=True)
        payload["destinations"][0]["service_names"] = ["Radarr", "Sonarr"]

        with self.assertRaisesRegex(ValueError, "currently enabled services: Sonarr"):
            self.manager.update_config(payload)

    def test_redacted_config_drops_filters_for_disabled_services(self):
        self.config_manager.config["radarr"] = {
            "enabled": True,
            "process_name": "Radarr",
        }
        self.config_manager.config["sonarr"] = {
            "enabled": False,
            "process_name": "Sonarr",
        }
        self.config["destinations"][0]["service_names"] = ["Radarr", "Sonarr"]

        result = self.manager.get_config(redact=True)

        self.assertEqual(result["destinations"][0]["service_names"], ["Radarr"])

    def test_routing_filters_by_severity_and_event_type(self):
        info = self.manager.emit("service.start.failed", "info", "Info", "Ignored")
        unrelated = self.manager.emit("update.failed", "critical", "Update", "Ignored")
        matching = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Queued"
        )

        self.assertEqual(info, [])
        self.assertEqual(unrelated, [])
        self.assertEqual(len(matching), 1)
        self.assertEqual(self.manager.get_delivery(matching[0])["status"], "queued")

    @patch("utils.notifications.requests.post")
    def test_webhook_delivery_records_success_without_exposing_url(self, post):
        post.return_value.raise_for_status.return_value = None
        delivery_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]

        self.manager._deliver_due()

        result = self.manager.get_delivery(delivery_id)
        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["attempts"], 1)
        self.assertNotIn("example.invalid", str(result))
        post.assert_called_once()

    @patch("utils.notifications.requests.post")
    def test_global_disable_pauses_regular_queue_but_allows_forced_test(self, post):
        post.return_value.raise_for_status.return_value = None
        regular_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]
        self.config["enabled"] = False
        forced_id = self.manager.emit(
            "manual",
            "info",
            "Test",
            "Details",
            destination_ids=["ops"],
            force=True,
        )[0]

        self.manager._deliver_due()

        self.assertEqual(self.manager.get_delivery(regular_id)["status"], "queued")
        self.assertEqual(self.manager.get_delivery(forced_id)["status"], "sent")

    @patch("utils.notifications.requests.post")
    def test_manual_send_skips_disabled_destination(self, post):
        self.config["destinations"][0]["enabled"] = False

        queued = self.manager.send_manual(
            "Operator message", "Details", destination_ids=["ops"]
        )

        self.assertEqual(queued, [])
        post.assert_not_called()

    @patch("utils.notifications.requests.post")
    def test_queued_delivery_is_deferred_when_destination_is_disabled(self, post):
        delivery_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]
        self.config["destinations"][0]["enabled"] = False

        self.manager._deliver_due()

        delivery = self.manager.get_delivery(delivery_id)
        self.assertEqual(delivery["status"], "queued")
        self.assertEqual(delivery["attempts"], 0)
        self.assertGreater(delivery["next_attempt_at"], time.time())
        post.assert_not_called()

    @patch("utils.notifications.requests.post")
    def test_explicit_test_can_use_disabled_destination(self, post):
        post.return_value.raise_for_status.return_value = None
        self.config["destinations"][0]["enabled"] = False
        delivery_id = self.manager.emit(
            "manual",
            "info",
            "Test",
            "Details",
            destination_ids=["ops"],
            force=True,
            include_disabled_destinations=True,
        )[0]

        self.manager._deliver_due()

        self.assertEqual(self.manager.get_delivery(delivery_id)["status"], "sent")
        post.assert_called_once()

    @patch("utils.notifications.requests.post")
    def test_queued_delivery_survives_manager_recreation(self, post):
        post.return_value.raise_for_status.return_value = None
        delivery_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]
        replacement = NotificationManager(
            process_handler=Mock(),
            metrics_collector=FakeMetricsCollector(),
            logger=self.logger,
            base_dir=self.temp_dir.name,
        )

        replacement._deliver_due()

        self.assertEqual(replacement.get_delivery(delivery_id)["status"], "sent")
        replacement.shutdown()

    def test_locked_storage_is_nonfatal_and_recovers_in_background(self):
        with (
            patch.object(
                NotificationManager,
                "_initialize_storage_once",
                side_effect=sqlite3.OperationalError("database is locked"),
            ) as initialize,
            patch("utils.notifications.time.sleep"),
        ):
            locked_manager = NotificationManager(
                process_handler=Mock(),
                metrics_collector=FakeMetricsCollector(),
                logger=self.logger,
                base_dir=self.temp_dir.name,
            )

            self.assertFalse(locked_manager._storage_ready)
            self.assertEqual(initialize.call_count, 4)
            self.assertEqual(
                locked_manager.emit(
                    "service.start.failed", "critical", "Failure", "Details"
                ),
                [],
            )
            with self.assertRaises(NotificationStorageUnavailableError):
                locked_manager.send_manual("Manual", "Details")

            initialize.side_effect = None
            self.assertTrue(locked_manager._ensure_storage_ready(force=True))
            self.assertTrue(locked_manager._storage_ready)

        self.logger.warning.assert_any_call(
            "Notification SQLite storage is locked after %s attempt(s). "
            "DUMB startup will continue and notification storage will retry "
            "in the background: %s",
            4,
            "database is locked",
        )
        self.logger.info.assert_any_call(
            "Notification SQLite storage recovered; queued delivery "
            "and history are available again."
        )

    def test_disabled_notifications_do_not_delay_startup_with_lock_retries(self):
        self.config["enabled"] = False
        with (
            patch.object(
                NotificationManager,
                "_initialize_storage_once",
                side_effect=sqlite3.OperationalError("database is locked"),
            ) as initialize,
            patch("utils.notifications.time.sleep") as sleep,
        ):
            manager = NotificationManager(
                process_handler=Mock(),
                metrics_collector=FakeMetricsCollector(),
                logger=self.logger,
                base_dir=self.temp_dir.name,
            )

        self.assertFalse(manager._storage_ready)
        self.assertEqual(initialize.call_count, 1)
        sleep.assert_not_called()

    def test_automatic_notification_lock_does_not_break_originating_operation(self):
        with (
            patch(
                "utils.notifications.get_notification_manager",
                return_value=self.manager,
            ),
            patch.object(
                self.manager,
                "emit",
                side_effect=sqlite3.OperationalError("database table is locked"),
            ),
        ):
            result = notify_event(
                "service.start.failed",
                "critical",
                "Failure",
                "Details",
                service_name="Example Service",
            )

        self.assertEqual(result, [])
        self.assertFalse(self.manager._storage_ready)
        self.logger.warning.assert_called_with(
            "Notification SQLite storage became locked. Delivery and history "
            "are paused while background recovery continues: %s",
            "database table is locked",
        )

    def test_cooldown_records_suppressed_delivery(self):
        self.config["destinations"][0]["cooldown_sec"] = 300
        first = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )
        second = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(
            {entry["status"] for entry in self.manager.history()},
            {"queued", "suppressed"},
        )

    @patch("utils.notifications.requests.post")
    def test_failed_delivery_retries_then_becomes_terminal(self, post):
        post.side_effect = RuntimeError("request failed")
        delivery_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]

        self.manager._deliver_due()
        first = self.manager.get_delivery(delivery_id)
        self.assertEqual(first["status"], "retrying")
        self.assertEqual(first["attempts"], 1)

        with self.manager._db_lock, self.manager._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET next_attempt_at = ? WHERE id = ?",
                (time.time() - 1, delivery_id),
            )
        self.manager._deliver_due()
        with self.manager._db_lock, self.manager._connect() as connection:
            connection.execute(
                "UPDATE deliveries SET next_attempt_at = ? WHERE id = ?",
                (time.time() - 1, delivery_id),
            )
        self.manager._deliver_due()

        terminal = self.manager.get_delivery(delivery_id)
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["attempts"], 3)

    def test_apprise_delivery_uses_embedded_library(self):
        client = Mock()
        client.add.return_value = True
        client.notify.return_value = True
        fake_apprise = types.SimpleNamespace(
            AppriseAsset=Mock(return_value=object()),
            Apprise=Mock(return_value=client),
            NotifyType=types.SimpleNamespace(
                INFO="info", SUCCESS="success", WARNING="warning", FAILURE="failure"
            ),
        )
        self.config["destinations"][0].update(
            provider="apprise", url="discord://token", event_types=[]
        )
        delivery_id = self.manager.emit(
            "service.start.failed", "critical", "Failure", "Details"
        )[0]

        with patch.dict(sys.modules, {"apprise": fake_apprise}):
            self.manager._deliver_due()

        self.assertEqual(self.manager.get_delivery(delivery_id)["status"], "sent")
        client.add.assert_called_once_with("discord://token")
        client.notify.assert_called_once()

    def test_webhook_configuration_rejects_non_http_url(self):
        payload = self.manager.get_config(redact=True)
        payload["destinations"][0]["url"] = "file:///etc/passwd"

        with self.assertRaisesRegex(ValueError, "Invalid webhook URL"):
            self.manager.update_config(payload)

    def test_condition_emits_once_and_sends_recovery(self):
        self.config["destinations"][0]["event_types"] = [
            "resource.cpu.high",
            "recovery",
        ]
        self.manager._condition(
            "resource:cpu",
            True,
            0,
            "resource.cpu.high",
            "warning",
            "CPU high",
            "CPU is high",
            value=95,
        )
        self.manager._condition(
            "resource:cpu",
            True,
            0,
            "resource.cpu.high",
            "warning",
            "CPU high",
            "CPU is high",
            value=96,
        )
        self.manager._condition(
            "resource:cpu",
            False,
            0,
            "resource.cpu.high",
            "warning",
            "CPU high",
            "CPU is high",
            value=50,
        )

        history = self.manager.history()
        self.assertEqual(len(history), 2)
        self.assertEqual(
            {entry["event_type"] for entry in history},
            {"resource.cpu.high", "recovery"},
        )

    def test_disabled_database_monitor_clears_latched_condition(self):
        self.config["destinations"][0]["event_types"] = ["database.pressure"]
        collector = MutableMetricsCollector(
            [
                {
                    "monitoring_enabled": True,
                    "process_name": "Plex",
                    "pressure": "high",
                    "databases": [],
                }
            ]
        )
        self.manager.metrics_collector = collector

        self.manager._collect_monitored_conditions(self.config)
        collector.services = []
        self.manager._collect_monitored_conditions(self.config)
        self.assertNotIn("database:Plex", self.manager._conditions)
        collector.services = [
            {
                "monitoring_enabled": True,
                "process_name": "Plex",
                "pressure": "high",
                "databases": [],
            }
        ]
        self.manager._collect_monitored_conditions(self.config)

        database_events = [
            entry
            for entry in self.manager.history()
            if entry["event_type"] == "database.pressure"
        ]
        self.assertEqual(len(database_events), 2)


if __name__ == "__main__":
    unittest.main()
