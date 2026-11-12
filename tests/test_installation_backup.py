"""Disposable complete-state and hostile-input proofs; never use managed live paths."""

from contextlib import ExitStack, contextmanager
from importlib import import_module
import io
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from types import SimpleNamespace
import tarfile
import tempfile
import threading
import traceback
import shutil
import unittest
from unittest.mock import patch

from utils.backup_storage import BackupStorage, BackupStorageError
from utils.installation_backup import (
    InstallationBackupManager,
    InstallationBackupError,
    _owned_child,
)
from utils.installation_backup_archive import (
    FORMAT,
    SCHEMA,
    InstallationArchiveError,
    _header,
    identity,
    inspect_archive,
    inventory,
    write_archive,
)
from utils.installation_backup_restore import prepare, apply, rollback, validate
from utils.installation_backup_inventory import capacity, collect_roots, compatibility


class Config:
    def __init__(self, path, config):
        self.file_path, self.config = str(path), config
        self.save_config()

    def get(self, key, section=None):
        return (
            self.config.get(section, {}).get(key) if section else self.config.get(key)
        )

    def save_config(self):
        Path(self.file_path).write_text(json.dumps(self.config))
        Path(self.file_path).chmod(0o600)


class Process:
    pid = None

    def __init__(self):
        self.stopped = False

    def poll(self):
        return 0 if self.stopped else None


class Handler:
    shutting_down = False

    def __init__(self):
        self.process_names = {"App": Process()}
        self.external_processes = {}
        self.events = []

    @staticmethod
    def _prefixed_name(name):
        return name

    @staticmethod
    def is_startup_complete():
        return True

    def stop_process(self, name):
        self.events.append(("stop", name))
        self.process_names[name].stopped = True

    def start_process(self, name):
        self.events.append(("start", name))
        self.process_names[name] = Process()
        return True, None


