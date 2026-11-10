import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from utils import frontend_source


class FrontendSourceFixture:
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "package.json").write_text('{"version":"1.2.3"}')
        self.runtime = self.root / ".output"
        (self.runtime / "server" / "chunks").mkdir(parents=True)
        (self.runtime / "public").mkdir()
        (self.runtime / "server" / "index.mjs").write_text("import './chunks/app.mjs';")
        self.chunk = self.runtime / "server" / "chunks" / "app.mjs"
        self.chunk.write_text("export const value = 'first';")
        self.info = {
            "repository": "nicocapalbo/dmbdb",
            "source_kind": "branch",
            "requested_ref": "dev",
            "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        }
        with frontend_source._CACHE_LOCK:
            frontend_source._CACHE.clear()

    def write(self, **changes):
        return frontend_source.write_frontend_source(
            self.root, {**self.info, **changes}
        )


class FrontendSourceTests(FrontendSourceFixture, unittest.TestCase):
    def test_writer_binds_complete_runtime_and_returns_only_public_identity(self):
        manifest = self.write()
        self.assertEqual(manifest["package_version"], "1.2.3")
        self.assertRegex(manifest["artifact_sha256"], r"^[0-9a-f]{64}$")
        public = frontend_source.read_installed_frontend_source(self.root)
        self.assertEqual(
            public,
            {"schema_version": 1, **self.info, "package_version": "1.2.3"},
        )
        self.assertNotIn("artifact_sha256", public)
        self.assertEqual(
            (self.root / frontend_source.MANIFEST_NAME).stat().st_mode & 0o777, 0o644
        )

    def test_changed_chunk_invalidates_marker_even_with_same_package_entry_and_size(
        self,
    ):
        self.write()
        before_package = (self.root / "package.json").read_bytes()
        before_entry = (self.runtime / "server" / "index.mjs").read_bytes()
        previous = self.chunk.stat()
        replacement = self.chunk.with_suffix(".replacement")
        replacement.write_text("export const value = 'other';")
        self.assertEqual(replacement.stat().st_size, previous.st_size)
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        replacement.replace(self.chunk)
        self.assertEqual((self.root / "package.json").read_bytes(), before_package)
        self.assertEqual(
            (self.runtime / "server" / "index.mjs").read_bytes(), before_entry
        )
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))

    def test_public_chunk_addition_and_removal_invalidate_marker(self):
        self.write()
        public = self.runtime / "public" / "asset.js"
        public.write_bytes(b"fixture")
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
        self.write()
        public.unlink()
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))

    def test_stat_signature_cache_skips_hashing_and_detects_in_place_change(self):
        self.write()
        with patch.object(frontend_source.os, "read", wraps=os.read) as reads:
            self.assertIsNotNone(
                frontend_source.read_installed_frontend_source(self.root)
            )
            reads.assert_not_called()
            self.chunk.write_text("export const value = 'other';")
            self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
            self.assertGreater(reads.call_count, 0)

    def test_runtime_installer_marker_takes_precedence_over_image_source(self):
        self.write()
        (self.root / "version.txt").write_text("custom-abcdef12\n")
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
        with self.assertRaisesRegex(
            frontend_source.FrontendSourceError, "takes precedence"
        ):
            self.write()

    def test_package_version_change_or_malformed_marker_never_returns_old_identity(
        self,
    ):
        self.write()
        (self.root / "package.json").write_text('{"version":"1.2.4"}')
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
        self.write()
        path = self.root / frontend_source.MANIFEST_NAME
        path.write_text("{")
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
        path.write_text("[]" * 10_000)
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))

    def test_invalid_identities_and_mismatched_package_version_are_rejected(self):
        for changes in (
            {"repository": "https://example.invalid/repo"},
            {"commit_sha": "short"},
            {"source_kind": ["branch"]},
            {"requested_ref": "dev\nother"},
            {"source_kind": "commit", "requested_ref": "abcdef1"},
            {"package_version": "9.9.9"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(frontend_source.FrontendSourceError):
                    self.write(**changes)
        self.assertFalse((self.root / frontend_source.MANIFEST_NAME).exists())

    def test_release_and_commit_sources_keep_exact_public_identity(self):
        for kind, requested in (
            ("release", "v1.2.3"),
            ("commit", self.info["commit_sha"]),
        ):
            with self.subTest(kind=kind):
                self.write(source_kind=kind, requested_ref=requested)
                public = frontend_source.read_installed_frontend_source(self.root)
                self.assertEqual(public["source_kind"], kind)
                self.assertEqual(public["requested_ref"], requested)

    def test_contained_relative_links_are_bound_and_external_links_refused(self):
        link = self.runtime / "server" / "linked.mjs"
        link.symlink_to("chunks/app.mjs")
        self.write()
        self.assertIsNotNone(frontend_source.read_installed_frontend_source(self.root))
        link.unlink()
        outside = self.root / "outside.mjs"
        outside.write_text("outside")
        link.symlink_to("../../outside.mjs")
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))
        with self.assertRaises(frontend_source.FrontendSourceError):
            self.write()

    def test_absolute_links_special_files_and_manifest_links_are_refused(self):
        link = self.runtime / "server" / "linked.mjs"
        link.symlink_to(self.chunk)
        with self.assertRaises(frontend_source.FrontendSourceError):
            self.write()
        link.unlink()
        os.mkfifo(self.runtime / "fifo")
        with self.assertRaises(frontend_source.FrontendSourceError):
            self.write()
        (self.runtime / "fifo").unlink()
        self.write()
        manifest = self.root / frontend_source.MANIFEST_NAME
        original = self.root / "original.json"
        manifest.rename(original)
        manifest.symlink_to(original.name)
        self.assertIsNone(frontend_source.read_installed_frontend_source(self.root))

    def test_resource_limits_and_failures_close_all_descriptors(self):
        before = len(list(Path("/proc/self/fd").iterdir()))
        for _ in range(12):
            with patch.object(frontend_source, "MAX_ENTRIES", 2):
                with self.assertRaises(frontend_source.FrontendSourceError):
                    self.write()
            with patch.object(frontend_source, "MAX_RUNTIME_BYTES", 1):
                with self.assertRaises(frontend_source.FrontendSourceError):
                    self.write()
        self.assertEqual(len(list(Path("/proc/self/fd").iterdir())), before)

    def test_changed_file_during_hashing_is_not_published(self):
        original = frontend_source.os.read
        changed = False

        def read(fd, amount):
            nonlocal changed
            value = original(fd, amount)
            if not changed:
                changed = True
                self.chunk.write_text("export const value = 'other';")
            return value

        with patch.object(frontend_source.os, "read", side_effect=read):
            with self.assertRaisesRegex(frontend_source.FrontendSourceError, "changed"):
                self.write()
        self.assertFalse((self.root / frontend_source.MANIFEST_NAME).exists())


