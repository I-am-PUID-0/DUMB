import copy
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from utils.arrangement_consumer import verify_consumer
from utils.arrangement_probe_child import _authorize_path, _open_directory, probe


class ConsumerVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="dumb-consumer-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.file = self.root / "sample.mp4"
        self.file.write_bytes(b"controlled media sample" * 100)
        self.file.chmod(0o644)
        self.uid = 65534 if os.geteuid() == 0 else os.geteuid()
        self.gid = 65534 if os.geteuid() == 0 else os.getegid()
        identity = (
            {"user": self.uid, "group": self.gid, "extra_groups": []}
            if os.geteuid() == 0
            else {}
        )
        self.consumer = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(30)"], **identity
        )
        self.addCleanup(self.stop_consumer)
        self.handler = types.SimpleNamespace(
            processes={
                self.consumer.pid: {"name": "Test consumer", "managed_service": True}
            }
        )
        self.config = {"plex": {"process_name": "Test consumer"}}
        self.action = {
            "source": "plex",
            "service_key": "plex",
            "action": "consumer_paths",
        }
        self.payload = {
            "arrangement": {
                "values": {
                    "plex::consumer_paths": [
                        {
                            "path": str(self.root),
                            "sample_file": "sample.mp4",
                            "max_bytes": 16,
                        }
                    ]
                }
            }
        }

    def stop_consumer(self):
        if self.consumer.poll() is None:
            self.consumer.terminate()
            self.consumer.wait(timeout=5)
        self.assertIsNotNone(self.consumer.poll())

    def verify(self, **kwargs):
        return verify_consumer(
            self.action,
            self.config,
            self.payload,
            "apply",
            process_handler=self.handler,
            **kwargs,
        )

    def test_actual_consumer_identity_reads_only_the_bounded_sample(self):
        before = self.file.read_bytes()
        result = self.verify()
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(result["evidence"]["uid"], self.uid)
        self.assertTrue(result["evidence"]["namespace_verified"])
        evidence = result["evidence"]["paths"][0]
        self.assertEqual(evidence["read_bytes"], 16)
        self.assertFalse(evidence["sample_complete"])
        self.assertTrue(evidence["content_checked"])
        self.assertEqual(self.file.read_bytes(), before)
        self.assertIsNone(self.consumer.poll())

    def test_kernel_permissions_are_checked_as_consumer_not_controller(self):
        if os.geteuid() != 0:
            self.skipTest("requires root to create a distinct consumer identity")
        self.file.chmod(0o600)
        result = self.verify()
        self.assertEqual(result["verification"], "failed")
        self.assertFalse(result["evidence"]["paths"][0]["accessible"])

    def test_preview_does_not_start_probe_or_touch_media(self):
        with patch(
            "utils.arrangement_consumer.subprocess.Popen",
            side_effect=AssertionError("probe started"),
        ):
            result = self.verify(read_only=True)
        self.assertEqual(result["execution"], "planned")
        self.assertEqual(result["verification"], "not_checked")

    def test_alias_cannot_authorize_a_protected_system_directory(self):
        alias = self.root / "alias"
        alias.symlink_to("/etc", target_is_directory=True)
        self.payload["arrangement"]["values"]["plex::consumer_paths"] = [
            {"path": str(alias)}
        ]
        self.assertEqual(self.verify()["verification"], "failed")

    def test_remote_alias_is_rejected_before_remote_metadata_access(self):
        alias = self.root / "alias"
        alias.symlink_to("/reserved-remote-media", target_is_directory=True)
        real = os.lstat

        def guarded(path):
            if str(path).startswith("/reserved-remote-media"):
                raise AssertionError("remote metadata was touched")
            return real(path)

        with patch("utils.arrangement_probe_child.os.lstat", side_effect=guarded):
            with self.assertRaisesRegex(ValueError, "Remote-mount"):
                _authorize_path(
                    str(alias),
                    [("/", "overlay"), ("/reserved-remote-media", "fuse.rclone")],
                    False,
                )

    def test_cancel_terminates_probe_without_stopping_consumer(self):
        result = self.verify(cancelled=lambda: True)
        self.assertEqual(result["execution"], "waiting_dependency")
        self.assertFalse(result["retryable"])
        self.assertIsNone(self.consumer.poll())

    def test_nested_remote_sample_is_rejected_before_opening_its_mount(self):
        remote = self.root / "network-sample"
        real_open = os.open

        def guarded(path, *args, **kwargs):
            if path == "network-sample":
                raise AssertionError("Remote mount was opened without consent")
            return real_open(path, *args, **kwargs)

        with (
            patch(
                "utils.arrangement_probe_child._mounts",
                return_value=[("/", "overlay"), (str(remote), "nfs4")],
            ),
            patch(
                "utils.arrangement_probe_child.enter_consumer",
                return_value=(self.uid, self.gid),
            ),
            patch("utils.arrangement_probe_child.os.open", side_effect=guarded),
        ):
            evidence = probe(
                {
                    "pid": self.consumer.pid,
                    "start_time": "fixture",
                    "paths": [
                        {
                            "path": str(self.root),
                            "sample_file": "network-sample/movie.mkv",
                        }
                    ],
                }
            )
        self.assertFalse(evidence["paths"][0]["accessible"])
        self.assertFalse(evidence["paths"][0]["content_checked"])

    def test_directory_open_does_not_follow_a_replaced_local_alias(self):
        alias = self.root / "changed-alias"
        alias.symlink_to("/reserved-remote-media", target_is_directory=True)
        with self.assertRaises(OSError):
            _open_directory(str(alias), [("/", "overlay")], False)

    def test_sample_can_traverse_an_execute_only_parent(self):
        directory = self.root / "traversal"
        directory.mkdir()
        sample = directory / "movie.mp4"
        sample.write_bytes(b"controlled sample")
        sample.chmod(0o644)
        directory.chmod(0o111)
        self.addCleanup(directory.chmod, 0o700)
        self.payload["arrangement"]["values"]["plex::consumer_paths"][0][
            "sample_file"
        ] = "traversal/movie.mp4"
        self.assertEqual(self.verify()["verification"], "passed")

    def test_config_and_request_remain_unchanged(self):
        config, payload = copy.deepcopy(self.config), copy.deepcopy(self.payload)
        self.verify()
        self.assertEqual(self.config, config)
        self.assertEqual(self.payload, payload)


if __name__ == "__main__":
    unittest.main()
