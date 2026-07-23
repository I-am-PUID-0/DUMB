import unittest
from unittest.mock import patch

from utils.plex_status import PlexStatusCollector


def _config(enabled=True, interval_sec=300):
    return {
        "dumb": {
            "metrics": {
                "plex_status": {
                    "enabled": enabled,
                    "interval_sec": interval_sec,
                }
            }
        }
    }


class PlexStatusCollectorTests(unittest.TestCase):
    def setUp(self):
        self.collector = PlexStatusCollector()
        self.payload = {
            "page": {"updated_at": "2026-07-23T12:00:00Z"},
            "status": {
                "indicator": "minor",
                "description": "Minor Service Outage",
            },
            "components": [
                {
                    "id": "auth",
                    "name": "Authentication and API server (plex.tv)",
                    "status": "partial_outage",
                },
                {
                    "id": "web",
                    "name": "Hosted web app (app.plex.tv)",
                    "status": "operational",
                },
            ],
            "incidents": [
                {
                    "id": "incident-1",
                    "name": "Login delays",
                    "status": "investigating",
                    "impact": "minor",
                    "updated_at": "2026-07-23T12:00:00Z",
                    "shortlink": "https://stspg.io/example",
                    "components": [
                        {
                            "name": "Authentication and API server (plex.tv)",
                            "status": "partial_outage",
                        }
                    ],
                }
            ],
            "scheduled_maintenances": [],
        }

    def test_disabled_metric_does_not_fetch(self):
        with patch.object(self.collector, "_fetch") as fetch:
            result = self.collector.snapshot(_config(enabled=False))

        fetch.assert_not_called()
        self.assertFalse(result["enabled"])
        self.assertEqual(result["indicator"], "disabled")

    def test_normalizes_summary_to_compact_metric(self):
        with patch.object(self.collector, "_fetch", return_value=self.payload):
            result = self.collector.snapshot(
                _config(), refresh_if_stale=True, wait_for_refresh=True
            )

        self.assertTrue(result["enabled"])
        self.assertTrue(result["available"])
        self.assertFalse(result["operational"])
        self.assertEqual(result["indicator"], "minor")
        self.assertEqual(result["component_status_counts"]["operational"], 1)
        self.assertEqual(result["component_status_counts"]["partial_outage"], 1)
        self.assertEqual(
            result["affected_components"][0]["name"],
            "Authentication and API server (plex.tv)",
        )
        self.assertEqual(result["active_incidents"][0]["name"], "Login delays")

    def test_uses_cached_sample_until_interval_expires(self):
        with patch.object(self.collector, "_fetch", return_value=self.payload) as fetch:
            with patch("utils.plex_status.time.monotonic", return_value=100.0):
                first = self.collector.snapshot(
                    _config(), refresh_if_stale=True, wait_for_refresh=True
                )
            with patch("utils.plex_status.time.monotonic", return_value=120.0):
                second = self.collector.snapshot(
                    _config(), refresh_if_stale=True, wait_for_refresh=True
                )

        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(first["stale"])
        self.assertEqual(second["cache_age_sec"], 20.0)

    def test_refresh_failure_preserves_last_sample_as_stale(self):
        with patch.object(self.collector, "_fetch", return_value=self.payload):
            self.collector.snapshot(
                _config(), refresh_if_stale=True, wait_for_refresh=True
            )
        self.collector.invalidate()

        with patch.object(self.collector, "_fetch", side_effect=OSError("private")):
            result = self.collector.snapshot(
                _config(), refresh_if_stale=True, wait_for_refresh=True
            )

        self.assertTrue(result["available"])
        self.assertTrue(result["stale"])
        self.assertIn("last successful", result["error"])
        self.assertNotIn("private", result["error"])

        follow_up = self.collector.snapshot(
            _config(), refresh_if_stale=True, wait_for_refresh=True
        )
        self.assertTrue(follow_up["stale"])
        self.assertIn("last successful", follow_up["error"])

    def test_first_failure_returns_safe_unavailable_result(self):
        with patch.object(self.collector, "_fetch", side_effect=OSError("private")):
            result = self.collector.snapshot(
                _config(), refresh_if_stale=True, wait_for_refresh=True
            )

        self.assertFalse(result["available"])
        self.assertEqual(result["indicator"], "unavailable")
        self.assertNotIn("private", result["error"])

    def test_failed_initial_fetch_respects_retry_interval(self):
        with patch.object(
            self.collector, "_fetch", side_effect=OSError("private")
        ) as fetch:
            with patch("utils.plex_status.time.monotonic", return_value=100.0):
                self.collector.snapshot(
                    _config(), refresh_if_stale=True, wait_for_refresh=True
                )
            with patch("utils.plex_status.time.monotonic", return_value=120.0):
                result = self.collector.snapshot(
                    _config(), refresh_if_stale=True, wait_for_refresh=True
                )

        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(result["available"])
        self.assertFalse(result["refreshing"])

    def test_incident_shortlinks_require_https(self):
        event = {"shortlink": "javascript:alert(1)"}
        self.assertIsNone(self.collector._normalize_event(event)["shortlink"])

    def test_interval_is_bounded(self):
        self.assertEqual(self.collector._interval(1), 60)
        self.assertEqual(self.collector._interval(99999), 3600)
        self.assertEqual(self.collector._interval("invalid"), 300)


if __name__ == "__main__":
    unittest.main()
