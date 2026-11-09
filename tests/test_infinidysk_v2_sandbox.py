import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts.infinidysk_v2_sandbox import confine, namespace_command, probe


class SandboxTests(unittest.TestCase):
    def test_mapping_is_private_and_preserves_requested_nonroot_uid_gid(self):
        with patch(
            "scripts.infinidysk_v2_sandbox.shutil.which",
            return_value="/usr/bin/unshare",
        ):
            command = namespace_command(1200, 1300, ["python", "owned-helper"])
        self.assertEqual(
            command,
            [
                "/usr/bin/unshare",
                "--user",
                "--map-users=0:0:1201",
                "--map-groups=0:0:1301",
                "--",
                "python",
                "owned-helper",
            ],
        )
        for uid, gid in ((0, 1000), (1000, 0), (-1, 1000), (2**32, 1000)):
            with self.assertRaises(ValueError):
                namespace_command(uid, gid, ["helper"])

    def test_missing_namespace_tool_or_unprivileged_controller_fails_closed(self):
        with patch("scripts.infinidysk_v2_sandbox.shutil.which", return_value=None):
            with self.assertRaisesRegex(ValueError, "unshare"):
                namespace_command(1000, 1000, ["helper"])
        with (
            patch("scripts.infinidysk_v2_sandbox.os.geteuid", return_value=1000),
            patch("scripts.infinidysk_v2_sandbox.ctypes.CDLL") as libc,
        ):
            with self.assertRaisesRegex(ValueError, "privileges"):
                confine([])
        libc.assert_not_called()

    def test_unsupported_landlock_never_attempts_namespace_or_writes(self):
        with (
            patch("scripts.infinidysk_v2_sandbox.os.geteuid", return_value=0),
            patch("scripts.infinidysk_v2_sandbox.ctypes.CDLL") as loader,
        ):
            loader.return_value.syscall.return_value = 2
            with self.assertRaisesRegex(ValueError, "ABI 3"):
                confine([])
            loader.return_value.unshare.assert_not_called()

    def test_probe_uses_literal_loopback_http_without_proxies_or_redirect_transport(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            auth = root / ".dumb-native-auth.json"
            auth.write_text(json.dumps({"api_key": "private-fixture-key"}))
            auth.chmod(0o600)
            response = Mock()
            response.status = 200
            response.read.return_value = b'{"state":"done"}'
            connection = Mock()
            connection.getresponse.return_value = response
            output = io.StringIO()
            with (
                patch(
                    "scripts.infinidysk_v2_sandbox.http.client.HTTPConnection",
                    return_value=connection,
                ) as factory,
                contextlib.redirect_stdout(output),
            ):
                probe(
                    {"data_dir": str(root), "port": 1234},
                    "GET",
                    "/api/v1/upgrade/status",
                )
            factory.assert_called_once_with("127.0.0.1", 1234, timeout=5)
            self.assertEqual(
                json.loads(output.getvalue()),
                {"status": 200, "body": {"state": "done"}},
            )
            self.assertNotIn("private-fixture-key", output.getvalue())
            connection.close.assert_called_once()

    def test_probe_refuses_redirected_credentials_and_mutating_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "key.json"
            outside.write_text('{"api_key":"private"}')
            (root / ".dumb-native-auth.json").symlink_to(outside)
            with self.assertRaises(OSError):
                probe({"data_dir": str(root), "port": 1234}, "GET", "/healthz")
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                probe(
                    {"data_dir": str(root), "port": 1234},
                    "POST",
                    "/api/v1/database/migration",
                )


if __name__ == "__main__":
    unittest.main()
