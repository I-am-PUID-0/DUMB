import hashlib
import io
import os
import stat
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests

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
        return True, None

    def wait(self, name):
        self.waited.append(name)


class BunRuntimeTests(unittest.TestCase):
    @staticmethod
    def _archive(name="bun-linux-x64-baseline.zip", entries=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            if entries is None:
                entries = [(f"{name[:-4]}/bun", b"validated Bun candidate")]
            for member, content in entries:
                bundle.writestr(member, content)
        return output.getvalue()

    def _downloads(self, *, archive=None, manifest=None, events=None):
        archive = archive if archive is not None else self._archive()
        digest = hashlib.sha256(archive).hexdigest()
        manifest = manifest or f"{digest}  bun-linux-x64-baseline.zip\n".encode()

        def download(name, destination, _limit):
            if events is not None:
                events.append(name)
            if name == "SHASUMS256.txt":
                destination.write_bytes(manifest)
            elif name == "SHASUMS256.txt.asc":
                destination.write_bytes(b"signed manifest fixture")
            else:
                destination.write_bytes(archive)

        return download

    def _ensure(self, root, **extra_env):
        return bun_runtime.ensure_bun_runtime(
            FakeProcessHandler(),
            str(root),
            {"BUN_INSTALL": str(root), "HOME": "/example/service", **extra_env},
            os.getuid(),
            os.getgid(),
        )

    def test_current_and_newer_runtimes_do_not_download(self):
        for version in ("1.4.3", "1.4.10", "1.5.0", "2.0.0"):
            with (
                self.subTest(version=version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                binary = root / "bin" / "bun"
                binary.parent.mkdir()
                binary.write_bytes(b"existing Bun")
                with (
                    patch.object(
                        bun_runtime.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess([], 0, version, ""),
                    ),
                    patch.object(bun_runtime, "_download_bun_file") as download,
                    patch.object(bun_runtime, "verify_publisher_signature") as verify,
                ):
                    success, error = self._ensure(root)
                self.assertTrue(success, error)
                download.assert_not_called()
                verify.assert_not_called()
                self.assertEqual(binary.read_bytes(), b"existing Bun")

    def test_authenticated_archive_precedes_extraction_probe_and_atomic_activation(
        self,
    ):
        for old_version in ((1, 3, 14), None):
            with (
                self.subTest(old_version=old_version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                binary = root / "bin" / "bun"
                if old_version:
                    binary.parent.mkdir()
                    binary.write_bytes(b"old Bun")
                events = []
                extracted = bun_runtime._extract_bun_binary
                archive_verified = bun_runtime._verify_bun_archive

                def probe(candidate, env):
                    if candidate == binary:
                        return old_version
                    events.append("probe")
                    self.assertEqual(Path(env["BUN_INSTALL"]).parent, Path("/tmp"))
                    self.assertEqual(env["HOME"], env["BUN_INSTALL"])
                    for variable in (
                        "LD_PRELOAD",
                        "LD_LIBRARY_PATH",
                        "LD_AUDIT",
                        "DYLD_INSERT_LIBRARIES",
                        "GLIBC_TUNABLES",
                        "BUN_OPTIONS",
                        "NODE_OPTIONS",
                    ):
                        self.assertNotIn(variable, env)
                    self.assertEqual(candidate.read_bytes(), b"validated Bun candidate")
                    self.assertEqual(candidate.stat().st_mode & 0o777, 0o755)
                    self.assertEqual(binary.exists(), bool(old_version))
                    return (1, 4, 3)

                def verify_checksum(*args):
                    events.append("checksum")
                    return archive_verified(*args)

                def extract(*args):
                    events.append("extract")
                    return extracted(*args)

                with (
                    patch.object(
                        bun_runtime,
                        "_bun_archive_name",
                        return_value="bun-linux-x64-baseline.zip",
                    ),
                    patch.object(
                        bun_runtime,
                        "_download_bun_file",
                        side_effect=self._downloads(events=events),
                    ),
                    patch.object(
                        bun_runtime,
                        "verify_publisher_signature",
                        side_effect=lambda *_args: events.append("signature"),
                    ) as verify,
                    patch.object(
                        bun_runtime, "_verify_bun_archive", side_effect=verify_checksum
                    ),
                    patch.object(
                        bun_runtime, "_extract_bun_binary", side_effect=extract
                    ),
                    patch.object(bun_runtime, "_bun_version", side_effect=probe),
                ):
                    extra_env = {
                        name: "example"
                        for name in (
                            "LD_PRELOAD",
                            "LD_LIBRARY_PATH",
                            "LD_AUDIT",
                            "DYLD_INSERT_LIBRARIES",
                            "GLIBC_TUNABLES",
                            "BUN_OPTIONS",
                            "NODE_OPTIONS",
                        )
                    }
                    success, error = self._ensure(root, **extra_env)
                self.assertTrue(success, error)
                self.assertTrue(all(value == "example" for value in extra_env.values()))
                self.assertEqual(
                    events,
                    [
                        "SHASUMS256.txt",
                        "SHASUMS256.txt.asc",
                        "signature",
                        "bun-linux-x64-baseline.zip",
                        "checksum",
                        "extract",
                        "probe",
                    ],
                )
                self.assertEqual(verify.call_args.args[2], "bun")
                self.assertEqual(binary.read_bytes(), b"validated Bun candidate")
                self.assertEqual(list(root.glob(".dumb-bun-*")), [])

    def test_signature_or_checksum_failures_preserve_existing_binary_without_executing_candidate(
        self,
    ):
        for failure in (
            "signature",
            "checksum",
            "missing checksum",
            "duplicate checksum",
            "download",
        ):
            with (
                self.subTest(failure=failure),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                binary = root / "bin" / "bun"
                binary.parent.mkdir()
                binary.write_bytes(b"old Bun")
                archive = self._archive()
                correct = f"{hashlib.sha256(archive).hexdigest()}  bun-linux-x64-baseline.zip\n".encode()
                manifest = {
                    "checksum": b"0" * 64 + b"  bun-linux-x64-baseline.zip\n",
                    "missing checksum": b"0" * 64 + b"  another.zip\n",
                    "duplicate checksum": correct + correct,
                }.get(failure, correct)
                signature_error = (
                    bun_runtime.ArtifactVerificationError("bad signature")
                    if failure == "signature"
                    else None
                )
                with (
                    patch.object(
                        bun_runtime,
                        "_bun_archive_name",
                        return_value="bun-linux-x64-baseline.zip",
                    ),
                    patch.object(
                        bun_runtime,
                        "_download_bun_file",
                        side_effect=(
                            requests.Timeout("download failed")
                            if failure == "download"
                            else self._downloads(archive=archive, manifest=manifest)
                        ),
                    ) as download,
                    patch.object(
                        bun_runtime,
                        "verify_publisher_signature",
                        side_effect=signature_error,
                    ),
                    patch.object(
                        bun_runtime, "_bun_version", return_value=(1, 3, 14)
                    ) as probe,
                ):
                    success, error = self._ensure(root)
                self.assertFalse(success)
                self.assertIn("preserved", error)
                self.assertEqual(probe.call_count, 1)
                self.assertEqual(binary.read_bytes(), b"old Bun")
                self.assertEqual(list(root.glob(".dumb-bun-*")), [])
                if failure == "signature":
                    self.assertEqual(download.call_count, 2)

    def test_invalid_candidate_or_activation_failure_preserves_existing_binary(self):
        for candidate_version, activation_error in (
            ((1, 3, 14), None),
            (None, None),
            ((1, 4, 3), PermissionError("permission denied")),
        ):
            with (
                self.subTest(candidate_version=candidate_version),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                binary = root / "bin" / "bun"
                binary.parent.mkdir()
                binary.write_bytes(b"old Bun")
                with (
                    patch.object(
                        bun_runtime,
                        "_bun_archive_name",
                        return_value="bun-linux-x64-baseline.zip",
                    ),
                    patch.object(
                        bun_runtime, "_download_bun_file", side_effect=self._downloads()
                    ),
                    patch.object(bun_runtime, "verify_publisher_signature"),
                    patch.object(
                        bun_runtime,
                        "_bun_version",
                        side_effect=[(1, 3, 14), candidate_version],
                    ),
                    patch.object(
                        bun_runtime.os, "replace", side_effect=activation_error
                    ),
                ):
                    success, error = self._ensure(root)
                self.assertFalse(success)
                self.assertIn("preserved", error)
                self.assertEqual(binary.read_bytes(), b"old Bun")
                self.assertEqual(list(root.glob(".dumb-bun-*")), [])

    def test_zip_paths_symlinks_and_duplicate_executables_are_rejected_before_probe(
        self,
    ):
        expected = "bun-linux-x64-baseline/bun"
        symlink = zipfile.ZipInfo(expected)
        symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
        entries = [
            [("../outside", b"bad")],
            [("/absolute", b"bad")],
            [("bun-linux-x64-baseline/../bun", b"bad")],
            [("bun-linux-x64-baseline\\bun", b"bad")],
            [(symlink, b"outside")],
            [(expected, b"one"), (expected, b"two")],
            [(expected, b"")],
        ]
        for members in entries:
            with (
                self.subTest(members=members),
                tempfile.TemporaryDirectory() as temp_dir,
            ):
                root = Path(temp_dir)
                with (
                    patch.object(
                        bun_runtime,
                        "_bun_archive_name",
                        return_value="bun-linux-x64-baseline.zip",
                    ),
                    patch.object(
                        bun_runtime,
                        "_download_bun_file",
                        side_effect=self._downloads(
                            archive=self._archive(entries=members)
                        ),
                    ),
                    patch.object(bun_runtime, "verify_publisher_signature"),
                    patch.object(bun_runtime, "_bun_version") as probe,
                ):
                    success, error = self._ensure(root)
                self.assertFalse(success)
                self.assertIn("preserved", error)
                probe.assert_not_called()
                self.assertFalse((root / "bin" / "bun").exists())
                self.assertEqual(list(root.iterdir()), [])

    def test_supported_architecture_aliases_and_unsupported_platforms(self):
        for machine, musl, expected in (
            ("x86_64", False, "bun-linux-x64-baseline.zip"),
            ("amd64", True, "bun-linux-x64-musl-baseline.zip"),
            ("aarch64", False, "bun-linux-aarch64.zip"),
            ("arm64", True, "bun-linux-aarch64-musl.zip"),
        ):
            with (
                self.subTest(machine=machine, musl=musl),
                patch.object(bun_runtime.platform, "system", return_value="Linux"),
                patch.object(bun_runtime.platform, "machine", return_value=machine),
                patch.object(bun_runtime.Path, "is_file", return_value=musl),
            ):
                self.assertEqual(bun_runtime._bun_archive_name(), expected)
        for system, machine in (
            ("Darwin", "arm64"),
            ("Linux", "armv7l"),
            ("Linux", "riscv64"),
        ):
            with (
                self.subTest(system=system, machine=machine),
                patch.object(bun_runtime.platform, "system", return_value=system),
                patch.object(bun_runtime.platform, "machine", return_value=machine),
            ):
                with self.assertRaises(bun_runtime.ArtifactVerificationError):
                    bun_runtime._bun_archive_name()

    def test_streamed_download_uses_fixed_release_tls_auth_timeout_and_size_limit(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.iter_content.return_value = [b"ab", b"cd"]
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "download.zip"
            with (
                patch.object(bun_runtime.requests, "get", return_value=response) as get,
                patch.object(
                    bun_runtime.Downloader,
                    "get_headers",
                    return_value={"Authorization": "token example"},
                ),
            ):
                bun_runtime._download_bun_file("bun-linux-aarch64.zip", destination, 4)
            self.assertEqual(destination.read_bytes(), b"abcd")
            self.assertEqual(
                get.call_args.args[0],
                "https://github.com/oven-sh/bun/releases/download/bun-v1.4.3/bun-linux-aarch64.zip",
            )
            self.assertEqual(
                get.call_args.kwargs,
                {
                    "headers": {"Authorization": "token example"},
                    "stream": True,
                    "timeout": (15, 300),
                },
            )
            response.iter_content.return_value = [b"ab", b"cd"]
            with (
                patch.object(bun_runtime.requests, "get", return_value=response),
                patch.object(bun_runtime.Downloader, "get_headers", return_value={}),
            ):
                with self.assertRaises(bun_runtime.ArtifactVerificationError):
                    bun_runtime._download_bun_file(
                        "bun-linux-aarch64.zip", destination, 3
                    )

    def test_service_parent_rename_cannot_substitute_verified_activation_bytes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "verified"
            source.write_bytes(b"authenticated candidate")
            binary = root / "runtime" / "bin" / "bun"
            original_copy = bun_runtime.shutil.copyfileobj
            substitutions = []

            def copy_then_substitute(*args, **kwargs):
                original_copy(*args, **kwargs)
                stage = next(binary.parent.glob(".dumb-bun-activate-*"))
                renamed = stage.with_name(stage.name + "-renamed")
                stage.rename(renamed)
                stage.mkdir(mode=0o700)
                (stage / "bun").write_bytes(b"unverified substitute")
                substitutions.append(stage)

            with patch.object(
                bun_runtime.shutil, "copyfileobj", side_effect=copy_then_substitute
            ):
                bun_runtime._activate_bun_binary(
                    source, binary, os.getuid(), os.getgid()
                )
            self.assertEqual(binary.read_bytes(), b"authenticated candidate")
            self.assertEqual(source.read_bytes(), b"authenticated candidate")
            self.assertEqual(
                (substitutions[0] / "bun").read_bytes(), b"unverified substitute"
            )
            self.assertFalse(
                substitutions[0].with_name(substitutions[0].name + "-renamed").exists()
            )

    def test_verification_temp_root_requires_root_owner_and_sticky_writable_directory(
        self,
    ):
        for mode, uid, allowed in (
            (stat.S_IFDIR | 0o1777, 0, True),
            (stat.S_IFDIR | 0o755, 0, True),
            (stat.S_IFDIR | 0o777, 0, False),
            (stat.S_IFDIR | 0o1777, 1000, False),
            (stat.S_IFLNK | 0o777, 0, False),
        ):
            with (
                self.subTest(mode=mode, uid=uid),
                patch.object(
                    bun_runtime.Path,
                    "lstat",
                    return_value=Mock(st_mode=mode, st_uid=uid),
                ),
            ):
                if allowed:
                    self.assertEqual(bun_runtime._trusted_bun_temp_root(), Path("/tmp"))
                else:
                    with self.assertRaises(bun_runtime.ArtifactVerificationError):
                        bun_runtime._trusted_bun_temp_root()

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
