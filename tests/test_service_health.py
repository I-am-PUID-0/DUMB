import json
import unittest
from unittest.mock import Mock, patch

from utils.service_health import ServiceHealthMonitor


class FakeRaw:
    def __init__(self, body):
        self.body = body

    def read(self, amount, decode_content=True):
        return self.body[:amount]


class FakeResponse:
    def __init__(self, status_code=200, body="", content_type="text/plain"):
        self.status_code = status_code
        self.raw = FakeRaw(body.encode("utf-8"))
        self.headers = {"content-type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ServiceHealthMonitorTests(unittest.TestCase):
    def setUp(self):
        self.monitor = ServiceHealthMonitor(cache_ttl_seconds=0)

    @patch("utils.service_health.requests.request")
    def test_nzbdav_plain_healthy_response(self, request):
        request.return_value = FakeResponse(body="Healthy")

        result = self.monitor.check(
            "nzbdav",
            "NzbDAV",
            {"backend_port": 8080},
            process_identity=123,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["details"]["endpoint"], "/health")
        request.assert_called_once()

    @patch("utils.service_health.requests.request")
    def test_nzbdav_migration_response_is_starting_not_unhealthy(self, request):
        request.return_value = FakeResponse(
            status_code=503,
            body=json.dumps({"status": "migrating"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "nzbdav",
            "NzbDAV",
            {"backend_port": 8080},
            process_identity=123,
        )

        self.assertEqual(result["status"], "starting")
        self.assertTrue(result["healthy"])
        self.assertIn("migrating", result["reason"])
        self.assertEqual(result["details"]["http_status"], 503)

    @patch("utils.service_health.requests.request")
    def test_structured_degraded_health_preserves_component_statuses(self, request):
        request.return_value = FakeResponse(
            body=json.dumps(
                {
                    "status": "Degraded",
                    "entries": {
                        "database": {"status": "Healthy"},
                        "providerPool": {"status": "Degraded"},
                    },
                }
            ),
            content_type="application/json",
        )

        result = self.monitor.check(
            "nzbdav",
            "NzbDAV",
            {"backend_port": 8080},
        )

        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["healthy"])
        self.assertIn("providerPool", result["reason"])
        self.assertEqual(
            result["details"]["components"],
            [
                {"name": "database", "status": "healthy"},
                {"name": "providerPool", "status": "degraded"},
            ],
        )

    @patch("utils.service_health.requests.request")
    def test_unhealthy_application_response_is_restart_worthy(self, request):
        request.return_value = FakeResponse(
            status_code=503,
            body=json.dumps({"status": "Unhealthy"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "nzbdav",
            "NzbDAV",
            {"backend_port": 8080},
        )

        self.assertEqual(result["status"], "unhealthy")
        self.assertFalse(result["healthy"])
        self.assertIn("Unhealthy", result["reason"])

    @patch("utils.service_health.requests.request")
    def test_unknown_reported_state_is_visible_as_degraded(self, request):
        request.return_value = FakeResponse(
            body=json.dumps({"status": "maintenance_window"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "nzbdav",
            "NzbDAV",
            {"backend_port": 8080},
        )

        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["healthy"])
        self.assertIn("maintenance_window", result["reason"])

    @patch("utils.service_health.requests.request")
    def test_missing_optional_endpoint_degrades_without_triggering_restart(
        self, request
    ):
        request.return_value = FakeResponse(status_code=404, body="Not Found")

        result = self.monitor.check(
            "jellyfin",
            "Jellyfin Media Server",
            {"port": 8096},
        )

        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["healthy"])
        self.assertFalse(result["details"]["supported"])

    @patch("utils.service_health.requests.request")
    def test_plex_identity_xml_marker_is_healthy(self, request):
        request.return_value = FakeResponse(
            body=(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<MediaContainer size="0" apiVersion="1.2.7.0" />'
            ),
            content_type="application/xml",
        )

        result = self.monitor.check(
            "plex",
            "Plex Media Server",
            {"port": 32400},
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertIsNone(result["reason"])
        self.assertNotIn("reported_status", result["details"])

    @patch("utils.service_health.requests.request")
    def test_plex_identity_without_marker_remains_degraded(self, request):
        request.return_value = FakeResponse(
            body="<html><body>Unexpected response</body></html>",
            content_type="text/html",
        )

        result = self.monitor.check(
            "plex",
            "Plex Media Server",
            {"port": 32400},
        )

        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["details"]["validation"], "identity marker missing")

    @patch("utils.service_health.requests.request")
    def test_pgadmin_ping_response_is_healthy(self, request):
        request.return_value = FakeResponse(body="PING")

        result = self.monitor.check(
            "pgadmin",
            "pgAdmin4",
            {"port": 5050},
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["details"]["reported_status"], "PING")

    @patch("utils.service_health.requests.request")
    def test_rclone_uses_local_rc_endpoint_and_post(self, request):
        request.return_value = FakeResponse(
            body=json.dumps({"version": "v1.74.4"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "rclone",
            "Rclone w/ NzbDAV",
            {
                "command": [
                    "rclone",
                    "mount",
                    "nzbdav:",
                    "/mnt/debrid/nzbdav",
                    "--rc",
                    "--rc-addr",
                    "127.0.0.1:5572",
                    "--rc-no-auth",
                ]
            },
        )

        self.assertEqual(result["status"], "healthy")
        _, url = request.call_args.args
        self.assertEqual(url, "http://127.0.0.1:5572/core/version")
        self.assertEqual(request.call_args.args[0], "POST")

    @patch("utils.service_health.requests.request")
    def test_rclone_without_rc_server_has_no_application_probe(self, request):
        result = self.monitor.check(
            "rclone",
            "Rclone w/ RealDebrid",
            {
                "command": [
                    "rclone",
                    "mount",
                    "realdebrid:",
                    "/mnt/debrid/realdebrid",
                ]
            },
        )

        self.assertIsNone(result)
        request.assert_not_called()

    @patch("utils.service_health.requests.request")
    def test_rclone_explicitly_disabled_rc_has_no_application_probe(self, request):
        disabled_commands = (
            ["rclone", "mount", "remote:", "/mnt/remote", "--rc=false"],
            ["rclone", "mount", "remote:", "/mnt/remote", "--rc", "false"],
        )
        for command in disabled_commands:
            with self.subTest(command=command):
                result = self.monitor.check(
                    "rclone",
                    "Rclone",
                    {"command": command},
                )
                self.assertIsNone(result)

        request.assert_not_called()

    @patch("utils.service_health.requests.request")
    def test_rclone_explicitly_enabled_rc_uses_application_probe(self, request):
        request.return_value = FakeResponse(
            body=json.dumps({"version": "v1.74.4"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "rclone",
            "Rclone",
            {
                "command": [
                    "rclone",
                    "mount",
                    "remote:",
                    "/mnt/remote",
                    "--rc=true",
                    "--rc-addr=127.0.0.1:5572",
                ]
            },
        )

        self.assertEqual(result["status"], "healthy")
        request.assert_called_once()

    @patch("utils.service_health.requests.request")
    def test_probe_results_are_cached_per_process_identity(self, request):
        request.return_value = FakeResponse(body="Healthy")
        monitor = ServiceHealthMonitor(cache_ttl_seconds=60)
        config = {"backend_port": 8080}

        first = monitor.check("nzbdav", "NzbDAV", config, process_identity=123)
        second = monitor.check("nzbdav", "NzbDAV", config, process_identity=123)
        third = monitor.check("nzbdav", "NzbDAV", config, process_identity=456)

        self.assertEqual(first, second)
        self.assertEqual(first["status"], third["status"])
        self.assertEqual(request.call_count, 2)

    @patch("utils.service_health.subprocess.run")
    @patch("utils.service_health.shutil.which", return_value="/usr/bin/pg_isready")
    def test_postgres_rejecting_connections_is_starting(self, _which, run):
        run.return_value = Mock(returncode=1, stdout="", stderr="")

        result = self.monitor.check(
            "postgres",
            "PostgreSQL",
            {"port": 5432, "user": "DUMB"},
        )

        self.assertEqual(result["status"], "starting")
        self.assertTrue(result["healthy"])
        self.assertIn("rejecting connections", result["reason"])
        run.assert_called_once_with(
            [
                "/usr/bin/pg_isready",
                "-U",
                "DUMB",
                "-d",
                "postgres",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
                "-t",
                "2",
            ],
            capture_output=True,
            text=True,
            timeout=3.5,
            check=False,
        )

    def test_unknown_service_has_no_application_probe(self):
        self.assertIsNone(
            self.monitor.check(
                "unknown",
                "Unknown Service",
                {"port": 1234},
            )
        )


if __name__ == "__main__":
    unittest.main()
