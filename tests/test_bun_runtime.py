import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import bun_runtime, setup


class FakeProcessHandler:
    def __init__(self, *, returncode=0, start_error=None):
        self.returncode = returncode
        self.stderr = "download failed" if returncode else ""
        self.stdout = ""
        self.start_error = start_error
        self.started = []
        self.waited = []

    def start_process(self, *args, **kwargs):
        self.started.append((args, kwargs))
        if self.start_error:
            return False, self.start_error
        if args[0] == "bun_install_runtime":
            candidate = Path(kwargs["env"]["BUN_INSTALL"]) / "bin" / "bun"
            candidate.parent.mkdir()
            candidate.write_bytes(b"validated Bun candidate")
        return True, None

    def wait(self, name):
        self.waited.append(name)


class BunRuntimeTests(unittest.TestCase):
    def test_current_and_newer_runtimes_do_not_download(self):
        for version in ("1.4.3", "1.4.10", "1.5.0", "2.0.0"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                binary = Path(temp_dir) / "bin" / "bun"
                binary.parent.mkdir()
                binary.write_bytes(b"existing Bun")
                handler = FakeProcessHandler()
                with patch.object(
                    bun_runtime.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, version, ""),
                ):
                    success, error = bun_runtime.ensure_bun_runtime(
                        handler, temp_dir, {"BUN_INSTALL": temp_dir}, 123, 456
                    )
                self.assertTrue(success, error)
                self.assertEqual(handler.started, [])
                self.assertEqual(binary.read_bytes(), b"existing Bun")

    def test_old_and_missing_runtimes_are_validated_before_replacement(self):
        for old_version in ((1, 3, 14), None):
            with (
                self.subTest(old_version=old_version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                binary = Path(temp_dir) / "bin" / "bun"
                if old_version:
                    binary.parent.mkdir()
                    binary.write_bytes(b"old Bun")
                handler = FakeProcessHandler()
                env = {"BUN_INSTALL": temp_dir, "HOME": "/example/service"}
                version_checks = ([old_version] if old_version else []) + [(1, 4, 3)]
                with (
                    patch.object(bun_runtime, "chown_recursive"),
                    patch.object(
                        bun_runtime, "_bun_version", side_effect=version_checks
                    ),
                ):
                    success, error = bun_runtime.ensure_bun_runtime(
                        handler, temp_dir, env, 123, 456
                    )
                self.assertTrue(success, error)
                self.assertEqual(binary.read_bytes(), b"validated Bun candidate")
                args, kwargs = handler.started[0]
                self.assertEqual(args[2][-1], "bun-v1.4.3")
                self.assertIn("pipefail", args[2])
                staging = kwargs["env"]["BUN_INSTALL"]
                self.assertNotEqual(staging, temp_dir)
                self.assertEqual(kwargs["env"]["HOME"], staging)
                self.assertFalse(Path(staging).exists())
                self.assertEqual(env["HOME"], "/example/service")

    def test_failed_or_invalid_upgrade_preserves_existing_binary(self):
        for returncode, candidate_version, expected_error in (
            (1, None, "download failed"),
            (0, (1, 3, 14), "expected"),
            (0, None, "unreadable"),
        ):
            with (
                self.subTest(expected_error=expected_error),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                binary = Path(temp_dir) / "bin" / "bun"
                binary.parent.mkdir()
                binary.write_bytes(b"old Bun")
                handler = FakeProcessHandler(returncode=returncode)
                with (
                    patch.object(bun_runtime, "chown_recursive"),
                    patch.object(
                        bun_runtime,
                        "_bun_version",
                        side_effect=[(1, 3, 14), candidate_version],
                    ),
                ):
                    success, error = bun_runtime.ensure_bun_runtime(
                        handler, temp_dir, {"BUN_INSTALL": temp_dir}, 123, 456
                    )
                self.assertFalse(success)
                self.assertIn(expected_error, error)
                self.assertEqual(binary.read_bytes(), b"old Bun")
                self.assertFalse(
                    Path(handler.started[0][1]["env"]["BUN_INSTALL"]).exists()
                )

    def test_runtime_start_failure_is_reported_without_waiting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FakeProcessHandler(start_error="permission denied")
            with patch.object(bun_runtime, "chown_recursive"):
                success, error = bun_runtime.ensure_bun_runtime(
                    handler, temp_dir, {"BUN_INSTALL": temp_dir}, 123, 456
                )
            self.assertFalse(success)
            self.assertIn("permission denied", error)
            self.assertEqual(handler.waited, [])
            self.assertFalse(Path(handler.started[0][1]["env"]["BUN_INSTALL"]).exists())

    def test_version_probe_handles_missing_timed_out_and_invalid_runtimes(self):
        for exception in (FileNotFoundError(), subprocess.TimeoutExpired("bun", 15)):
            with (
                self.subTest(exception=exception),
                patch.object(bun_runtime.subprocess, "run", side_effect=exception),
            ):
                self.assertIsNone(bun_runtime._bun_version("/example/bun", {}))
        for result in (
            subprocess.CompletedProcess([], 1, "", "broken binary"),
            subprocess.CompletedProcess([], 0, "unexpected output", ""),
        ):
            with (
                self.subTest(result=result),
                patch.object(bun_runtime.subprocess, "run", return_value=result),
            ):
                self.assertIsNone(bun_runtime._bun_version("/example/bun", {}))


class BunEnvironmentTests(unittest.TestCase):
    def _setup(self, root, handler, runtime_result=(True, None)):
        with (
            patch.dict(os.environ, {"BUN_INSTALL": str(root / "runtime")}),
            patch.object(setup, "chown_recursive"),
            patch.object(setup, "_prepare_shared_build_cache"),
            patch.object(setup, "ensure_bun_runtime", return_value=runtime_result),
        ):
            return setup.setup_bun_environment(handler, str(root))

    def test_lockfiles_remain_frozen_after_runtime_upgrade(self):
        for lockfile in ("bun.lock", "bun.lockb", None):
            with (
                self.subTest(lockfile=lockfile),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                (root / "package.json").write_text(
                    '{"scripts": {"build": "build", "migrate": "migrate"}}'
                )
                if lockfile:
                    (root / lockfile).write_text('{"lockfileVersion": 2}')
                handler = FakeProcessHandler()
                success, error = self._setup(root, handler)
                self.assertTrue(success, error)
                self.assertEqual(
                    [args[0] for args, _kwargs in handler.started],
                    ["bun_install", "bun_build", "bun_migrate"],
                )
                self.assertEqual(
                    "--frozen-lockfile" in handler.started[0][0][2], bool(lockfile)
                )
                if lockfile:
                    self.assertEqual(
                        (root / lockfile).read_text(), '{"lockfileVersion": 2}'
                    )

    def test_runtime_upgrade_failure_prevents_dependency_install(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            handler = FakeProcessHandler()
            success, error = self._setup(
                Path(temp_dir), handler, (False, "runtime validation failed")
            )
            self.assertFalse(success)
            self.assertIn("runtime validation failed", error)
            self.assertEqual(handler.started, [])

    def test_failed_frozen_install_never_retries_without_lockfile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "bun.lock").write_text('{"lockfileVersion": 2}')
            handler = FakeProcessHandler(returncode=1)
            handler.stderr = "lockfile had changes, but lockfile is frozen"
            success, error = self._setup(root, handler)
            self.assertFalse(success)
            self.assertIn("lockfile is frozen", error)
            self.assertEqual(len(handler.started), 1)
            self.assertIn("--frozen-lockfile", handler.started[0][0][2])


if __name__ == "__main__":
    unittest.main()
