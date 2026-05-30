import unittest
from unittest.mock import patch

from utils import url_security


class URLSecurityTests(unittest.TestCase):
    def test_safe_request_accepts_http_and_preserves_request_data(self):
        req = url_security.safe_request(
            "http://127.0.0.1:8080/api",
            data=b"{}",
            headers={"X-Test": "yes"},
            method="POST",
        )

        self.assertEqual(req.full_url, "http://127.0.0.1:8080/api")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.data, b"{}")
        self.assertEqual(req.get_header("X-test"), "yes")

    def test_safe_request_accepts_https(self):
        req = url_security.safe_request("https://example.test/index.json")

        self.assertEqual(req.full_url, "https://example.test/index.json")

    def test_safe_request_rejects_userinfo_urls(self):
        with self.assertRaises(ValueError):
            url_security.safe_request("https://user:pass@example.com/api")

    def test_safe_request_rejects_urls_without_host(self):
        with self.assertRaises(ValueError):
            url_security.safe_request("https:///api")

    def test_safe_request_rejects_non_http_schemes(self):
        with self.assertRaises(ValueError):
            url_security.safe_request("file:///etc/passwd")

    def test_safe_urlopen_rejects_relative_urls_before_opening(self):
        with patch.object(url_security.urllib.request, "urlopen") as urlopen:
            with self.assertRaises(ValueError):
                url_security.safe_urlopen("/api/v1/status", timeout=1)

        urlopen.assert_not_called()

    def test_safe_urlopen_validates_request_before_opening(self):
        req = url_security.safe_request("https://example.test/api")

        with patch.object(url_security.urllib.request, "urlopen") as urlopen:
            result = url_security.safe_urlopen(req, timeout=1)

        urlopen.assert_called_once_with(req, timeout=1)
        self.assertIs(result, urlopen.return_value)


if __name__ == "__main__":
    unittest.main()