class PrivateControllerDirectoryTests(unittest.TestCase):
    def test_new_control_directories_clear_inherited_sgid_without_changing_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "config"
            parent.mkdir(mode=0o700)
            parent.chmod(0o2700)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _owned_child(descriptor, "installation-backups") as root:
                    value = os.fstat(root)
                    self.assertEqual(stat.S_IMODE(value.st_mode), 0o700)
                    self.assertEqual(
                        (value.st_uid, value.st_gid), (os.geteuid(), os.getegid())
                    )
                    with _owned_child(root, "jobs") as jobs:
                        self.assertEqual(stat.S_IMODE(os.fstat(jobs).st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o2700)
            finally:
                os.close(descriptor)

    @unittest.skipUnless(
        os.geteuid() == 0, "Requires root only to simulate inherited foreign parent GID"
    )
    def test_new_private_root_uses_controller_group_instead_of_inherited_parent_group(
        self,
    ):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "config"
            parent.mkdir(mode=0o700)
            os.chown(parent, os.geteuid(), os.getegid() + 1)
            parent.chmod(0o2700)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with _owned_child(descriptor, "installation-backups") as root:
                    value = os.fstat(root)
                    self.assertEqual(
                        (value.st_uid, value.st_gid), (os.geteuid(), os.getegid())
                    )
                    self.assertEqual(stat.S_IMODE(value.st_mode), 0o700)
                self.assertEqual(parent.stat().st_gid, os.getegid() + 1)
                self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o2700)
            finally:
                os.close(descriptor)

    def test_existing_shared_or_unsupported_special_directory_is_never_changed(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for mode in (0o750, 0o770, 0o1700, 0o4700):
                    child = parent / ("state-" + str(mode))
                    child.mkdir(mode=0o700)
                    child.chmod(mode)
                    with (
                        self.subTest(mode=oct(mode)),
                        self.assertRaises(InstallationBackupError),
                    ):
                        with _owned_child(descriptor, child.name):
                            self.fail("Unsafe state was accepted")
                    self.assertEqual(stat.S_IMODE(child.stat().st_mode), mode)
            finally:
                os.close(descriptor)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.scratch = self.root / "scratch"
        self.scratch.mkdir()
        self.roots = [
            {
                "id": "root_000",
                "path": str(self.source),
                "identity": identity(self.source.stat()),
                "kind": "directory",
                "roles": ["application"],
            }
        ]
        self.metadata = {
            "format": FORMAT,
            "schema_version": SCHEMA,
            "roots": self.roots,
        }

    def archive(self):
        handle = io.BytesIO()
        write_archive(handle, self.metadata, self.roots, self.scratch)
        return handle.getvalue()

    def test_sqlite_wal_auth_keys_links_and_unicode_roundtrip(self):
        with sqlite3.connect(self.source / "state.sqlite") as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("PRAGMA wal_autocheckpoint=0")
            database.execute("CREATE TABLE state(value TEXT)")
            database.execute("INSERT INTO state VALUES ('committed')")
            database.commit()
            self.assertTrue((self.source / "state.sqlite-wal").exists())
            (self.source / "users.json").write_text('{"fixture":"private"}')
            (self.source / "key").write_bytes(b"reserved fixture key")
            weird = os.fsdecode(b"nonutf8-\xff")
            (self.source / weird).write_bytes(b"payload")
            os.symlink("/reserved-example/media/movie.mkv", self.source / "movie.mkv")
            raw = self.archive()
        destination = self.root / "stage"
        destination.mkdir()
        proof = inspect_archive(io.BytesIO(raw), self.scratch, extract_root=destination)
        self.assertTrue(proof["verified"])
        restored = destination / "root_000"
        self.assertEqual((restored / weird).read_bytes(), b"payload")
        self.assertEqual(
            os.readlink(restored / "movie.mkv"), "/reserved-example/media/movie.mkv"
        )
        with sqlite3.connect(restored / "state.sqlite") as database:
            self.assertEqual(
                database.execute("SELECT value FROM state").fetchall(), [("committed",)]
            )
            self.assertEqual(
                database.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )

    def test_hardlinks_are_flattened_and_stream_cache_stays_bounded(self):
        original = self.source / "original"
        original.write_text("fixture")
        os.link(original, self.source / "linked")
        for index in range(128):
            (self.source / f"entry-{index:04d}").write_bytes(b"x")
        original_add = tarfile.TarFile.addfile
        observed = []

        def add(archive, *args, **kwargs):
            original_add(archive, *args, **kwargs)
            observed.append(len(archive.members))

        with patch.object(tarfile.TarFile, "addfile", add):
            raw = self.archive()
        self.assertLessEqual(max(observed), 1)
        destination = self.root / "stage"
        destination.mkdir()
        inspect_archive(io.BytesIO(raw), self.scratch, extract_root=destination)
        first, second = (
            destination / "root_000/original",
            destination / "root_000/linked",
        )
        self.assertNotEqual(first.stat().st_ino, second.stat().st_ino)

    def test_archive_corruption_and_missing_footer_are_refused(self):
        (self.source / "payload").write_bytes(b"unique payload bytes")
        raw = self.archive()
        corrupted = raw.replace(b"unique payload bytes", b"unique payload FAILS", 1)
        with self.assertRaises(InstallationArchiveError):
            inspect_archive(io.BytesIO(corrupted), self.scratch)
        with self.assertRaises(InstallationArchiveError):
            inspect_archive(io.BytesIO(raw[:1024]), self.scratch)

    def malformed(self, name, kind=tarfile.REGTYPE, pax=None):
        output = io.BytesIO()
        with tarfile.open(
            fileobj=output, mode="w", format=tarfile.PAX_FORMAT
        ) as archive:
            payload = json.dumps(self.metadata).encode()
            header = _header(
                {
                    "name": "installation.json",
                    "mode": stat.S_IFREG | 0o600,
                    "uid": os.geteuid(),
                    "gid": os.getegid(),
                    "mtime_ns": 0,
                    "size": len(payload),
                }
            )
            archive.addfile(header, io.BytesIO(payload))
            info = tarfile.TarInfo(name)
            info.type, info.mode, info.size = kind, 0o600, 0
            info.pax_headers = {"DUMB.mtime_ns": "0", **(pax or {})}
            info.linkname = "/reserved-example/elsewhere"
            archive.addfile(info, io.BytesIO())
        return output.getvalue()

    def test_traversal_hardlink_sparse_and_special_members_are_refused(self):
        cases = [
            ("../escape", tarfile.REGTYPE, None),
            ("roots/root_000/hard", tarfile.LNKTYPE, None),
            ("roots/root_000/fifo", tarfile.FIFOTYPE, None),
        ]
        for pax in [
            {"GNU.sparse.size": "0"},
            {"GNU.sparse.map": "0,0"},
            {"GNU.sparse.major": "1", "GNU.sparse.minor": "0"},
        ]:
            cases.append(("roots/root_000/sparse", tarfile.REGTYPE, pax))
        for name, kind, pax in cases:
            with (
                self.subTest(name=name, pax=pax),
                self.assertRaises(InstallationArchiveError),
            ):
                inspect_archive(
                    io.BytesIO(self.malformed(name, kind, pax)), self.scratch
                )
        self.assertFalse((self.root / "escape").exists())

    def test_source_mutation_blocks_publication(self):
        (self.source / "a").write_text("original")
        added = False

        def mutate():
            nonlocal added
            if not added:
                (self.source / "new").write_text("added after inventory")
                added = True

        with self.assertRaises(InstallationArchiveError):
            write_archive(
                io.BytesIO(),
                self.metadata,
                self.roots,
                self.scratch,
                owner_check=mutate,
            )

    def test_xattrs_and_privileged_modes_are_not_silently_discarded(self):
        (self.source / "file").write_text("fixture")
        with (
            patch("os.listxattr", return_value=["user.fixture"]),
            self.assertRaises(InstallationArchiveError),
        ):
            inventory(self.roots, self.scratch)
        (self.source / "file").chmod(0o4755)
        with self.assertRaises(InstallationArchiveError):
            inventory(self.roots, self.scratch)


class TransactionTests(unittest.TestCase):
    def transaction(self, directory=True):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        target, staged = root / "target", root / "stage"
        if directory:
            target.mkdir()
            staged.mkdir()
            for name in ("same", "second"):
                (target / name).write_text("original-" + name)
                (staged / name).write_text("restored-" + name)
            kind = "directory"
        else:
            target.write_text("original")
            staged.write_text("restored")
            kind = "file"
        rows = []

        def persist(row):
            # Model a durable JSON write, not shared mutable in-memory aliases.
            if rows:
                rows[0] = json.loads(json.dumps(row))
            else:
                rows.append(json.loads(json.dumps(row)))

        row = prepare(
            {"id": "root_000", "path": str(target), "kind": kind},
            staged,
            "a" * 32,
            [],
            persist,
        )
        return target, row, rows, persist

    def test_crash_before_and_after_every_directory_rename_rolls_back_idempotently(
        self,
    ):
        for point in ("before_old", "after_old", "before_new", "after_new"):
            for chosen in ("same", "second"):
                with self.subTest(point=point, name=chosen):
                    target, row, rows, persist = self.transaction()

                    def crash(phase, name):
                        if (phase, name) == (point, chosen):
                            raise RuntimeError("fixture crash")

                    with self.assertRaises(RuntimeError):
                        apply(row, persist, crash)
                    recovered = json.loads(json.dumps(rows[0]))
                    rollback(recovered, persist)
                    rollback(json.loads(json.dumps(rows[0])), persist)
                    self.assertEqual((target / "same").read_text(), "original-same")
                    self.assertEqual((target / "second").read_text(), "original-second")

    def test_file_crash_boundaries_keep_originals(self):
        for point in ("before_old", "after_old", "before_new", "after_new"):
            with self.subTest(point=point):
                target, row, rows, persist = self.transaction(False)

                def crash(phase, _name):
                    if phase == point:
                        raise RuntimeError("fixture crash")

                with self.assertRaises(RuntimeError):
                    apply(row, persist, crash)
                rollback(rows[0], persist)
                rollback(rows[0], persist)
                self.assertEqual(target.read_text(), "original")

    def test_foreign_new_entry_holder_replacement_and_mounts_block_before_mutation(
        self,
    ):
        target, row, _rows, persist = self.transaction()
        (target / "foreign").write_text("keep")
        with self.assertRaises(InstallationArchiveError):
            apply(row, persist)
        self.assertEqual((target / "same").read_text(), "original-same")
        (target / "foreign").unlink()
        with (
            patch(
                "utils.installation_backup_restore.mount_records",
                return_value=[{"path": str(target / "same")}],
            ),
            self.assertRaises(InstallationArchiveError),
        ):
            apply(row, persist)
        holder = Path(row["holder"])
        moved = holder.with_name(holder.name + "-retained")
        holder.rename(moved)
        holder.mkdir()
        with self.assertRaises(InstallationArchiveError):
            validate(row)

    def test_cross_filesystem_copy_has_independent_verified_inodes(self):
        target, row, _rows, persist = self.transaction()
        staged = Path(row["holder"]) / "staged"
        self.assertNotEqual(
            (target / "same").stat().st_ino, (staged / "same").stat().st_ino
        )
        apply(row, persist)
        self.assertEqual((target / "same").read_text(), "restored-same")
        rollback(row, persist)
        self.assertEqual((target / "same").read_text(), "original-same")


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        config_root, data = self.root / "config", self.root / "data"
        config_root.mkdir()
        data.mkdir()
        self.application = data / "fixture-app"
        self.application.mkdir()
        self.payload = self.application / "application.sqlite"
        with sqlite3.connect(self.payload) as database:
            database.execute("CREATE TABLE records(value TEXT)")
            database.execute("INSERT INTO records VALUES('before')")
        (config_root / "users.json").write_text(
            '{"enabled":false,"users":[],"jwt_secret":"reserved-fixture-key"}'
        )
        (config_root / "users.json").chmod(0o600)
        archive_root = self.root / "archives"
        archive_root.mkdir()
        self.config = Config(
            config_root / "dumb_config.json",
            {
                "puid": os.geteuid(),
                "pgid": os.getegid(),
                "data_root": str(data),
                "fixture": {
                    "enabled": True,
                    "process_name": "App",
                    "config_dir": str(self.application),
                },
                "backup_storage": {"profiles": {}, "default_destination_id": ""},
            },
        )
        self.storage = BackupStorage(self.config)
        self.storage.save_profile(
            {
                "id": "fixture",
                "name": "Fixture",
                "path": str(archive_root),
                "enabled": True,
                "make_default": True,
            }
        )
        self.handler = Handler()
        self.manager = InstallationBackupManager(
            self.config, self.handler, storage=self.storage
        )
        self.manager.workspace_root = self.root / "private-work"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Full discovery deliberately leaves a legacy minimal dependency module.
        # Supply this fixture's explicit runtime registry while preserving every
        # existing module/getter and restore its original interface at cleanup.
        dependencies = sys.modules.get("utils.dependencies")
        if dependencies is None:
            dependencies = import_module("utils.dependencies")
        self.runtime_instances = {}
        self.stack.enter_context(
            patch.object(
                dependencies, "_shared_instances", self.runtime_instances, create=True
            )
        )
        self.stack.enter_context(
            patch(
                "utils.installation_backup.compatibility",
                return_value=compatibility(self.config.config),
            )
        )
        self.stack.enter_context(
            patch.dict(
                os.environ, {"DUMB_AUTH_CONFIG_PATH": str(config_root / "users.json")}
            )
        )
        self.stack.enter_context(
            patch("utils.installation_backup._MANAGER", self.manager)
        )
        for name in (
            "infinidysk_namespace_migration_active",
            "infinidysk_v2_jobs_active",
            "infinidysk_postgres_migration_active",
            "infinidysk_namespace_recovery_pending",
            "infinidysk_postgres_recovery_pending",
            "infinidysk_v2_recovery_pending",
        ):
            self.stack.enter_context(
                patch(
                    "utils.infinidysk_migration_admission." + name, return_value=False
                )
            )
        self.addCleanup(self.join_worker)

    def join_worker(self):
        if self.manager._worker:
            self.manager._worker.join(timeout=20)
            self.assertFalse(self.manager._worker.is_alive())

    def start_backup(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        self.assertTrue(preview["ready"], preview["blockers"])
        result = self.manager.start(
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "secrets": True,
                    "external_state": True,
                },
            }
        )
        self.join_worker()
        return self.manager.get_job(result["job"]["job_id"])["job"]

    def test_real_backup_restore_roundtrip_and_exact_rollback(self):
        job = self.start_backup()
        self.assertEqual(job["status"], "completed", job)
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        with sqlite3.connect(self.payload) as database:
            database.execute("UPDATE records SET value='after'")
        preview = self.manager.restore_preview(
            {"reference": job["result"]["archive"]["reference"]}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["backup"]["verified"])
        result = self.manager.restore_start(
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "replace_state": True,
                    "external_state": True,
                },
            }
        )
        self.join_worker()
        restored = self.manager.get_job(result["job"]["job_id"])["job"]
        self.assertEqual(restored["status"], "completed", restored)
        self.assertTrue(restored["restart_required"])
        self.assertTrue(self.manager.has_blocking_job())
        with sqlite3.connect(self.payload) as database:
            self.assertEqual(
                database.execute("SELECT value FROM records").fetchone()[0], "before"
            )
        self.manager.rollback(
            restored["job_id"], {"confirmation": restored["rollback_confirmation_text"]}
        )
        with sqlite3.connect(self.payload) as database:
            self.assertEqual(
                database.execute("SELECT value FROM records").fetchone()[0], "after"
            )

    def test_failed_archive_resumes_prior_running_set_and_no_partial_catalog(self):
        with patch.object(
            self.storage,
            "write_file",
            side_effect=BackupStorageError("Fixture destination unavailable"),
        ):
            job = self.start_backup()
        self.assertEqual(job["status"], "failed")
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.manager.catalog("fixture")["artifacts"], [])

    def test_unknown_data_and_space_shortfall_are_blockers_not_partial_backups(self):
        unknown = Path(self.config.config["data_root"]) / "unclassified-media"
        unknown.mkdir()
        (unknown / "payload").write_text("never sweep media")
        preview = self.manager.preview({"destination_id": "fixture"})
        self.assertFalse(preview["ready"])
        self.assertTrue(any(str(unknown) in message for message in preview["blockers"]))
        self.assertTrue(any("directory" in message for message in preview["blockers"]))
        self.assertEqual(self.handler.events, [])
        (
            unknown.rmdir()
            if not any(unknown.iterdir())
            else (unknown / "payload").unlink()
        )
        if unknown.exists():
            unknown.rmdir()
        usage = shutil.disk_usage(self.root)
        with patch(
            "utils.installation_backup_inventory.shutil.disk_usage",
            return_value=usage._replace(free=0),
        ):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertFalse(preview["ready"])
        self.assertEqual(preview["capacity"]["status"], "insufficient")
        with self.assertRaises(InstallationBackupError):
            self.manager.start(
                {
                    "preview_token": preview["preview_token"],
                    "confirmation": preview["confirmation_text"],
                    "acknowledgements": {
                        "downtime": True,
                        "secrets": True,
                        "external_state": True,
                    },
                }
            )
        self.assertEqual(self.handler.events, [])

    def test_stale_preview_config_and_destination_identity_refuse_before_stop(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        self.config.config["tz"] = "UTC"
        self.config.save_config()
        with self.assertRaises(InstallationBackupError):
            self.manager.start(
                {
                    "preview_token": preview["preview_token"],
                    "confirmation": preview["confirmation_text"],
                    "acknowledgements": {
                        "downtime": True,
                        "secrets": True,
                        "external_state": True,
                    },
                }
            )
        self.assertEqual(self.handler.events, [])

    def test_fresh_container_mounted_file_import_needs_no_receipt(self):
        job = self.start_backup()
        reference = job["result"]["archive"]["reference"]
        mounted = self.root / "import.tar"
        with self.storage.open_artifact(reference) as source:
            mounted.write_bytes(source.read())
        mounted.chmod(0o600)
        fresh = InstallationBackupManager(
            self.config,
            self.handler,
            state_root=self.root / "fresh-state",
            storage=BackupStorage(self.config, state_root=self.root / "empty-catalog"),
        )
        fresh.workspace_root = self.root / "fresh-work"
        preview = fresh.restore_preview({"archive_path": str(mounted)})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(preview["backup"]["archive_path"], str(mounted))
        self.assertNotIn("reference", preview["backup"])

    def test_shared_mounted_archive_consent_is_explicit_and_bound_to_preview(self):
        job = self.start_backup()
        mounted = self.root / "shared-import.tar"
        with self.storage.open_artifact(
            job["result"]["archive"]["reference"]
        ) as source:
            mounted.write_bytes(source.read())
        mounted.chmod(0o664)
        before = list(self.handler.events)
        denied = self.manager.restore_preview({"archive_path": str(mounted)})
        self.assertFalse(denied["ready"])
        self.assertIn("0664", " ".join(denied["blockers"]))
        self.assertIn("explicitly allow", " ".join(denied["blockers"]))
        preview = self.manager.restore_preview(
            {"archive_path": str(mounted), "allow_shared_permissions": True}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertTrue(preview["backup"]["allow_shared_permissions"])
        self.assertEqual(preview["observed_permissions"]["file_mode"], "0664")
        self.assertIn("ACLs", " ".join(preview["warnings"]))
        record = self.manager._validated_request(
            "restore",
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "replace_state": True,
                    "external_state": True,
                },
            },
        )
        reviewed_source = record["plan"]["source"]
        self.assertIs(reviewed_source["allow_shared_permissions"], True)
        with self.manager._open_source(reviewed_source) as handle:
            self.assertEqual(handle.read(), mounted.read_bytes())
        # Consent never permits an archive to change after the reviewed proof.
        mounted.chmod(0o666)
        with self.assertRaisesRegex(InstallationBackupError, "changed after preview"):
            with self.manager._open_source(reviewed_source):
                self.fail("Changed archive unexpectedly opened")
        self.assertEqual(self.handler.events, before)

    def test_shared_mounted_archive_consent_does_not_allow_linked_sources(self):
        job = self.start_backup()
        mounted = self.root / "shared-import.tar"
        with self.storage.open_artifact(
            job["result"]["archive"]["reference"]
        ) as source:
            mounted.write_bytes(source.read())
        mounted.chmod(0o664)
        alias = self.root / "hardlink.tar"
        os.link(mounted, alias)
        preview = self.manager.restore_preview(
            {"archive_path": str(mounted), "allow_shared_permissions": True}
        )
        self.assertFalse(preview["ready"])
        self.assertIn("hard links", " ".join(preview["blockers"]))
        alias.unlink()
        alias.symlink_to(mounted)
        preview = self.manager.restore_preview(
            {"archive_path": str(alias), "allow_shared_permissions": True}
        )
        self.assertFalse(preview["ready"])

    def test_restore_permission_consent_rejects_coercion_and_reference_overrides(self):
        for value in ("false", "true", 1, None, {}):
            with self.subTest(value=value):
                preview = self.manager.restore_preview(
                    {
                        "archive_path": "/reserved-example/archive.tar",
                        "allow_shared_permissions": value,
                    }
                )
                self.assertFalse(preview["ready"])
                self.assertIn("must be a boolean", " ".join(preview["blockers"]))
        preview = self.manager.restore_preview(
            {
                "reference": "dumb-backup://fixture/installation/" + "a" * 32,
                "allow_shared_permissions": True,
            }
        )
        self.assertFalse(preview["ready"])
        self.assertIn(
            "destination's reviewed permission policy", " ".join(preview["blockers"])
        )
        self.assertEqual(self.handler.events, [])

    def test_shared_mounted_archive_rejects_fifo_replacement_without_blocking(self):
        mounted = self.root / "shared-import.tar"
        mounted.write_bytes(b"source before replacement")
        reviewed = self.manager._restore_source(
            {"archive_path": str(mounted), "allow_shared_permissions": True}
        )
        mounted.unlink()
        os.mkfifo(mounted, 0o600)
        original_open = os.open

        def require_nonblocking_leaf(name, flags, *args, **kwargs):
            if str(name) == mounted.name and not flags & os.O_DIRECTORY:
                self.assertTrue(flags & os.O_NONBLOCK)
            return original_open(name, flags, *args, **kwargs)

        with patch(
            "utils.installation_backup.os.open", side_effect=require_nonblocking_leaf
        ):
            with self.assertRaisesRegex(
                InstallationBackupError, "regular installation archive"
            ):
                with self.manager._open_source(reviewed):
                    self.fail("FIFO unexpectedly accepted")

    def test_registered_shared_archive_restores_with_its_destination_policy(self):
        root = self.root / "archives"
        self.storage.save_profile(
            {
                "id": "fixture",
                "name": "Fixture",
                "path": str(root),
                "enabled": True,
                "allow_shared_permissions": True,
            }
        )
        job = self.start_backup()
        receipt = job["result"]["archive"]
        artifact = (
            Path(self.storage.bound_identity("fixture")["artifact_root"])
            / "installation"
            / (receipt["artifact_id"] + ".tar")
        )
        artifact.chmod(0o664)
        preview = self.manager.restore_preview({"reference": receipt["reference"]})
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertEqual(preview["backup"]["reference"], receipt["reference"])

    def test_interrupted_backup_survives_manager_recreation_and_blocks_mutation(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        record = self.manager._validated_request(
            "backup",
            {
                "preview_token": preview["preview_token"],
                "confirmation": preview["confirmation_text"],
                "acknowledgements": {
                    "downtime": True,
                    "secrets": True,
                    "external_state": True,
                },
            },
        )
        with patch.object(threading.Thread, "start"):
            result = self.manager._admit("backup", record)
        recovered = InstallationBackupManager(
            self.config, self.handler, storage=self.storage
        )
        self.assertTrue(recovered.has_blocking_job())
        status = recovered.status()
        self.assertEqual(status["job"]["status"], "recovery_required")
        self.assertTrue(status["job"]["can_rollback"])
        self.manager._worker = None
        recovered.rollback(
            result["job"]["job_id"],
            {"confirmation": status["job"]["rollback_confirmation_text"]},
        )
        self.assertFalse(recovered.has_blocking_job())

    def test_external_reservation_and_scheduled_worker_block_backup(self):
        from utils.infinidysk_migration_admission import (
            reserve_infinidysk_external_mutation,
            release_infinidysk_external_mutation,
        )

        preview = self.manager.preview({"destination_id": "fixture"})
        token = reserve_infinidysk_external_mutation("fixture managed setup")
        try:
            with self.assertRaises(InstallationBackupError):
                self.manager.start(
                    {
                        "preview_token": preview["preview_token"],
                        "confirmation": preview["confirmation_text"],
                        "acknowledgements": {
                            "downtime": True,
                            "secrets": True,
                            "external_state": True,
                        },
                    }
                )
        finally:
            release_infinidysk_external_mutation(token)
        self.assertEqual(self.handler.events, [])

    def backup_request(self):
        preview = self.manager.preview({"destination_id": "fixture"})
        self.assertTrue(preview["ready"], preview["blockers"])
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "secrets": True,
                "external_state": True,
            },
        }

    def restore_request(self, reference):
        preview = self.manager.restore_preview({"reference": reference})
        self.assertTrue(preview["ready"], preview["blockers"])
        return {
            "preview_token": preview["preview_token"],
            "confirmation": preview["confirmation_text"],
            "acknowledgements": {
                "downtime": True,
                "replace_state": True,
                "external_state": True,
            },
        }

    def test_consumed_backup_token_replays_one_job_after_completion(self):
        request = self.backup_request()
        first = self.manager.start(request)
        self.join_worker()
        second = self.manager.start(request)
        self.assertEqual(first["job"]["job_id"], second["job"]["job_id"])
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])

    def test_concurrent_backup_requests_admit_only_one_live_writer(self):
        request = self.backup_request()
        entered, release = threading.Event(), threading.Event()
        original = self.manager._backup

        def held(identifier):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(identifier)

        results, errors = [], []
        callers = threading.Barrier(2)

        def start():
            try:
                callers.wait(timeout=10)
                results.append(self.manager.start(request))
            except Exception as error:
                errors.append(
                    {
                        "type": type(error).__name__,
                        "filename": getattr(error, "filename", None),
                        "traceback": traceback.format_exc(),
                    }
                )

        with patch.object(self.manager, "_backup", held):
            threads = [threading.Thread(target=start) for _ in range(2)]
            try:
                for thread in threads:
                    thread.start()
                self.assertTrue(entered.wait(10))
            finally:
                release.set()
                for thread in threads:
                    thread.join(timeout=15)
                    self.assertFalse(thread.is_alive())
                self.join_worker()
        self.assertEqual(errors, [])
        self.assertEqual(len({row["job"]["job_id"] for row in results}), 1)
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])

    def test_restore_replay_and_exclusive_rollback_lane(self):
        backup = self.start_backup()
        request = self.restore_request(backup["result"]["archive"]["reference"])
        restored = self.manager.restore_start(request)
        self.join_worker()
        replay = self.manager.restore_start(request)
        self.assertEqual(restored["job"]["job_id"], replay["job"]["job_id"])
        identifier = replay["job"]["job_id"]
        original = self.manager._rollback_roots
        entered, release = threading.Event(), threading.Event()
        failures = []

        def held(*args):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(*args)

        def undo():
            try:
                self.manager.rollback(
                    identifier, {"confirmation": "ROLL BACK " + identifier}
                )
            except Exception as error:
                failures.append(error)

        with patch.object(self.manager, "_rollback_roots", held):
            thread = threading.Thread(target=undo)
            try:
                thread.start()
                self.assertTrue(entered.wait(10))
                with self.assertRaises(InstallationBackupError):
                    self.manager.rollback(
                        identifier, {"confirmation": "ROLL BACK " + identifier}
                    )
                self.assertTrue(self.manager.has_blocking_job())
            finally:
                release.set()
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(
            self.manager.get_job(identifier)["job"]["status"], "rolled_back"
        )

    def test_read_only_restore_failure_keeps_live_services_available(self):
        backup = self.start_backup()
        request = self.restore_request(backup["result"]["archive"]["reference"])
        before = list(self.handler.events)
        with patch.object(
            self.manager,
            "_inspect_source",
            side_effect=InstallationArchiveError("Fixture checksum mismatch"),
        ):
            job = self.manager.restore_start(request)["job"]
            self.join_worker()
        final = self.manager.get_job(job["job_id"])["job"]
        self.assertEqual(final["status"], "failed")
        self.assertFalse(final["can_rollback"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.handler.events, before)
        self.assertIsNone(self.handler.process_names["App"].poll())

    def test_capacity_changed_after_preview_blocks_before_any_service_stop(self):
        request = self.backup_request()
        usage = shutil.disk_usage(self.root)
        with patch(
            "utils.installation_backup_inventory.shutil.disk_usage",
            return_value=usage._replace(free=0),
        ):
            with self.assertRaises(InstallationBackupError):
                self.manager.start(request)
        self.assertEqual(self.handler.events, [])

    def test_scheduled_transient_blocker_is_visible_and_retries_after_five_minutes(
        self,
    ):
        now = [1000.0]
        self.manager.clock = lambda: now[0]
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 1,
            "keep_last": 1,
        }
        self.config.save_config()
        self.manager.tick()
        self.assertEqual(self.manager.status()["schedule_runtime"]["next_due_at"], 4600)
        now[0] = 4600
        with patch.object(
            self.manager,
            "preview",
            return_value={"ready": False, "blockers": ["Fixture unavailable"]},
        ) as preview:
            self.manager.tick()
            runtime = self.manager.status()["schedule_runtime"]
            self.assertEqual(runtime["status"], "blocked")
            self.assertEqual(runtime["blocker"], "Fixture unavailable")
            self.assertEqual(runtime["next_due_at"], 4900)
            now[0] = 4899
            self.manager.tick()
            self.assertEqual(preview.call_count, 1)
        self.assertEqual(self.handler.events, [])

    def test_scheduled_retention_keeps_manual_and_other_producers(self):
        self.config.config["installation_backup"] = {
            "enabled": True,
            "destination_id": "fixture",
            "interval_hours": 24,
            "keep_last": 1,
        }
        self.config.save_config()
        receipts = []
        for offset, scope in enumerate(("manual", "scheduled", "scheduled")):
            # This case proves chronological retention, not random-ID ordering
            # of several receipts created within one wall-clock second.
            with patch("utils.backup_storage.time.time", return_value=1000 + offset):
                receipts.append(
                    self.storage.write_file(
                        "fixture",
                        "installation",
                        lambda handle: handle.write(b"fixture-archive"),
                        metadata={
                            "kind": "installation_backup",
                            "retention_scope": scope,
                        },
                    )
                )
        other = self.storage.write_file(
            "fixture",
            "installation",
            lambda handle: handle.write(b"other-producer"),
            metadata={"kind": "unrelated", "retention_scope": "scheduled"},
        )
        self.manager._retention(receipts[-1])
        remaining = {
            row["reference"]
            for row in self.storage.list_artifacts("fixture", "installation")
        }
        self.assertIn(receipts[0]["reference"], remaining)
        self.assertIn(receipts[-1]["reference"], remaining)
        self.assertIn(other["reference"], remaining)
        self.assertNotIn(receipts[1]["reference"], remaining)

    def test_known_custom_native_paths_and_sqlite_sidecars_are_covered(self):
        external = self.root / "custom"
        external.mkdir()
        sqlite_file = external / "native.sqlite"
        sqlite_file.write_bytes(b"fixture")
        (external / "native.sqlite-wal").write_bytes(b"fixture-wal")
        v2 = self.root / "v2"
        v2.mkdir()
        cli_config = external / "cli-config"
        cli_config.mkdir()
        cli_db = external / "cli-db"
        cli_db.mkdir()
        bazarr = external / "bazarr"
        bazarr.mkdir()
        aio = external / "aio.sqlite"
        aio.write_bytes(b"fixture")
        self.config.config.update(
            {
                "infinidysk": {
                    "process_name": "Native",
                    "runtime_generation": "2",
                    "v2_data_dir": str(v2),
                    "env": {"INFINIDYSK_DATABASE_URL": "sqlite:" + str(sqlite_file)},
                },
                "aiostreams": {
                    "process_name": "AIO",
                    "config_dir": str(external / "aio-runtime"),
                    "database_uri": "sqlite://" + str(aio),
                },
                "cli_debrid": {
                    "process_name": "CLI",
                    "config_dir": str(external / "cli-runtime"),
                    "env": {
                        "USER_CONFIG": str(cli_config),
                        "USER_DB_CONTENT": str(cli_db),
                    },
                },
                "bazarr": {
                    "process_name": "Bazarr",
                    "config_dir": str(external / "bazarr-runtime"),
                    "command": ["python", "bazarr.py", "--config", str(bazarr)],
                },
            }
        )
        self.config.save_config()
        roots, _exclusions = collect_roots(
            self.config.config,
            self.config.file_path,
            self.manager.state_root,
            self.storage,
        )
        selected = {row["path"] for row in roots}
        for path in (
            sqlite_file,
            external / "native.sqlite-wal",
            aio,
            cli_config,
            cli_db,
            bazarr,
        ):
            self.assertIn(str(path), selected)

    def test_nested_controller_auth_is_explicitly_unsupported_before_capture(self):
        nested = Path(self.config.file_path).parent / "nested" / "users.json"
        nested.parent.mkdir()
        nested.write_text('{"enabled":false}')
        with patch.dict(os.environ, {"DUMB_AUTH_CONFIG_PATH": str(nested)}):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("nested controller" in value.lower() for value in preview["blockers"])
        )
        self.assertEqual(self.handler.events, [])

    def test_incomplete_installation_archive_is_blocked_without_quiescence(self):
        backup = self.start_backup()
        reference = backup["result"]["archive"]["reference"]
        with self.storage.open_artifact(reference) as handle:
            metadata = inspect_archive(
                handle, self.manager._workspace("restore-preview")
            )["metadata"]
        roots = [
            row for row in metadata["roots"] if row["path"] == str(self.application)
        ]
        metadata["roots"] = roots
        target = self.root / "incomplete.tar"
        with target.open("wb") as handle:
            write_archive(
                handle, metadata, roots, self.manager._workspace("restore-preview")
            )
        target.chmod(0o600)
        before = list(self.handler.events)
        preview = self.manager.restore_preview({"archive_path": str(target)})
        self.assertFalse(preview["ready"])
        self.assertTrue(
            any("controller" in message.lower() for message in preview["blockers"])
        )
        self.assertEqual(self.handler.events, before)

    def test_staged_controller_tamper_fails_before_any_writer_stops(self):
        backup = self.start_backup()
        request = self.restore_request(backup["result"]["archive"]["reference"])
        original = self.manager._inspect_source

        def changed(source, scratch, **options):
            proof = original(source, scratch, **options)
            stage = options.get("extract_root")
            if stage is not None:
                root = next(
                    row
                    for row in proof["metadata"]["roots"]
                    if row["path"] == str(Path(self.config.file_path).parent)
                )
                (Path(stage) / root["id"] / "users.json").write_text(
                    "invalid controller JSON"
                )
            return proof

        before = list(self.handler.events)
        with patch.object(self.manager, "_inspect_source", changed):
            started = self.manager.restore_start(request)["job"]
            self.join_worker()
        result = self.manager.get_job(started["job_id"])["job"]
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.handler.events, before)

    def test_recovery_fallback_is_bounded_nofollow_and_absence_is_normal(self):
        from utils import installation_backup as module

        root = self.root / "fallback-config"
        root.mkdir(mode=0o700)
        original_open = os.open

        def redirected(path, *args, **kwargs):
            return original_open(root if path == "/config" else path, *args, **kwargs)

        with (
            patch.object(module, "_MANAGER", None),
            patch.object(module, "_BOOT_HOLD", None),
            patch.object(module.os, "open", redirected),
        ):
            self.assertIsNone(module.installation_backup_blocker())
            state = root / "installation-backups"
            state.mkdir(mode=0o700)
            self.assertIsNone(module.installation_backup_blocker())
            active = state / "active.json"
            with active.open("wb") as handle:
                handle.truncate(module.MAX_STATE + 1)
            active.chmod(0o600)
            self.assertIsNotNone(module.installation_backup_blocker())
            active.unlink()
            os.symlink(self.config.file_path, active)
            self.assertIsNotNone(module.installation_backup_blocker())
            active.unlink()
            self.assertIsNone(module.installation_backup_blocker())

    def test_backup_preview_reader_lane_blocks_and_releases_after_failed_census(self):
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        try:
            with self.assertRaisesRegex(InstallationBackupError, "already inspecting"):
                self.manager.preview({"destination_id": "fixture"})
        finally:
            self.manager._preview_reader.release()
        with patch(
            "utils.installation_backup.capacity",
            side_effect=InstallationArchiveError("fixture ledger quota"),
        ):
            preview = self.manager.preview({"destination_id": "fixture"})
        self.assertFalse(preview["ready"])
        self.assertEqual(self.handler.events, [])
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        self.manager._preview_reader.release()

    def test_admission_reader_lane_waits_before_allocating_another_ledger(self):
        request = self.backup_request()
        entered = threading.Event()
        results, errors = [], []
        original = self.manager._preview_scan

        @contextmanager
        def observed_lane(**kwargs):
            entered.set()
            with original(**kwargs):
                yield

        def start():
            try:
                results.append(self.manager.start(request))
            except BaseException as error:
                errors.append(error)

        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        with patch.object(self.manager, "_preview_scan", observed_lane):
            thread = threading.Thread(target=start)
            try:
                thread.start()
                self.assertTrue(entered.wait(5))
                self.assertTrue(thread.is_alive())
                self.assertIn(request["preview_token"], self.manager._previews)
                self.assertEqual(self.handler.events, [])
            finally:
                self.manager._preview_reader.release()
                thread.join(timeout=20)
                self.assertFalse(thread.is_alive())
                self.join_worker()
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 1)

    def test_different_token_is_blocked_before_census_for_an_active_writer(self):
        first, second = self.backup_request(), self.backup_request()
        entered, release = threading.Event(), threading.Event()
        original = self.manager._backup

        def held(identifier):
            entered.set()
            self.assertTrue(release.wait(10))
            return original(identifier)

        with patch.object(self.manager, "_backup", held):
            try:
                self.manager.start(first)
                self.assertTrue(entered.wait(5))
                with patch(
                    "utils.installation_backup.capacity",
                    side_effect=AssertionError(
                        "No second ledger during an active writer"
                    ),
                ):
                    with self.assertRaisesRegex(
                        InstallationBackupError, "owns managed state"
                    ):
                        self.manager.start(second)
            finally:
                release.set()
                self.join_worker()
        self.assertIn(second["preview_token"], self.manager._previews)
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        self.manager._preview_reader.release()

    def test_restore_preview_reader_lane_blocks_duplicate_kernel_io(self):
        self.assertTrue(self.manager._preview_reader.acquire(blocking=False))
        try:
            with self.assertRaises(InstallationBackupError):
                self.manager.restore_preview(
                    {"archive_path": str(self.root / "fixture.tar")}
                )
        finally:
            self.manager._preview_reader.release()

    def test_source_and_destination_capacity_group_real_filesystems_and_margin(self):
        roots, exclusions = collect_roots(
            self.config.config,
            self.config.file_path,
            self.manager.state_root,
            self.storage,
        )
        report, _details = capacity(
            roots,
            self.manager._workspace("preview"),
            self.root / "archives",
            exclusions,
        )
        self.assertEqual(len(report["filesystems"]), 1)
        row = report["filesystems"][0]
        self.assertEqual(
            row["required_bytes"],
            row["base_required_bytes"] + row["safety_margin_bytes"],
        )
        self.assertIn("local_verification", row["roles"])
        self.assertIn("cold_archive", row["roles"])
        if (
            Path("/dev/shm").is_dir()
            and Path("/dev/shm").stat().st_dev != self.root.stat().st_dev
        ):
            separate, _details = capacity(
                roots, self.manager._workspace("preview"), Path("/dev/shm"), exclusions
            )
            self.assertEqual(len(separate["filesystems"]), 2)

    def test_running_optimizer_registry_blocks_before_any_service_stop(self):
        self.runtime_instances["rclone_optimizer_manager"] = SimpleNamespace(
            _threads={"fixture": SimpleNamespace(is_alive=lambda: True)}
        )
        request = self.backup_request()
        with self.assertRaisesRegex(InstallationBackupError, "optimizer"):
            self.manager.start(request)
        self.assertEqual(self.handler.events, [])

    def test_active_media_registry_blocks_before_any_service_stop(self):
        self.runtime_instances["media_protection_manager"] = SimpleNamespace(
            lock=threading.RLock(),
            incidents={"fixture": {"status": "waiting_for_recovery"}},
        )
        request = self.backup_request()
        with self.assertRaisesRegex(InstallationBackupError, "media protection"):
            self.manager.start(request)
        self.assertEqual(self.handler.events, [])

    def test_repeated_barrier_synchronized_first_use_backup_requests(self):
        for round_number in range(8):
            with self.subTest(round=round_number):
                fixture = ManagerTests(
                    "test_concurrent_backup_requests_admit_only_one_live_writer"
                )
                fixture.setUp()
                try:
                    fixture.test_concurrent_backup_requests_admit_only_one_live_writer()
                finally:
                    self.assertTrue(fixture.doCleanups())

    def test_independent_control_managers_validate_raced_directory_creation(self):
        # Independent instance locks cannot conceal a shared-directory mkdir race.
        self.manager._state("seed.json")
        second = InstallationBackupManager(
            self.config, self.handler, storage=self.storage
        )
        overlap = threading.Barrier(2)
        original = os.mkdir
        failures = []

        def mkdir(path, *args, **kwargs):
            if Path(path).name == "raced-state":
                overlap.wait(timeout=10)
            return original(path, *args, **kwargs)

        def create(manager):
            try:
                path = manager._state("raced-state", "fixture.json")
                self.assertEqual(path.parent.stat().st_uid, os.geteuid())
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            except BaseException:
                failures.append(traceback.format_exc())

        with patch("os.mkdir", mkdir):
            threads = [
                threading.Thread(target=create, args=(manager,))
                for manager in (self.manager, second)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])

    def test_existing_unsafe_state_components_are_not_accepted_after_create_race(self):
        from utils.backup_storage import _child_directory, _directory

        self.manager._state("seed.json")
        state = self.manager.state_root
        target = self.root / "foreign-target"
        target.mkdir()
        (state / "redirect").symlink_to(target, target_is_directory=True)
        (state / "regular").write_text("retain")
        (state / "public").mkdir(mode=0o755)
        for name in ("redirect", "regular", "public"):
            with self.subTest(name=name):
                with self.assertRaises((OSError, InstallationBackupError)):
                    self.manager._state(name, "must-not-exist.json")
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual((state / "regular").read_text(), "retain")
        with _directory(state) as parent:
            for name in ("redirect", "regular", "public"):
                with self.subTest(storage=name):
                    with self.assertRaises((OSError, BackupStorageError)):
                        with _child_directory(parent, name, create=True):
                            self.fail("Unsafe receipt component was accepted.")


