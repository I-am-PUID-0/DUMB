import json
import errno
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import install_cache as install_cache_module
from utils.install_cache import InstallCache
from utils.transactional_install import (
    DeferredClearTransaction,
    DirectoryReleaseTransaction,
    RuntimeRollbackSnapshot,
    TransactionError,
)


class InstallCacheTests(unittest.TestCase):
    def test_changed_cache_root_owner_repairs_complete_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir, "cache")
            nested = root / "downloads" / "objects"
            nested.mkdir(parents=True)
            cached_file = nested / "release.bin"
            cached_file.write_bytes(b"cached")
            real_lstat = Path.lstat

            def changed_root_owner(path):
                info = real_lstat(path)
                if Path(path) == root:
                    return type(
                        "ChangedOwnerStat",
                        (),
                        {
                            "st_mode": info.st_mode,
                            "st_uid": info.st_uid + 1,
                            "st_gid": info.st_gid + 1,
                        },
                    )()
                return info

            with (
                patch.object(Path, "lstat", changed_root_owner),
                patch.object(install_cache_module, "_secure_cache_entry") as secure,
            ):
                install_cache_module._repair_cache_tree(
                    root, os.geteuid(), os.getegid()
                )

            secured_paths = {Path(call.args[0]) for call in secure.call_args_list}
            self.assertIn(root, secured_paths)
            self.assertIn(root / "downloads", secured_paths)
            self.assertIn(nested, secured_paths)
            self.assertIn(cached_file, secured_paths)

    def test_cache_mode_repair_preserves_executable_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            executable = root / "runtime"
            executable.write_bytes(b"runtime")
            executable.chmod(0o777)
            ordinary = root / "manifest.json"
            ordinary.write_text("{}", encoding="utf-8")
            ordinary.chmod(0o666)
            writable_dir = root / "dependencies"
            writable_dir.mkdir(mode=0o777)
            writable_dir.chmod(0o777)

            for path in (executable, ordinary, writable_dir):
                install_cache_module._secure_cache_entry(
                    path, os.geteuid(), os.getegid()
                )

            self.assertEqual(executable.stat().st_mode & 0o777, 0o755)
            self.assertEqual(ordinary.stat().st_mode & 0o777, 0o644)
            self.assertEqual(writable_dir.stat().st_mode & 0o777, 0o755)

    def test_symlinked_cache_root_uses_isolated_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("keep", encoding="utf-8")
            configured = root / "cache"
            configured.symlink_to(target, target_is_directory=True)
            fallback = root / "fallback"
            fallback.mkdir()
            cache = InstallCache(configured)

            with patch.object(
                install_cache_module,
                "_isolated_fallback_cache_root",
                return_value=fallback,
            ):
                cache.ensure()

            self.assertEqual(cache.root, fallback)
            self.assertTrue(cache.fallback_reason)
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
            self.assertTrue(cache.telemetry_db.is_file())

    def test_permission_failure_uses_isolated_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = root / "cache"
            configured.mkdir()
            fallback = root / "fallback"
            fallback.mkdir()
            cache = InstallCache(configured)
            real_repair = install_cache_module._repair_cache_tree

            def deny_configured(path, user_id, group_id):
                if Path(path) == configured:
                    raise PermissionError("simulated ownership denial")
                return real_repair(Path(path), user_id, group_id)

            with (
                patch.object(
                    install_cache_module,
                    "_repair_cache_tree",
                    side_effect=deny_configured,
                ),
                patch.object(
                    install_cache_module,
                    "_isolated_fallback_cache_root",
                    return_value=fallback,
                ),
            ):
                cache.ensure()

            status = cache.status()
            self.assertTrue(status["using_fallback"])
            self.assertIn("ownership denial", status["fallback_reason"])
            self.assertEqual(status["path"], str(fallback))
            self.assertEqual(status["configured_path"], str(configured))

    def test_mounted_cache_namespace_uses_isolated_fallback(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            configured = root / "cache"
            configured.mkdir()
            fallback = root / "fallback"
            fallback.mkdir()
            cache = InstallCache(configured)
            real_is_mount = Path.is_mount

            def mounted_artifacts(path):
                if Path(path) == configured / "artifacts":
                    return True
                return real_is_mount(path)

            with (
                patch.object(Path, "is_mount", mounted_artifacts),
                patch.object(
                    install_cache_module,
                    "_isolated_fallback_cache_root",
                    return_value=fallback,
                ),
            ):
                cache.ensure()

            self.assertEqual(cache.root, fallback)
            self.assertIn("mounted install-cache namespace", cache.fallback_reason)

    def test_download_cache_verifies_content_and_quarantines_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = InstallCache(Path(temp_dir, "cache"))
            metadata = cache.store_download(
                "https://example.invalid/release.zip",
                b"verified-content",
                etag='"v1"',
                filename="release.zip",
            )

            content, restored = cache.lookup_download(
                "https://example.invalid/release.zip"
            )
            self.assertEqual(content, b"verified-content")
            self.assertEqual(restored["sha256"], metadata["sha256"])

            object_path = cache._object_path(metadata["sha256"])
            object_path.write_bytes(b"corrupt")
            content, restored = cache.lookup_download(
                "https://example.invalid/release.zip"
            )
            self.assertIsNone(content)
            self.assertEqual(restored, {})
            self.assertFalse(object_path.exists())
            self.assertTrue(any(cache.quarantine.iterdir()))

    def test_artifact_restore_rejects_modified_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "server.js").write_text("working", encoding="utf-8")
            cache = InstallCache(root / "cache")
            cache.store_artifact("example", "a" * 64, source)
            artifact_file = (
                cache.artifacts / "example" / ("a" * 64) / "files" / "server.js"
            )
            artifact_file.write_text("corrupt", encoding="utf-8")

            restored, error = cache.restore_artifact(
                "example", "a" * 64, root / "restore"
            )

            self.assertFalse(restored)
            self.assertIn("manifest", error)
            self.assertFalse((cache.artifacts / "example" / ("a" * 64)).exists())

    def test_artifact_cache_rejects_symlink_escaping_the_tree(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            (source / "escape").symlink_to("../../outside")
            cache = InstallCache(root / "cache")

            with self.assertRaises(ValueError):
                cache.store_artifact("example", "b" * 64, source)

    def test_operation_status_is_persisted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = InstallCache(Path(temp_dir, "cache"))
            operation_id = cache.begin_operation("Example")
            cache.update_operation(
                operation_id,
                stage="complete",
                status="completed",
                cache_hits=1,
            )

            operation = cache.recent_operations(1)[0]
            self.assertEqual(operation["process_name"], "Example")
            self.assertEqual(operation["stage"], "complete")
            self.assertEqual(operation["cache_hits"], 1)

    def test_status_includes_legacy_cache_in_combined_total(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = InstallCache(root / "cache")
            cache.store_download("https://example.invalid/file", b"managed")
            legacy = root / "legacy-pip"
            legacy.mkdir()
            (legacy / "wheel.bin").write_bytes(b"legacy-cache")
            specs = [
                {
                    "path": str(legacy),
                    "resolved_path": str(legacy.resolve()),
                    "manager": "pip",
                    "label": "Legacy per-service pip cache",
                }
            ]

            with patch.object(cache, "_legacy_candidate_specs", return_value=specs):
                status = cache.status()

            self.assertEqual(status["legacy_bytes"], len(b"legacy-cache"))
            self.assertGreater(status["managed_bytes"], 0)
            self.assertEqual(
                status["total_bytes"],
                status["managed_bytes"] + status["legacy_bytes"],
            )
            self.assertEqual(status["legacy_entries"][0]["manager"], "pip")

    def test_legacy_discovery_matches_old_buckets_but_preserves_tpa_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_root = root / "config"
            pnpm_root = config_root / ".pnpm-store"
            legacy_bucket = pnpm_root / "nzbdav-0123456789ab"
            legacy_bucket.mkdir(parents=True)
            (legacy_bucket / "package.bin").write_bytes(b"old")
            tpa_runtime = pnpm_root / "traefik-proxy-admin-runtime"
            tpa_runtime.mkdir()
            (tpa_runtime / "pnpm-home").mkdir()
            cache = InstallCache(root / "cache", legacy_config_root=config_root)

            entries = cache.legacy_entries()

            self.assertEqual([entry["path"] for entry in entries], [str(legacy_bucket)])
            self.assertTrue(tpa_runtime.exists())

    def test_cleanup_removes_only_allowlisted_legacy_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = InstallCache(root / "cache")
            legacy = root / "legacy-nuget"
            legacy.mkdir()
            (legacy / "package.bin").write_bytes(b"reclaimable")
            protected = root / "service-data.db"
            protected.write_bytes(b"keep")
            specs = [
                {
                    "path": str(legacy),
                    "resolved_path": str(legacy.resolve()),
                    "manager": "nuget",
                    "label": "Legacy per-service NuGet packages",
                }
            ]

            with patch.object(cache, "_legacy_candidate_specs", return_value=specs):
                result = cache.cleanup(["legacy"])

            self.assertFalse(legacy.exists())
            self.assertEqual(protected.read_bytes(), b"keep")
            self.assertEqual(result["removed_bytes"], len(b"reclaimable"))
            self.assertEqual(result["errors"], [])

    def test_cleanup_rejects_unknown_scope(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = InstallCache(Path(temp_dir, "cache"))

            with self.assertRaisesRegex(ValueError, "unsupported"):
                cache.cleanup(["arbitrary-path"])


class TransactionalInstallTests(unittest.TestCase):
    def test_activation_rename_failure_restores_previous_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            target.mkdir()
            (target / "version.txt").write_text("old", encoding="utf-8")
            transaction = DirectoryReleaseTransaction(str(target), "Example")
            transaction.journal = root / "journals" / "transaction.json"
            candidate = Path(transaction.prepare())
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            real_replace = os.replace

            def fail_candidate_activation(source, destination):
                if Path(source) == candidate and Path(destination) == target:
                    raise OSError(errno.EIO, "simulated rename failure")
                return real_replace(source, destination)

            with patch(
                "utils.transactional_install.os.replace",
                side_effect=fail_candidate_activation,
            ):
                with self.assertRaises(TransactionError):
                    transaction.activate()

            self.assertEqual((target / "version.txt").read_text(), "old")

    def test_overlay_cross_device_rename_uses_copy_activation_and_can_roll_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            target.mkdir()
            (target / "version.txt").write_text("old", encoding="utf-8")
            transaction = DirectoryReleaseTransaction(str(target), "Example")
            transaction.journal = root / "journals" / "transaction.json"
            candidate = Path(transaction.prepare())
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            real_replace = os.replace

            def reject_lower_directory_rename(source, destination):
                if Path(source) == target and Path(destination) == transaction.previous:
                    raise OSError(errno.EXDEV, "simulated overlay rename")
                return real_replace(source, destination)

            with patch(
                "utils.transactional_install.os.replace",
                side_effect=reject_lower_directory_rename,
            ):
                transaction.activate()

            self.assertTrue(transaction.activated)
            self.assertEqual((target / "version.txt").read_text(), "new")
            self.assertEqual((transaction.previous / "version.txt").read_text(), "old")
            self.assertFalse(transaction.previous_staging.exists())
            self.assertTrue(transaction.rollback())
            self.assertEqual((target / "version.txt").read_text(), "old")

    def test_candidate_activation_can_roll_back(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            target.mkdir()
            (target / "version.txt").write_text("old", encoding="utf-8")
            transaction = DirectoryReleaseTransaction(str(target), "Example")
            transaction.journal = root / "journals" / "transaction.json"
            candidate = Path(transaction.prepare())
            (candidate / "version.txt").write_text("new", encoding="utf-8")

            transaction.activate()
            self.assertEqual((target / "version.txt").read_text(), "new")
            self.assertTrue(transaction.rollback())
            self.assertEqual((target / "version.txt").read_text(), "old")

    def test_runtime_snapshot_preserves_persistent_data_during_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir, "service")
            target.mkdir()
            (target / "runtime.bin").write_text("old", encoding="utf-8")
            data = target / "data"
            data.mkdir()
            (data / "database.db").write_text("original", encoding="utf-8")
            snapshot = RuntimeRollbackSnapshot(
                str(target), "Example", persistent_paths=["data"]
            )
            self.assertTrue(snapshot.capture())
            (target / "runtime.bin").write_text("broken", encoding="utf-8")
            (data / "database.db").write_text("new-data", encoding="utf-8")
            (target / "partial.bin").write_text("partial", encoding="utf-8")

            self.assertTrue(snapshot.rollback())
            self.assertEqual((target / "runtime.bin").read_text(), "old")
            self.assertEqual((data / "database.db").read_text(), "new-data")
            self.assertFalse((target / "partial.bin").exists())

    def test_recovery_restores_previous_after_interrupted_activation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            previous = root / ".service.previous"
            candidate = root / ".service.candidate"
            previous.mkdir()
            (previous / "version.txt").write_text("old", encoding="utf-8")
            candidate.mkdir()
            transaction_root = root / "transactions"
            transaction_root.mkdir()
            journal = transaction_root / "service-test.json"
            journal.write_text(
                json.dumps(
                    {
                        "target": str(target),
                        "previous": str(previous),
                        "candidate": str(candidate),
                        "state": "activating",
                    }
                ),
                encoding="utf-8",
            )

            from utils import transactional_install

            original_root = transactional_install.install_cache_root
            transactional_install.install_cache_root = lambda: root
            try:
                recovered = DirectoryReleaseTransaction.recover_incomplete(str(target))
            finally:
                transactional_install.install_cache_root = original_root

            self.assertIn("restored_previous", recovered)
            self.assertEqual((target / "version.txt").read_text(), "old")
            self.assertFalse(journal.exists())

    def test_recovery_rolls_back_activated_but_uncommitted_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            previous = root / ".service.previous"
            candidate = root / ".service.candidate"
            target.mkdir()
            previous.mkdir()
            candidate.mkdir()
            (target / "version.txt").write_text("new", encoding="utf-8")
            (previous / "version.txt").write_text("old", encoding="utf-8")
            transaction_root = root / "transactions"
            transaction_root.mkdir()
            journal = transaction_root / "service-test.json"
            journal.write_text(
                json.dumps(
                    {
                        "target": str(target),
                        "previous": str(previous),
                        "candidate": str(candidate),
                        "state": "activated",
                    }
                ),
                encoding="utf-8",
            )

            from utils import transactional_install

            with patch.object(
                transactional_install, "install_cache_root", return_value=root
            ):
                recovered = DirectoryReleaseTransaction.recover_incomplete(str(target))

            self.assertIn("rolled_back_uncommitted_activation", recovered)
            self.assertEqual((target / "version.txt").read_text(), "old")
            self.assertFalse(journal.exists())

    def test_recovery_restores_overlay_copy_during_target_replacement(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = root / "service"
            previous = root / ".service.previous"
            candidate = root / ".service.candidate"
            target.mkdir()
            previous.mkdir()
            candidate.mkdir()
            (target / "partial.txt").write_text("partial", encoding="utf-8")
            (previous / "version.txt").write_text("old", encoding="utf-8")
            (candidate / "version.txt").write_text("new", encoding="utf-8")
            transaction_root = root / "transactions"
            transaction_root.mkdir()
            journal = transaction_root / "service-test.json"
            journal.write_text(
                json.dumps(
                    {
                        "target": str(target),
                        "previous": str(previous),
                        "candidate": str(candidate),
                        "state": "replacing_overlay_target",
                    }
                ),
                encoding="utf-8",
            )

            from utils import transactional_install

            with patch.object(
                transactional_install, "install_cache_root", return_value=root
            ):
                recovered = DirectoryReleaseTransaction.recover_incomplete(str(target))

            self.assertIn("restored_overlay_previous", recovered)
            self.assertEqual((target / "version.txt").read_text(), "old")
            self.assertFalse((target / "partial.txt").exists())
            self.assertFalse(candidate.exists())
            self.assertFalse(journal.exists())

    def test_deferred_clear_can_restore_after_partial_enospc_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir, "service")
            target.mkdir()
            (target / "one.txt").write_text("one", encoding="utf-8")
            (target / "two.txt").write_text("two", encoding="utf-8")
            transaction = DeferredClearTransaction(str(target))
            real_replace = os.replace
            moves = 0

            def fail_second_move(source, destination):
                nonlocal moves
                if Path(source).parent == target:
                    moves += 1
                    if moves == 2:
                        raise OSError(errno.ENOSPC, "simulated full filesystem")
                return real_replace(source, destination)

            with patch(
                "utils.transactional_install.os.replace", side_effect=fail_second_move
            ):
                with self.assertRaises(OSError):
                    transaction.capture()

            self.assertTrue(transaction.rollback())
            self.assertEqual((target / "one.txt").read_text(), "one")
            self.assertEqual((target / "two.txt").read_text(), "two")


if __name__ == "__main__":
    unittest.main()
