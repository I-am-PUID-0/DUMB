import json
import socket
import ssl
import threading
import unittest
from unittest import mock

from utils import arrangement_http as http


class Response:
    def __init__(self, status=200, body=b"{}", headers=()):
        self.status, self.body, self.headers = status, body, headers
        self.offset = 0

    def read(self, size):
        result = self.body[self.offset : self.offset + size]
        self.offset += len(result)
        return result

    read1 = read

    def getheaders(self):
        return self.headers


class Connection:
    instances = []
    response = None

    def __init__(self, host, address, port, timeout, **kwargs):
        self.host, self.address, self.port, self.timeout = host, address, port, timeout
        self.kwargs = kwargs
        self.calls, self.closed = [], False
        self.sock = mock.Mock()
        self.instances.append(self)

    def request(self, method, target, body=None, headers=None):
        self.calls.append((method, target, body, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class ApiTransportTests(unittest.TestCase):
    def setUp(self):
        Connection.instances = []
        Connection.response = Response()
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(http, "_PinnedHTTP", Connection).start()
        mock.patch.object(http, "_PinnedHTTPS", Connection).start()

    def resolved(self, addresses):
        return mock.patch.object(http, "_resolve", return_value=addresses)

    def test_explicit_schema_limits_are_bounded_and_do_not_change_default_requests(
        self,
    ):
        body = json.dumps({"schema": "x" * (3 * 1024 * 1024)}).encode()
        with self.resolved(["127.0.0.1"]):
            Connection.response = Response(body=body)
            client = http.ApiClient("http://127.0.0.1:9696", managed=True)
            with self.assertRaises(http.ApiError):
                client.get("/api/v1/indexer/schema")
            Connection.response = Response(body=body)
            result = client.get(
                "/api/v1/indexer/schema",
                response_limit=8 * 1024 * 1024,
                json_node_limit=1000000,
            )
            self.assertEqual(len(result.json()["schema"]), 3 * 1024 * 1024)
            Connection.response = Response(body=body)
            with self.assertRaises(http.ApiError):
                client.get("/api/v1/status")
        self.assertTrue(all(connection.closed for connection in Connection.instances))

    def test_schema_node_limit_keeps_depth_and_hard_caps(self):
        content = json.dumps([0] * 100001).encode()
        with self.assertRaises(http.ApiError):
            http.ApiResponse(200, content).json()
        self.assertEqual(
            len(http.ApiResponse(200, content, json_node_limit=1000000).json()), 100001
        )
        nested = 0
        for _ in range(34):
            nested = [nested]
        with self.assertRaises(http.ApiError):
            http.ApiResponse(
                200, json.dumps(nested).encode(), json_node_limit=1000000
            ).json()
        for key, value in (
            ("response_limit", 16 * 1024 * 1024 + 1),
            ("json_node_limit", 1000001),
            ("response_limit", True),
            ("json_node_limit", 0),
        ):
            with (
                mock.patch.object(http, "_resolve") as resolver,
                self.assertRaises(ValueError),
            ):
                http.ApiClient("http://127.0.0.1", managed=True).get(
                    "/schema", **{key: value}
                )
            resolver.assert_not_called()

    def test_managed_loopback_and_authorized_private_are_distinct(self):
        with self.resolved(["127.0.0.1"]):
            http.ApiClient("http://127.0.0.1:8080", managed=True).get("/api/status")
            with self.assertRaises(ValueError):
                http.ApiClient("http://127.0.0.1:8080").get("/api/status")
            http.ApiClient("http://127.0.0.1:8080", allow_private=True).get(
                "/api/status"
            )
        with self.resolved(["10.20.30.40"]):
            with self.assertRaises(ValueError):
                http.ApiClient("http://service.example.com", managed=True).get(
                    "/api/status"
                )
            http.ApiClient("http://service.example.com", allow_private=True).get(
                "/api/status"
            )

    def test_mixed_dns_answers_and_metadata_addresses_never_receive_credentials(self):
        for addresses in (
            ["93.184.216.34", "127.0.0.1"],
            ["169.254.169.254"],
            ["::ffff:169.254.169.254"],
            ["0.0.0.0"],
            ["224.0.0.1"],
        ):
            with self.subTest(addresses=addresses), self.resolved(addresses):
                with self.assertRaises(ValueError):
                    http.ApiClient(
                        "https://service.example.com",
                        headers={"Authorization": "fixture-secret"},
                    ).get("/api")
        self.assertEqual(Connection.instances, [])

    def test_dns_is_resolved_once_and_connection_is_pinned_to_approved_ip(self):
        with mock.patch.object(
            http, "_resolve", side_effect=[["93.184.216.34"], ["127.0.0.1"]]
        ) as resolver:
            http.ApiClient(
                "http://service.example.com", headers={"X-Api-Key": "fixture"}
            ).get("/api")
        self.assertEqual(resolver.call_count, 1)
        self.assertEqual(Connection.instances[0].address, "93.184.216.34")
        self.assertEqual(
            Connection.instances[0].calls[0][3]["Host"], "service.example.com:80"
        )

    def test_redirect_is_not_followed_or_treated_as_success(self):
        Connection.response = Response(
            302, b"{}", [("Location", "http://169.254.169.254/metadata")]
        )
        with self.resolved(["93.184.216.34"]):
            result = http.ApiClient(
                "https://service.example.com",
                headers={"Authorization": "fixture-secret"},
            ).get("/api")
        self.assertEqual(len(Connection.instances), 1)
        with self.assertRaises(http.ApiError):
            result.raise_for_status()

    def test_tls_verifies_certificates_by_default_and_insecure_is_explicit(self):
        with self.resolved(["93.184.216.34"]):
            http.ApiClient("https://service.example.com").get("/api")
            http.ApiClient("https://service.example.com", verify_tls=False).get("/api")
        contexts = [connection.kwargs["context"] for connection in Connection.instances]
        self.assertTrue(contexts[0].check_hostname)
        self.assertEqual(contexts[0].verify_mode, ssl.CERT_REQUIRED)
        self.assertFalse(contexts[1].check_hostname)
        self.assertEqual(contexts[1].verify_mode, ssl.CERT_NONE)

    def test_oversized_body_invalid_json_and_deep_json_are_bounded(self):
        Connection.response = Response(body=b"x" * (http.MAX_RESPONSE + 1))
        with self.resolved(["93.184.216.34"]):
            with self.assertRaises(http.ApiError):
                http.ApiClient("http://service.example.com").get("/api")
        self.assertTrue(Connection.instances[-1].closed)
        for body in (b"invalid", ("[" * 100 + "0" + "]" * 100).encode()):
            with self.subTest(body=body[:12]):
                with self.assertRaises(http.ApiError):
                    http.ApiResponse(200, body).json()

    def test_url_and_relative_path_injection_rejected_before_connection(self):
        for url in (
            "file:///etc/passwd",
            "https://user:password@example.com",
            "https://example.com?token=x",
            "https://example.com/#fragment",
            "https://exa\tmple.com",
            " https://example.com",
        ):
            with self.subTest(url=url), self.resolved(["93.184.216.34"]):
                with self.assertRaises(ValueError):
                    http.ApiClient(url).get("/api")
        for path in (
            "//evil.example.com/api",
            "https://evil.example.com/api",
            "/api\r\nX: y",
        ):
            with self.subTest(path=path), self.resolved(["93.184.216.34"]):
                with self.assertRaises(ValueError):
                    http.ApiClient("https://example.com").get(path)

    def test_proxy_environment_is_not_consulted_and_session_cookies_stay_on_original_origin(
        self,
    ):
        Connection.response = Response(
            headers=[("Set-Cookie", "session=fixture; Path=/; HttpOnly")]
        )
        with (
            self.resolved(["93.184.216.34"]),
            mock.patch.dict("os.environ", {"HTTPS_PROXY": "http://evil.example.com"}),
        ):
            client = http.ApiClient("https://service.example.com")
            client.get("/auth")
            client.get("/api")
        self.assertTrue(
            all(
                connection.host == "service.example.com"
                for connection in Connection.instances
            )
        )
        self.assertEqual(
            Connection.instances[1].calls[0][3]["Cookie"], "session=fixture"
        )

    def test_depth_zero_propfind_is_supported_without_enabling_write_verbs(self):
        with self.resolved(["93.184.216.34"]):
            result = http.ApiClient("https://service.example.com").request(
                "PROPFIND", "/media", headers={"Depth": "0"}
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(Connection.instances[0].calls[0][0], "PROPFIND")
        self.assertEqual(Connection.instances[0].calls[0][3]["Depth"], "0")
        with self.resolved(["93.184.216.34"]):
            with self.assertRaises(ValueError):
                http.ApiClient("https://service.example.com").request("MOVE", "/media")

    def test_total_deadline_interrupts_a_stalled_response(self):
        class Blocking(Connection):
            def getresponse(self):
                released.wait(3)
                if released.is_set():
                    raise OSError("fixture disconnect")
                return Response()

        released = threading.Event()

        def abort(*args):
            released.set()

        with (
            self.resolved(["93.184.216.34"]),
            mock.patch.object(http, "_PinnedHTTP", Blocking),
        ):
            # No real network; shutdown wakes the deliberately stalled read.
            original_init = Blocking.__init__

            def init(connection, *args, **kwargs):
                original_init(connection, *args, **kwargs)
                connection.sock.shutdown.side_effect = abort

            with mock.patch.object(Blocking, "__init__", init):
                client = http.ApiClient("http://service.example.com", timeout=1)
                with self.assertRaises(http.ApiError):
                    client.get("/api")
        self.assertTrue(released.is_set())