class ReceiptDirectoryRaceTests(unittest.TestCase):
    def test_profile_and_receipt_directories_validate_concurrent_creation(self):
        from utils.backup_storage import _child_directory, _directory

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = os.mkdir
            for kind in ("raced-profile", "raced-receipt"):
                overlap = threading.Barrier(2)
                failures = []

                def mkdir(path, *args, **kwargs):
                    if Path(path).name == kind:
                        overlap.wait(timeout=10)
                    return original(path, *args, **kwargs)

                def create(parent):
                    try:
                        context = (
                            _directory(root / kind, create=True, private=True)
                            if kind == "raced-profile"
                            else _child_directory(parent, kind, create=True)
                        )
                        with context as descriptor:
                            self.assertTrue(stat.S_ISDIR(os.fstat(descriptor).st_mode))
                            self.assertEqual(
                                stat.S_IMODE(os.fstat(descriptor).st_mode), 0o700
                            )
                    except BaseException:
                        failures.append(traceback.format_exc())

                with _directory(root) as parent, patch("os.mkdir", mkdir):
                    threads = [
                        threading.Thread(target=create, args=(parent,))
                        for _ in range(2)
                    ]
                    for thread in threads:
                        thread.start()
                    for thread in threads:
                        thread.join(timeout=15)
                        self.assertFalse(thread.is_alive())
                self.assertEqual(failures, [])
