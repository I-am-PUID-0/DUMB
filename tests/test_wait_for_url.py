import sys
import types
import unittest

requests_stub = types.ModuleType("requests")
requests_stub.RequestException = Exception
requests_stub.request = lambda *args, **kwargs: None
sys.modules["requests"] = requests_stub

from utils import wait_for_url


class FakeResponse:
    def __init__(self, status_code, payload=None, json_error=False):
        self.status_code = status_code
        self._payload = payload
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._payload


class FakeLogger:
    def __init__(self):
        self.debug_messages = []

    def debug(self, *args):
        self.debug_messages.append(args)


class WaitForUrlHelperTests(unittest.TestCase):
    def test_webdav_wait_detection_matches_webdav_and_dav_paths(self):
        self.assertTrue(wait_for_url._looks_like_webdav_wait("http://service/webdav"))
        self.assertTrue(
            wait_for_url._looks_like_webdav_wait("http://service/api/webdav/files")
        )
        self.assertTrue(wait_for_url._looks_like_webdav_wait("http://service/dav/root"))
        self.assertFalse(
            wait_for_url._looks_like_webdav_wait("http://service/api/health")
        )

    def test_resolve_probe_defaults_webdav_to_propfind_depth_zero(self):
        method, headers = wait_for_url._resolve_probe({"url": "http://service/webdav"})

        self.assertEqual(method, "PROPFIND")
        self.assertEqual(headers, {"Depth": "0"})

    def test_resolve_probe_preserves_explicit_method_and_headers(self):
        method, headers = wait_for_url._resolve_probe(
            {
                "url": "http://service/webdav",
                "probe_method": "post",
                "probe_headers": {"Depth": "1", "X-Test": "yes"},
            }
        )

        self.assertEqual(method, "POST")
        self.assertEqual(headers, {"Depth": "1", "X-Test": "yes"})

    def test_json_path_exists_handles_nested_dict_paths(self):
        payload = {"status": {"ready": True, "details": {"port": 8080}}}

        self.assertTrue(wait_for_url._json_path_exists(payload, "status.ready"))
        self.assertTrue(wait_for_url._json_path_exists(payload, "status.details.port"))
        self.assertFalse(wait_for_url._json_path_exists(payload, "status.missing"))
        self.assertFalse(wait_for_url._json_path_exists(payload, "status.ready.value"))

    def test_response_ready_accepts_2xx_and_webdav_207(self):
        self.assertTrue(wait_for_url._response_is_ready(FakeResponse(200), "GET"))
        self.assertTrue(wait_for_url._response_is_ready(FakeResponse(204), "GET"))
        self.assertTrue(wait_for_url._response_is_ready(FakeResponse(207), "PROPFIND"))
        self.assertTrue(wait_for_url._response_is_ready(FakeResponse(207), "GET"))
        self.assertFalse(wait_for_url._response_is_ready(FakeResponse(302), "GET"))
        self.assertFalse(wait_for_url._response_is_ready(FakeResponse(500), "GET"))

    def test_response_ready_requires_expected_json_path_when_configured(self):
        wait_entry = {"expected_json_path": "status.ready"}
        logger = FakeLogger()

        self.assertTrue(
            wait_for_url._response_is_ready(
                FakeResponse(200, {"status": {"ready": True}}),
                "GET",
                wait_entry,
                logger,
                "http://service/api/status",
            )
        )
        self.assertFalse(
            wait_for_url._response_is_ready(
                FakeResponse(200, {"status": {}}),
                "GET",
                wait_entry,
                logger,
                "http://service/api/status",
            )
        )
        self.assertFalse(
            wait_for_url._response_is_ready(
                FakeResponse(200, json_error=True),
                "GET",
                wait_entry,
                logger,
                "http://service/api/status",
            )
        )
        self.assertEqual(len(logger.debug_messages), 2)


if __name__ == "__main__":
    unittest.main()
