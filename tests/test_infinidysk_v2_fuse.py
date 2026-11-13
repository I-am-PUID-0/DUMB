"""Read-only FUSE environment checks before migration downtime and runtime launch."""

from contextlib import ExitStack
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_infinidysk_v2_runtime as runtime_fixtures
from utils import infinidysk_v2_fuse as fuse
from utils import infinidysk_v2_runtime as runtime


class FusePrerequisiteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.device = self.root / "fuse-device"
        self.device.touch()
        self.policy = self.root / "fuse.conf"
        self.policy.write_text("# managed fixture\nuser_allow_other\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.helper = self.bin / "fusermount3"
        self.helper.write_text("#!/bin/sh\nexit 0\n")
        self.helper.chmod(0o755)
        self.paths = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        def private_path(value):
            self.paths.append(value)
            return {"/dev/fuse": self.device, "/etc/fuse.conf": self.policy}[value]

        # Production keeps fixed paths; only this test maps them to private files.
        self.stack.enter_context(patch.object(fuse, "Path", side_effect=private_path))
        self.stack.enter_context(patch.dict(os.environ, {"PATH": str(self.bin)}))

    def test_allowed_policy_is_read_only_and_does_not_execute_helper(self):
        before = {
            path: (
                path.read_bytes(),
                path.stat().st_mode,
                path.stat().st_uid,
                path.stat().st_mtime_ns,
            )
            for path in (self.device, self.policy, self.helper)
        }
        with patch(
            "subprocess.Popen",
            side_effect=AssertionError("A prerequisite check executed a process"),
        ):
            self.assertEqual(fuse.validate_fuse_prerequisites(1001), (True, None))
        self.assertEqual(self.paths, ["/dev/fuse", "/etc/fuse.conf"])
        for path, recorded in before.items():
            self.assertEqual(
                (
                    path.read_bytes(),
                    path.stat().st_mode,
                    path.stat().st_uid,
                    path.stat().st_mtime_ns,
                ),
                recorded,
            )

    def test_missing_device_is_an_explicit_blocker(self):
        self.device.unlink()
        self.assertEqual(
            fuse.validate_fuse_prerequisites(1001), (False, fuse.FUSE_DEVICE_REQUIRED)
        )
        self.assertEqual(self.paths, ["/dev/fuse"])

    def test_missing_helper_is_an_explicit_blocker(self):
        self.helper.unlink()
        self.assertEqual(
            fuse.validate_fuse_prerequisites(1001), (False, fuse.FUSE_HELPER_REQUIRED)
        )
        self.assertEqual(self.paths, ["/dev/fuse"])

    def test_commented_or_different_policy_remains_blocked_without_rewriting_it(self):
        for contents in (
            "#user_allow_other\n",
            "user_allow_other=true\n",
            "reserved-private-policy-value\n",
        ):
            with self.subTest(contents=contents):
                self.policy.write_text(contents)
                before = self.policy.read_bytes()
                self.assertEqual(
                    fuse.validate_fuse_prerequisites(1001),
                    (False, fuse.FUSE_POLICY_REQUIRED),
                )
                self.assertEqual(self.policy.read_bytes(), before)

    def test_missing_or_invalid_policy_reports_static_read_failure(self):
        self.policy.unlink()
        self.assertEqual(
            fuse.validate_fuse_prerequisites(1001), (False, fuse.FUSE_POLICY_UNREADABLE)
        )
        self.assertFalse(self.policy.exists())
        self.policy.write_bytes(b"reserved-private-policy-value\xff")
        self.assertEqual(
            fuse.validate_fuse_prerequisites(1001), (False, fuse.FUSE_POLICY_UNREADABLE)
        )
        with patch.object(
            Path,
            "read_text",
            side_effect=PermissionError("reserved-private-policy-value"),
        ):
            self.assertEqual(
                fuse.validate_fuse_prerequisites(1001),
                (False, fuse.FUSE_POLICY_UNREADABLE),
            )

    def test_root_owner_does_not_require_or_create_user_allow_other_policy(self):
        self.policy.unlink()
        self.assertEqual(fuse.validate_fuse_prerequisites(0), (True, None))
        self.assertEqual(self.paths, ["/dev/fuse"])
        self.assertFalse(self.policy.exists())

    def test_invalid_process_owner_is_blocked_without_filesystem_access(self):
        for uid in (None, "1001", True, -1):
            with self.subTest(uid=uid):
                self.assertEqual(
                    fuse.validate_fuse_prerequisites(uid),
                    (False, fuse.FUSE_UID_REQUIRED),
                )
        self.assertEqual(self.paths, [])

    def test_device_inspection_failure_does_not_expose_exception_values(self):
        device = Mock()
        device.exists.side_effect = PermissionError("reserved device credential")
        with patch.object(fuse, "Path", return_value=device):
            self.assertEqual(
                fuse.validate_fuse_prerequisites(1001),
                (False, fuse.FUSE_DEVICE_UNAVAILABLE),
            )


class SafePreparationReasonTests(unittest.TestCase):
    def test_authored_mount_and_fuse_causes_keep_their_useful_static_detail(self):
        for reason in (
            fuse.FUSE_DEVICE_REQUIRED,
            fuse.FUSE_HELPER_REQUIRED,
            fuse.FUSE_POLICY_REQUIRED,
            fuse.FUSE_POLICY_UNREADABLE,
            "Native mount target is occupied; stop and unmount its verified owner first",
            "Native mount path crosses a symbolic link",
            "Native mount target must be an empty directory",
            "An unrelated rclone instance owns the selected mount path",
            "Multiple associated rclone instances make mount ownership ambiguous",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(fuse.safe_native_preparation_error(reason), reason)

    def test_dynamic_unknown_or_filesystem_causes_do_not_expose_values(self):
        for reason in (
            None,
            {"api_key": "reserved-private-value"},
            "postgresql://reserved:reserved-private-value@example.invalid/db",
            "Native mount target must be an empty directory reserved-private-value",
            "Permission denied: /reserved-private-value",
            "No such file or directory: /reserved-private-value",
        ):
            with self.subTest(reason=reason):
                detail = fuse.safe_native_preparation_error(reason)
                self.assertNotIn("reserved-private-value", detail)
                self.assertNotIn("postgresql://", detail)
                self.assertLess(len(detail), 200)


class RuntimeFusePrerequisiteTests(unittest.TestCase):
    def setUp(self):
        FusePrerequisiteTests.setUp(self)
        self.fixture = runtime_fixtures.NativeRuntimeTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.installed()
        self.service = self.fixture.service
        self.service["mount_backend"] = "fuse"
        self.mount = self.fixture.root / "delivery"
        self.service["mount_path"] = str(self.mount)

    def test_old_image_policy_is_blocked_before_mount_data_or_credentials_are_created(
        self,
    ):
        self.policy.write_text("#user_allow_other\n")
        with patch.object(runtime, "native_auth", wraps=runtime.native_auth) as auth:
            valid, reason = runtime.prepare_v2_launch(self.service, 1001, 1001)
        self.assertFalse(valid)
        self.assertEqual(reason, fuse.FUSE_POLICY_REQUIRED)
        auth.assert_not_called()
        self.assertFalse(self.mount.exists())
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())

    def test_allowed_policy_keeps_enabled_associated_legacy_owner_and_prepares_empty_target(
        self,
    ):
        legacy = {
            "legacy": {
                "enabled": True,
                "key_type": "infinidysk",
                "mount_dir": str(self.mount.parent),
                "mount_name": self.mount.name,
            }
        }
        valid, reason = runtime.prepare_v2_launch(
            self.service, os.geteuid(), os.getegid(), rclone_instances=legacy
        )
        self.assertTrue(valid, reason)
        self.assertTrue(legacy["legacy"]["enabled"])
        self.assertTrue(self.mount.is_dir())
        self.assertEqual(list(self.mount.iterdir()), [])
        self.assertTrue(
            (Path(self.service["v2_data_dir"]) / runtime.AUTH_FILE).is_file()
        )

    def test_foreign_configured_owner_remains_blocked_before_preparation(self):
        foreign = {
            "foreign": {
                "enabled": True,
                "key_type": "realdebrid",
                "mount_dir": str(self.mount.parent),
                "mount_name": self.mount.name,
            }
        }
        with patch.object(
            runtime,
            "validate_fuse_prerequisites",
            wraps=fuse.validate_fuse_prerequisites,
        ) as prerequisites:
            valid, reason = runtime.prepare_v2_launch(
                self.service, 1001, 1001, rclone_instances=foreign
            )
        self.assertFalse(valid)
        self.assertIn("unrelated rclone", reason)
        prerequisites.assert_not_called()
        self.assertFalse(self.mount.exists())
        self.assertFalse(Path(self.service["v2_data_dir"]).exists())

    def test_historical_rclone_runtime_does_not_require_fuse_prerequisites(self):
        self.service["mount_backend"] = "rclone"
        self.device.unlink()
        with patch.object(
            runtime,
            "validate_fuse_prerequisites",
            side_effect=AssertionError("Rclone checked native FUSE prerequisites"),
        ):
            valid, reason = runtime.prepare_v2_launch(
                self.service, os.geteuid(), os.getegid()
            )
        self.assertTrue(valid, reason)


if __name__ == "__main__":
    unittest.main()
