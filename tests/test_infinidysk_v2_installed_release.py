"""Accepted native program state without running programs or opening databases."""

import copy
import hashlib
import json
import os
from pathlib import Path
import pwd
import unittest
from unittest.mock import patch

from tests import test_infinidysk_v2_update as fixtures
from utils import infinidysk_v2_runtime as runtime
from utils.infinidysk_v2_installed_release import (
    InstalledReleaseError,
    _installed_lock,
    read_installed_release,
    record_installed_release,
)
from utils.infinidysk_v2_releases import architecture


class InstalledReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.NativeRuntimeUpdateTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.program = self.fixture.runtime
        self.data = self.root / "native-data"
        self.data.mkdir()
        self.service = self.fixture.service
        self.service["v2_data_dir"] = str(self.data)
        self.metadata = self.fixture.old
        self.state = self.root / "accepted"

    def record(self, **options):
        return record_installed_release(
            self.service, self.metadata, self.state, data_root=self.root, **options
        )

    def read(self):
        return read_installed_release(self.service, self.state, data_root=self.root)

    def path(self):
        paths = list(self.state.glob("installed-release-*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0]

    def replace_program(self, metadata, binary):
        self.program.joinpath("infinidysk").write_bytes(binary)
        self.program.joinpath("version.txt").write_text(metadata["release"])
        self.program.joinpath("commit.txt").write_text(metadata["commit"])
        self.program.joinpath(runtime.MARKER).write_text(json.dumps(metadata))

    def test_verified_record_is_private_scoped_and_idempotent_without_data_access(self):
        with patch("sqlite3.connect", side_effect=AssertionError("Native data opened")):
            result = self.record(origin="fresh_install")
            self.assertEqual(self.read(), self.metadata)
            before = self.path().read_bytes()
            stamp = self.path().stat().st_mtime_ns
            self.assertEqual(self.record(origin="existing_runtime"), result)
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.path().stat().st_mtime_ns, stamp)
        self.assertEqual(result["runtime"], str(self.program))
        self.assertEqual(result["data_dir"], str(self.data))
        self.assertEqual(result["origin"], "fresh_install")
        self.assertEqual(self.path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path().stat().st_uid, os.geteuid())

    def test_ordinary_service_settings_do_not_rebind_accepted_program(self):
        self.record()
        before = self.path().read_bytes()
        self.service.update(
            release_version="v2.0.0-beta.20",
            release_version_enabled=True,
            log_level="DEBUG",
            frontend_port=3333,
            env={"INFINIDYSK__SAB__API_KEY": "reserved-fixture-key"},
        )
        self.assertEqual(self.read(), self.metadata)
        self.assertNotIn("reserved-fixture-key", self.path().read_text())
        self.assertEqual(self.path().read_bytes(), before)

    def test_accepted_metadata_survives_absent_container_program(self):
        self.record()
        self.program.rename(self.root / "old-container-program")
        self.assertEqual(self.read(), self.metadata)
        self.assertFalse(self.program.exists())

    def test_new_record_refuses_data_retarget_without_overwriting_evidence(self):
        self.record()
        before = self.path().read_bytes()
        self.service["v2_data_dir"] = str(self.root / "different-data")
        with self.assertRaises(InstalledReleaseError):
            self.read()
        with self.assertRaises(InstalledReleaseError):
            self.record()
        self.assertEqual(self.path().read_bytes(), before)

    def test_default_data_scope_tracks_the_explicit_data_root_without_creating_it(self):
        self.service.pop("v2_data_dir")
        result = self.record()
        self.assertEqual(result["data_dir"], str(self.root / "infinidysk-v2"))
        self.assertFalse((self.root / "infinidysk-v2").exists())
        with self.assertRaises(InstalledReleaseError):
            read_installed_release(
                self.service, self.state, data_root=self.root / "other-root"
            )

    def test_invalid_existing_record_never_becomes_absent_or_replaced(self):
        self.record()
        valid = json.loads(self.path().read_text())
        for value in (
            "{incomplete",
            json.dumps({**valid, "format": "unknown"}),
            json.dumps({**valid, "runtime": str(self.root / "different-runtime")}),
            json.dumps({**valid, "release_metadata": {}}),
            json.dumps({**valid, "origin": None}),
            json.dumps({**valid, "unexpected": True}),
        ):
            with self.subTest(value=value):
                self.path().write_text(value)
                with self.assertRaises(InstalledReleaseError):
                    self.read()
                with self.assertRaises(InstalledReleaseError):
                    self.record()
                self.assertEqual(self.path().read_text(), value)

    def test_public_or_linked_state_is_preserved_and_refused(self):
        self.record()
        path = self.path()
        original = path.read_bytes()
        path.chmod(0o644)
        with self.assertRaises(InstalledReleaseError):
            self.read()
        path.chmod(0o600)
        held = self.root / "retained-state"
        path.rename(held)
        path.symlink_to(held)
        with self.assertRaises(InstalledReleaseError):
            self.read()
        with self.assertRaises(InstalledReleaseError):
            self.record()
        self.assertTrue(path.is_symlink())
        self.assertEqual(held.read_bytes(), original)
        path.unlink()
        os.link(held, path)
        with self.assertRaises(InstalledReleaseError):
            self.read()

    def test_artifact_must_be_verified_before_first_record_is_published(self):
        self.program.joinpath("infinidysk").write_bytes(b"changed program")
        with self.assertRaises(InstalledReleaseError):
            self.record()
        self.assertEqual(list(self.state.glob("installed-release-*.json")), [])

    def test_only_a_newer_verified_release_can_replace_the_current_record(self):
        self.record()
        before = self.path().read_bytes()
        for release in (self.metadata["release"], "v2.0.0-beta.5"):
            with self.subTest(release=release):
                binary = b"different verified artifact"
                replacement = {
                    **self.metadata,
                    "release": release,
                    "binary_sha256": hashlib.sha256(binary).hexdigest(),
                }
                self.replace_program(replacement, binary)
                with self.assertRaisesRegex(
                    InstalledReleaseError, "older or different"
                ):
                    record_installed_release(
                        self.service, replacement, self.state, data_root=self.root
                    )
                self.assertEqual(self.path().read_bytes(), before)
        newer = copy.deepcopy(self.fixture.new)
        self.replace_program(newer, b"new program")
        record_installed_release(
            self.service,
            newer,
            self.state,
            data_root=self.root,
            origin="runtime_update",
        )
        self.assertEqual(self.read(), newer)

    def test_legacy_qualified_marker_is_accepted_without_rewriting_program_files(self):
        binary_hash = hashlib.sha256(b"old program").hexdigest()
        self.program.joinpath("version.txt").write_text(runtime.RELEASE)
        self.program.joinpath("commit.txt").write_text(runtime.COMMIT)
        self.program.joinpath(runtime.MARKER).write_text(
            json.dumps({"commit": runtime.COMMIT, "binary_sha256": binary_hash})
        )
        expected = {
            "format": "official-v2-release-v1",
            "release": runtime.RELEASE,
            "commit": runtime.COMMIT,
            "architecture": architecture(),
            "archive_sha256": runtime.ASSETS[architecture()],
            "binary_sha256": binary_hash,
        }
        before = {path.name: path.read_bytes() for path in self.program.iterdir()}
        record_installed_release(
            self.service,
            expected,
            self.state,
            data_root=self.root,
            origin="migration",
        )
        self.assertEqual(self.read(), expected)
        self.assertEqual(
            {path.name: path.read_bytes() for path in self.program.iterdir()}, before
        )

    def test_state_path_and_public_data_root_arguments_are_guarded(self):
        for invalid in (None, True, 17, "relative"):
            with self.subTest(data_root=invalid):
                with self.assertRaises(InstalledReleaseError):
                    read_installed_release(self.service, self.state, data_root=invalid)
        for root in (self.program / "state", self.data / "state"):
            with self.subTest(state_root=root):
                with self.assertRaises(InstalledReleaseError):
                    record_installed_release(self.service, self.metadata, root)
                self.assertFalse(root.exists())
        alias = self.root / "data-alias"
        alias.symlink_to(self.data, target_is_directory=True)
        self.service["v2_data_dir"] = str(alias)
        with self.assertRaises(InstalledReleaseError):
            self.record()

    def test_dedicated_state_lock_refuses_overlap_and_does_not_change_record(self):
        self.record()
        before = self.path().read_bytes()
        with _installed_lock(self.path()):
            with self.assertRaisesRegex(InstalledReleaseError, "writer owns"):
                self.record()
        self.assertEqual(self.path().read_bytes(), before)
        self.assertEqual(self.read(), self.metadata)
        self.record()

    def test_linked_or_public_lock_is_refused_without_overwriting_it(self):
        self.record()
        lock = self.path().with_suffix(".lock")
        before = self.path().read_bytes()
        lock.chmod(0o644)
        with self.assertRaises(InstalledReleaseError):
            self.record()
        lock.chmod(0o600)
        held = self.root / "retained-lock"
        lock.rename(held)
        lock.symlink_to(held)
        with self.assertRaises(InstalledReleaseError):
            self.record()
        self.assertTrue(lock.is_symlink())
        lock.unlink()
        os.link(held, lock)
        with self.assertRaises(InstalledReleaseError):
            self.record()
        self.assertEqual(self.path().read_bytes(), before)

    @unittest.skipUnless(
        os.geteuid() == 0, "Requires isolated controller-root ownership fixture"
    )
    def test_root_controller_preserves_trusted_service_owned_state_and_lock(self):
        from utils.infinidysk_v2_update import (
            NativeUpdateRecoveryRequired,
            _journal_lock,
        )

        service_user = next(
            (entry for entry in pwd.getpwall() if entry.pw_uid > 0), None
        )
        if service_user is None:
            self.skipTest("No unprivileged fixture owner is available")
        parent = self.root / "service-owned-updates"
        parent.mkdir(mode=0o700)
        os.chown(parent, service_user.pw_uid, service_user.pw_gid)
        for existing in (False, True):
            with self.subTest(existing_state_directory=existing):
                self.state = parent / ("existing" if existing else "new")
                if existing:
                    self.state.mkdir(mode=0o700)
                    os.chown(self.state, service_user.pw_uid, service_user.pw_gid)
                self.record()
                self.assertEqual(self.read(), self.metadata)
                for path in (self.state, self.path(), self.path().with_suffix(".lock")):
                    value = path.stat()
                    self.assertEqual(
                        (value.st_uid, value.st_gid),
                        (service_user.pw_uid, service_user.pw_gid),
                    )
                    self.assertEqual(value.st_mode & 0o077, 0)
                # Native update admission retains its stricter existing owner
                # contract; only independent installed-state locking changed.
                with self.assertRaises(NativeUpdateRecoveryRequired):
                    with _journal_lock(self.state / "native-runtime-fixture.json"):
                        self.fail("Active update ownership was weakened")
                lock = self.path().with_suffix(".lock")
                os.chown(lock, service_user.pw_uid + 100000, service_user.pw_gid)
                with self.assertRaises(InstalledReleaseError):
                    self.record()
                self.assertEqual(self.read(), self.metadata)


if __name__ == "__main__":
    unittest.main()