class FrontendSourceApiTests(FrontendSourceFixture, unittest.TestCase):
    def test_existing_process_routes_deliver_public_identity_without_version_policy_change(
        self,
    ):
        test_id = (
            f"tests.{Path(__file__).stem}.{type(self).__name__}.{self._testMethodName}"
        )
        if os.environ.get("DUMB_FRONTEND_SOURCE_API_TEST") != test_id:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", test_id],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, "DUMB_FRONTEND_SOURCE_API_TEST": test_id},
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return

        from api.routers import process

        self.write()
        config = {
            "process_name": "DUMB Frontend",
            "config_dir": str(self.root),
            "enabled": True,
            "branch_enabled": False,
            "commit_sha": "",
        }
        with (
            patch.object(process.CONFIG_MANAGER, "config", {"dumb_frontend": config}),
            patch.object(
                process.CONFIG_MANAGER,
                "find_key_for_process",
                return_value=("dumb_frontend", None),
            ),
            patch.object(process, "find_service_config", return_value=config),
            patch.object(
                process.versions, "version_check", return_value=("v1.2.3", None)
            ),
        ):
            single = process.fetch_process(
                "DUMB Frontend",
                logger=Mock(),
                api_state=None,
                updater=None,
                current_user=None,
            )
            collection = process._collect_process_entries()
            for row in (single, collection[0]):
                self.assertEqual(row["version"], "v1.2.3")
                self.assertEqual(row["installed_source"]["requested_ref"], "dev")
                self.assertEqual(
                    row["installed_source"]["commit_sha"], self.info["commit_sha"]
                )
                self.assertNotIn("artifact_sha256", row["installed_source"])
            self.chunk.write_text("replaced")
            self.assertIsNone(
                process.fetch_process(
                    "DUMB Frontend",
                    logger=Mock(),
                    api_state=None,
                    updater=None,
                    current_user=None,
                )["installed_source"]
            )
            self.assertIsNone(process._collect_process_entries()[0]["installed_source"])
        self.assertFalse(config["branch_enabled"])
        self.assertEqual(config["commit_sha"], "")
