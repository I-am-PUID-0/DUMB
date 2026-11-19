"""Filesystem preflight must not walk remote aliases or overstate access."""

import copy
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch

from utils.arrangement_preflight import _probe, storage_checks


class StoragePathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)

    def no_remote_metadata(self, remote, path):
        original = Path.lstat
        touched = []

        def guarded(candidate, *args, **kwargs):
            touched.append(str(candidate))
            if str(candidate) == str(remote) or str(candidate).startswith(
                str(remote) + "/"
            ):
                raise AssertionError("remote metadata touched")
            return original(candidate, *args, **kwargs)

        with (
            patch("utils.arrangement_preflight.Path.lstat", guarded),
            patch(
                "utils.arrangement_preflight.os.statvfs",
                side_effect=AssertionError("remote statvfs"),
            ),
            patch(
                "utils.arrangement_preflight.os.path.realpath",
                side_effect=AssertionError("unbounded realpath"),
            ),
        ):
            result = _probe(
                str(path),
                12345,
                12345,
                False,
                [("/", "overlay"), (str(remote), "fuse.rclone")],
            )
        self.assertIsNone(result["accessible"])
        self.assertEqual(result["filesystem"], "fuse.rclone")
        return touched

    def test_symlink_alias_child_mount_is_classified_before_metadata(self):
        real = self.root / "real"
        real.mkdir(mode=0o755)
        (real / "remote").mkdir()
        (self.root / "alias").symlink_to(real, target_is_directory=True)
        touched = self.no_remote_metadata(
            real / "remote", self.root / "alias/remote/library"
        )
        self.assertNotIn(str(self.root / "alias/remote"), touched)

    def test_relative_symlink_and_dotdot_keep_canonical_mount_identity(self):
        real = self.root / "real"
        (real / "inner").mkdir(parents=True, mode=0o755)
        (real / "remote").mkdir()
        (self.root / "alias").symlink_to("real/inner", target_is_directory=True)
        self.no_remote_metadata(real / "remote", self.root / "alias/../remote/library")

    def test_absolute_symlink_to_remote_never_traverses_target(self):
        remote = self.root / "remote"
        remote.mkdir()
        (self.root / "alias").symlink_to(
            str(remote / "nested"), target_is_directory=True
        )
        self.no_remote_metadata(remote, self.root / "alias/library")

    def test_execute_only_ancestors_allow_writable_final_directory(self):
        ancestor = self.root / "searchable"
        ancestor.mkdir(mode=0o711)
        final = ancestor / "config"
        final.mkdir()
        final.chmod(0o777)
        result = _probe(str(final), 12345, 12345, True, [("/", "overlay")])
        self.assertTrue(result["present"])
        self.assertTrue(result["accessible"])

    def test_readonly_filesystem_never_reports_writable_even_for_root(self):
        final = self.root / "config"
        final.mkdir(mode=0o777)
        capacity = types.SimpleNamespace(
            f_bavail=100000, f_frsize=4096, f_flag=os.ST_RDONLY
        )
        with patch("utils.arrangement_preflight.os.statvfs", return_value=capacity):
            result = _probe(str(final), 0, 0, True, [("/", "overlay")])
        self.assertFalse(result["accessible"])
        self.assertIn("read-only", result["reason"])

    def test_missing_directory_uses_nearest_writable_ancestor_without_creating_it(self):
        self.root.chmod(0o777)
        absent = self.root / "missing/child"
        result = _probe(str(absent), 12345, 12345, True, [("/", "overlay")])
        self.assertFalse(result["present"])
        self.assertTrue(result["accessible"])
        self.assertFalse((self.root / "missing").exists())

    def test_installer_creation_preserves_service_search_and_controller_write_checks(
        self,
    ):
        service_owner = (os.geteuid() + 10000, os.getegid() + 10000)
        missing = self.root / "new-runtime/config"
        result = _probe(
            str(missing),
            *service_owner,
            True,
            [("/", "overlay")],
            create_owner=(0, 0),
        )
        self.assertTrue(result["accessible"])
        self.assertFalse(missing.exists())
        no_controller_write = _probe(
            str(missing),
            *service_owner,
            True,
            [("/", "overlay")],
            create_owner=service_owner,
        )
        self.assertFalse(no_controller_write["accessible"])
        self.root.chmod(0o777)
        cannot_assign_owner = _probe(
            str(missing),
            *service_owner,
            True,
            [("/", "overlay")],
            create_owner=(os.geteuid() + 20000, os.getegid() + 20000),
        )
        self.assertFalse(cannot_assign_owner["accessible"])
        self.assertIn("cannot assign", cannot_assign_owner["reason"])
        self.root.chmod(0o700)
        no_service_search = _probe(
            str(missing),
            *service_owner,
            True,
            [("/", "overlay")],
            create_owner=(os.geteuid(), os.getegid()),
        )
        self.assertFalse(no_service_search["accessible"])

    def test_installer_creation_cannot_override_a_readonly_filesystem(self):
        capacity = types.SimpleNamespace(
            f_bavail=100000, f_frsize=4096, f_flag=os.ST_RDONLY
        )
        with patch("utils.arrangement_preflight.os.statvfs", return_value=capacity):
            result = _probe(
                str(self.root / "new-runtime"),
                os.geteuid() + 10000,
                os.getegid() + 10000,
                True,
                [("/", "overlay")],
                create_owner=(os.geteuid(), os.getegid()),
            )
        self.assertFalse(result["accessible"])
        self.assertIn("read-only", result["reason"])

    def test_symlink_cycle_has_a_bounded_metadata_budget(self):
        (self.root / "loop").symlink_to("loop", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symbolic link"):
            _probe(str(self.root / "loop/child"), 0, 0, False, [("/", "overlay")])


class BazarrStoragePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o711)
        self.install = self.root / "application"
        self.install.mkdir(mode=0o755)
        self.data = self.root / "runtime-data"
        self.config = {
            "puid": os.geteuid() + 10000,
            "pgid": os.getegid() + 10000,
            "bazarr": {
                "enabled": False,
                "config_dir": str(self.install),
                "config_file": str(self.data / "config/config.yaml"),
                "command": ["python", "bazarr.py", "--config", str(self.data)],
            },
        }

    def checks(self):
        return storage_checks(
            {"services": [{"id": "bazarr", "service_key": "bazarr"}]},
            {"optional_services": ["bazarr"]},
            self.config,
            self.config,
            mounts=[("/", "overlay")],
        )

    def test_installer_owned_bazarr_tree_does_not_require_service_write_access(self):
        before = copy.deepcopy(self.config)
        with patch("os.chown", side_effect=AssertionError("preflight changed owner")):
            checks = {item["path"]: item for item in self.checks()}
        self.assertTrue(checks[str(self.install)]["ok"])
        self.assertIs(checks[str(self.data)]["ok"], os.geteuid() == 0)
        self.assertFalse(checks[str(self.data)]["present"])
        self.assertFalse(self.data.exists())
        self.assertEqual(self.config, before)

    def test_existing_bazarr_runtime_data_still_requires_service_write_access(self):
        self.data.mkdir(mode=0o755)
        checks = {item["path"]: item for item in self.checks()}
        self.assertIs(checks[str(self.data)]["ok"], False)
        self.assertTrue(checks[str(self.data)]["present"])

    def test_existing_bazarr_application_must_remain_readable_by_the_service(self):
        self.install.chmod(0o700)
        checks = {item["path"]: item for item in self.checks()}
        installation = checks[str(self.install)]
        self.assertIs(installation["ok"], False)
        self.assertTrue(installation["present"])
        self.assertTrue(installation["critical"])
        self.assertIn("read/search", installation["detail"])
        self.assertEqual(len(checks), 2)

    def test_missing_bazarr_application_uses_installer_creation_permissions(self):
        self.install.rmdir()
        checks = {item["path"]: item for item in self.checks()}
        self.assertIs(checks[str(self.install)]["ok"], True)
        self.assertFalse(checks[str(self.install)]["present"])
        self.assertFalse(self.install.exists())

    def test_unavailable_bazarr_service_access_metadata_stays_unknown(self):
        def probe(path, uid, gid, write, mounts, **kwargs):
            if path == str(self.install) and not write:
                raise OSError("metadata unavailable")
            return _probe(path, uid, gid, write, mounts, **kwargs)

        with patch("utils.arrangement_preflight._probe", side_effect=probe):
            checks = {item["path"]: item for item in self.checks()}
        self.assertIsNone(checks[str(self.install)]["ok"])
        self.assertIn("could not be checked", checks[str(self.install)]["detail"])

    def test_bazarr_default_runtime_data_is_checked_separately_from_installation(self):
        self.config["bazarr"]["command"] = ["python", "bazarr.py"]
        calls = []

        def probe(path, *args, **kwargs):
            calls.append(path)
            return {"present": None, "accessible": None, "reason": "Unverified"}

        with patch("utils.arrangement_preflight._probe", side_effect=probe):
            checks = self.checks()
        self.assertIn("/bazarr/data", calls)
        self.assertEqual(len(checks), 2)
        self.assertTrue(all(item["ok"] is None for item in checks))
