import json
import unittest
from unittest.mock import Mock, patch

from utils.service_health import MAX_RESPONSE_BYTES, ServiceHealthMonitor
from utils.service_health import requests as probe_requests


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
            "infinidysk",
            "InfiniDysk",
            {"backend_port": 8080},
            process_identity=123,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertIsNone(result["reason"])
        self.assertEqual(result["details"]["endpoint"], "/health")
        request.assert_called_once()

    @patch("utils.service_health.requests.request")
    def test_emby_native_ping_identity_is_healthy(self, request):
        for body in ("Emby Server", "\r\nEmby Server\r\n", "emby SERVER"):
            with self.subTest(body=body):
                request.reset_mock()
                request.return_value = FakeResponse(body=body)
                result = self.monitor.check(
                    "emby", "Cinema Media Server", {"port": 8096}
                )
                self.assertEqual(result["status"], "healthy")
                self.assertTrue(result["healthy"])
                self.assertIsNone(result["reason"])
                self.assertNotIn("reported_status", result["details"])
                self.assertEqual(result["details"]["endpoint"], "/emby/System/Ping")
                request.assert_called_once_with(
                    "GET",
                    "http://127.0.0.1:8096/emby/System/Ping",
                    auth=None,
                    allow_redirects=False,
                    stream=True,
                    timeout=2.5,
                )

    @patch("utils.service_health.requests.request")
    def test_emby_identity_requires_whole_raw_response(self, request):
        for body, content_type in (
            ("Emby Server failed", "text/plain"),
            ("Not Emby Server", "text/plain"),
            ("<html><body>Emby Server</body></html>", "text/html"),
            ('"Emby Server"', "application/json"),
            (json.dumps({"status": "Emby Server"}), "application/json"),
        ):
            with self.subTest(body=body):
                request.return_value = FakeResponse(
                    body=body, content_type=content_type
                )
                result = self.monitor.check("emby", "Emby", {"port": 8096})
                self.assertEqual(result["status"], "degraded")
                self.assertTrue(result["healthy"])
                self.assertIsNotNone(result["reason"])
                self.assertIn("reported_status", result["details"])

    @patch("utils.service_health.requests.request")
    def test_emby_identity_cannot_override_http_failure_or_redirect_status(
        self, request
    ):
        for code in (302, 401, 403, 404, 405, 429, 500, 503):
            with self.subTest(code=code):
                request.return_value = FakeResponse(
                    status_code=code, body="Emby Server"
                )
                result = self.monitor.check("emby", "Emby", {"port": 8096})
                self.assertEqual(
                    result["status"], "degraded" if code < 500 else "unhealthy"
                )
                self.assertEqual(result["healthy"], code < 500)
                self.assertIsNotNone(result["reason"])
                self.assertEqual(result["details"]["http_status"], code)
                self.assertEqual(result["details"]["supported"], code != 404)

    @patch("utils.service_health.requests.request")
    def test_emby_reported_states_and_component_severity_remain_authoritative(
        self, request
    ):
        bodies = (
            ("Unhealthy", "text/plain", "unhealthy"),
            ("maintenance_window", "text/plain", "degraded"),
            (
                json.dumps({"status": "Starting", "message": "Emby Server"}),
                "application/json",
                "starting",
            ),
            (
                json.dumps({"status": "Degraded", "message": "Emby Server"}),
                "application/json",
                "degraded",
            ),
            (
                json.dumps({"status": "Unhealthy", "message": "Emby Server"}),
                "application/json",
                "unhealthy",
            ),
            (
                json.dumps(
                    {
                        "name": "Emby Server",
                        "components": {"database": {"status": "Unhealthy"}},
                    }
                ),
                "application/json",
                "unhealthy",
            ),
            (
                json.dumps(
                    {
                        "status": "Emby Server",
                        "components": {"database": {"status": "Unhealthy"}},
                    }
                ),
                "application/json",
                "unhealthy",
            ),
        )
        for body, content_type, expected in bodies:
            with self.subTest(body=body):
                request.return_value = FakeResponse(
                    body=body, content_type=content_type
                )
                result = self.monitor.check("emby", "Emby", {"port": 8096})
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["healthy"], expected != "unhealthy")
                self.assertIsNotNone(result["reason"])
                if "components" in body:
                    self.assertEqual(
                        result["details"]["components"],
                        [{"name": "database", "status": "unhealthy"}],
                    )

    @patch("utils.service_health.requests.request")
    def test_emby_identity_is_not_a_generic_healthy_state(self, request):
        for key, config in (
            ("jellyfin", {"port": 8096}),
            ("infinidysk", {"backend_port": 8080}),
        ):
            with self.subTest(key=key):
                request.return_value = FakeResponse(body="Emby Server")
                result = self.monitor.check(key, "Emby", config)
                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["details"]["reported_status"], "Emby Server")
        request.return_value = FakeResponse(body="Healthy")
        result = self.monitor.check("jellyfin", "Jellyfin", {"port": 8096})
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["details"]["endpoint"], "/health")

    @patch("utils.service_health.requests.request")
    def test_emby_timeout_remains_unhealthy(self, request):
        # Full discovery uses a minimal requests stub. Model a timeout through
        # the same RequestException interface caught by the monitor.
        class Timeout(probe_requests.RequestException):
            pass

        request.side_effect = Timeout("fixture timeout")
        result = self.monitor.check("emby", "Emby", {"port": 8096})
        self.assertEqual(result["status"], "unhealthy")
        self.assertFalse(result["healthy"])
        self.assertEqual(result["details"]["error_type"], "Timeout")
        self.assertIn("did not respond", result["reason"])

    @patch("utils.service_health.requests.request")
    def test_emby_identity_does_not_bypass_response_size_limit(self, request):
        request.return_value = FakeResponse(
            body="Emby Server" + " " * MAX_RESPONSE_BYTES
        )
        result = self.monitor.check("emby", "Emby", {"port": 8096})
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["details"]["response_truncated"])
        self.assertIn("safe size limit", result["reason"])

    @patch("utils.service_health.requests.request")
    def test_mediastorm_uses_application_health_endpoint(self, request):
        request.return_value = FakeResponse(body="OK")

        result = self.monitor.check(
            "mediastorm",
            "mediastorm",
            {"port": 7777},
            process_identity=123,
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["details"]["endpoint"], "/health")
        request.assert_called_once()
        _, url = request.call_args.args
        self.assertEqual(url, "http://127.0.0.1:7777/health")

    @patch("utils.service_health.requests.request")
    def test_aiostreams_uses_database_aware_health_endpoint(self, request):
        request.return_value = FakeResponse(
            body=json.dumps({"success": True}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "aiostreams", "AIOStreams", {"port": 3006}, process_identity=123
        )

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["healthy"])
        self.assertEqual(result["details"]["endpoint"], "/api/v1/health")
        _, url = request.call_args.args
        self.assertEqual(url, "http://127.0.0.1:3006/api/v1/health")

    @patch("utils.service_health.requests.request")
    def test_aiostreams_unexpected_health_payload_is_degraded(self, request):
        request.return_value = FakeResponse(
            body=json.dumps({"status": "ok"}),
            content_type="application/json",
        )

        result = self.monitor.check("aiostreams", "AIOStreams", {"port": 3006})

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["details"]["validation"], "missing success")

    @patch("utils.service_health.requests.request")
    def test_nzbdav_migration_response_is_starting_not_unhealthy(self, request):
        request.return_value = FakeResponse(
            status_code=503,
            body=json.dumps({"status": "migrating"}),
            content_type="application/json",
        )

        result = self.monitor.check(
            "infinidysk",
            "InfiniDysk",
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
            "infinidysk",
            "InfiniDysk",
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
            "infinidysk",
            "InfiniDysk",
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
            "infinidysk",
            "InfiniDysk",
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
            "Rclone w/ InfiniDysk",
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

        first = monitor.check("infinidysk", "InfiniDysk", config, process_identity=123)
        second = monitor.check("infinidysk", "InfiniDysk", config, process_identity=123)
        third = monitor.check("infinidysk", "InfiniDysk", config, process_identity=456)

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
