"""Real selected-service archive/transaction flows on disposable SQLite state."""

from copy import copy, deepcopy
import io
import json
from pathlib import Path
import shutil
import sqlite3
import tarfile
import unittest
from unittest.mock import patch

from tests import test_installation_backup as fixtures
from utils.installation_backup import InstallationBackupError
from utils.installation_backup_archive import InstallationArchiveError, inspect_archive
from utils.service_backup import SERVICE_SCHEMA, ServiceBackupOperations, scope_options


class ServiceBackupTests(unittest.TestCase):
    join_worker = fixtures.ManagerTests.join_worker

    def setUp(self):
        fixtures.ManagerTests.setUp(self)
        self.other = self.root / "data" / "other-app"
        self.other.mkdir()
        self.other_payload = self.other / "retain.sqlite"
        with sqlite3.connect(self.other_payload) as database:
            database.execute("CREATE TABLE retained(value TEXT)")
            database.execute("INSERT INTO retained VALUES('unselected-original')")
        self.postgres = self.root / "data" / "postgres"
        self.postgres.mkdir()
        self.pg_payload = self.postgres / "cluster-control"
        self.pg_payload.write_bytes(b"fixture shared PostgreSQL bytes")
        self.config.config.update(
            tz="UTC",
            dumb={"onboarding_completed": True, "ui": {"fixture": "current-global"}},
            other={
                "enabled": True,
                "process_name": "Other App",
                "config_dir": str(self.other),
                "env": {"OTHER_KEY": "unselected-original"},
            },
            postgres={
                "enabled": True,
                "process_name": "PostgreSQL",
                "config_dir": str(self.postgres),
                "host": "127.0.0.1",
                "port": 5432,
            },
        )
        self.config.config["fixture"]["env"] = {
            "API_KEY": "reserved-selected-archive-secret"
        }
        self.config.save_config()
        self.handler.process_names["Other App"] = fixtures.Process()
        self.handler.process_names["PostgreSQL"] = fixtures.Process()
        self.other_process = self.handler.process_names["Other App"]
        self.postgres_process = self.handler.process_names["PostgreSQL"]
        self.auth_path = Path(self.config.file_path).parent / "users.json"
        self.env_path = self.auth_path.parent / ".env"
        self.env_path.write_text("GLOBAL_FIXTURE=keep-current\n")
        self.global_original = self.auth_path.read_bytes()
        from utils import installation_backup as coordinator

        self.stack.enter_context(
            patch(
                "utils.service_backup.compatibility",
                return_value=coordinator.compatibility(self.config.config),
            )
        )

    def query(self, payload=None):
        with sqlite3.connect(payload or self.payload) as database:
            return database.execute("SELECT value FROM records").fetchone()[0]

    def change_selected(self):
        with sqlite3.connect(self.payload) as database:
            database.execute("UPDATE records SET value='live-after-backup'")
        self.config.config["fixture"]["env"][
            "API_KEY"
        ] = "reserved-selected-current-secret"
        self.config.config["dumb"]["ui"]["fixture"] = "latest-global"
        self.config.config["tz"] = "Etc/UTC"
        self.config.config["other"]["env"]["OTHER_KEY"] = "latest-unselected"
        self.config.save_config()
        self.auth_path.write_text(
            '{"enabled":true,"users":[],"jwt_secret":"reserved-current-auth"}'
        )
        self.auth_path.chmod(0o600)
        return deepcopy(self.config.config), self.auth_path.read_bytes()

    def backup(self, service_ids=None):
        preview = self.manager.preview(
            {
                "destination_id": "fixture",
                "scope": "services",
                "service_ids": service_ids or ["fixture"],
            }
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertNotIn("reserved-selected-archive-secret", json.dumps(preview))
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
        job = self.manager.get_job(result["job"]["job_id"])["job"]
        self.assertEqual(job["status"], "completed", job)
        self.assertNotIn("reserved-selected-archive-secret", json.dumps(job))
        return job

    def restore_preview(self, job):
        preview = self.manager.restore_preview(
            {"reference": job["result"]["archive"]["reference"]}
        )
        self.assertTrue(preview["ready"], preview["blockers"])
        self.assertNotIn("reserved-selected-archive-secret", json.dumps(preview))
        return preview

    def start_restore(self, preview):
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
        return self.manager.get_job(result["job"]["job_id"])["job"]

    def assert_unselected(self, current, auth):
        self.assertEqual(self.auth_path.read_bytes(), auth)
        self.assertEqual(self.env_path.read_text(), "GLOBAL_FIXTURE=keep-current\n")
        for key in (
            "dumb",
            "tz",
            "puid",
            "pgid",
            "data_root",
            "backup_storage",
            "postgres",
            "other",
        ):
            self.assertEqual(self.config.config[key], current[key], key)
        with sqlite3.connect(self.other_payload) as database:
            self.assertEqual(
                database.execute("SELECT value FROM retained").fetchone()[0],
                "unselected-original",
            )
        self.assertEqual(
            self.pg_payload.read_bytes(), b"fixture shared PostgreSQL bytes"
        )
        self.assertIs(self.handler.process_names["Other App"], self.other_process)
        self.assertIs(self.handler.process_names["PostgreSQL"], self.postgres_process)
        self.assertIsNone(self.other_process.poll())
        self.assertIsNone(self.postgres_process.poll())
        self.assertNotIn(("stop", "Other App"), self.handler.events)
        self.assertNotIn(("stop", "PostgreSQL"), self.handler.events)

    def rewrite_metadata(self, job, change, name="hostile.tar"):
        mounted = self.root / name
        with self.storage.open_artifact(
            job["result"]["archive"]["reference"]
        ) as incoming:
            original = incoming.read()
        with (
            tarfile.open(fileobj=io.BytesIO(original), mode="r:") as source,
            tarfile.open(mounted, "w", format=tarfile.PAX_FORMAT) as destination,
        ):
            for entry in source:
                entry = copy(entry)
                stream = source.extractfile(entry) if entry.isfile() else None
                if entry.name == "installation.json":
                    metadata = json.loads(stream.read())
                    change(metadata)
                    raw = json.dumps(metadata).encode()
                    entry.size = len(raw)
                    stream = io.BytesIO(raw)
                destination.addfile(entry, stream)
                if stream is not None:
                    stream.close()
        mounted.chmod(0o600)
        return mounted

    def test_sqlite_and_selected_settings_restore_without_auth_global_pg_or_other_app(
        self,
    ):
        job = self.backup()
        self.assertEqual(self.handler.events, [("stop", "App"), ("start", "App")])
        current, auth = self.change_selected()
        preview = self.restore_preview(job)
        self.assertEqual(preview["service_scope"]["service_ids"], ["fixture"])
        self.assertEqual(preview["service_scope"]["affected_process_names"], ["App"])
        restored = self.start_restore(preview)
        self.assertEqual(restored["status"], "completed", restored)
        self.assertFalse(restored["restart_required"])
        self.assertFalse(self.manager.has_blocking_job())
        self.assertEqual(self.query(), "before")
        self.assertEqual(
            self.config.config["fixture"]["env"]["API_KEY"],
            "reserved-selected-archive-secret",
        )
        self.assertEqual(
            self.handler.events,
            [("stop", "App"), ("start", "App"), ("stop", "App"), ("start", "App")],
        )
        self.assert_unselected(current, auth)
        self.assertNotIn("reserved-selected-archive-secret", json.dumps(restored))
        self.assertNotIn("reserved-current-auth", json.dumps(restored))

    def test_schema_two_contains_only_selected_private_configuration(self):
        job = self.backup()
        scratch = self.root / "inspect-private"
        scratch.mkdir(mode=0o700)
        with self.storage.open_artifact(
            job["result"]["archive"]["reference"]
        ) as incoming:
            proof = inspect_archive(incoming, scratch)
        self.assertEqual(proof["metadata"]["schema_version"], SERVICE_SCHEMA)
        self.assertEqual(set(proof["service_configuration"]), {"fixture"})
        self.assertEqual(
            proof["service_configuration"]["fixture"]["env"]["API_KEY"],
            "reserved-selected-archive-secret",
        )
        self.assertNotIn("auth_config_path", proof["metadata"])
        self.assertNotIn("auth", proof["controller_sha256"])
        self.assertNotIn(
            str(self.other), [row["path"] for row in proof["metadata"]["roots"]]
        )
        self.assertNotIn(
            str(self.postgres), [row["path"] for row in proof["metadata"]["roots"]]
        )
        self.assertNotIn(
            "reserved-selected-archive-secret",
            json.dumps(self.manager.catalog("fixture")),
        )

    def test_lost_application_directory_and_sqlite_database_are_restored(self):
        job = self.backup()
        current, auth = self.change_selected()
        shutil.rmtree(self.application)
        self.assertFalse(self.application.exists())
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(self.query(), "before")
        self.assert_unselected(current, auth)

    def test_missing_sqlite_file_inside_the_selected_root_is_restored(self):
        job = self.backup()
        current, auth = self.change_selected()
        self.payload.unlink()
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(self.query(), "before")
        self.assert_unselected(current, auth)

    def test_lost_explicit_native_settings_file_is_restored_without_other_apps_pausing(
        self,
    ):
        filename = self.root / "single-native.json"
        filename.write_text('{"fixture":"archived-native-value"}')
        self.config.config["single"] = {
            "process_name": "Stopped single app",
            "enabled": False,
            "settings_file": str(filename),
        }
        self.config.save_config()
        job = self.backup(["single"])
        before = list(self.handler.events)
        filename.unlink()
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(filename.read_text(), '{"fixture":"archived-native-value"}')
        self.assertEqual(self.handler.events, before)

    def test_current_disabled_choice_is_preserved_and_a_stopped_app_is_not_started(
        self,
    ):
        job = self.backup()
        self.change_selected()
        self.config.config["fixture"]["enabled"] = False
        self.config.save_config()
        self.handler.process_names["App"].stopped = True
        before = list(self.handler.events)
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertFalse(self.config.config["fixture"]["enabled"])
        self.assertEqual(self.handler.events, before)
        self.assertIsNotNone(self.handler.process_names["App"].poll())
        self.assertEqual(self.query(), "before")

    def test_config_only_disabled_service_has_no_application_root_requirement(self):
        self.config.config["empty"] = {
            "process_name": "Not installed",
            "enabled": False,
            "env": {"KEY": "reserved-config-only-archived"},
        }
        self.config.save_config()
        job = self.backup(["empty"])
        self.assertEqual(self.handler.events, [])
        self.config.config["empty"]["env"]["KEY"] = "reserved-config-only-current"
        self.config.save_config()
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(
            self.config.config["empty"]["env"]["KEY"], "reserved-config-only-archived"
        )
        self.assertEqual(self.handler.events, [])

    def test_shared_owner_bundle_is_required_and_copied_once(self):
        self.config.config["other"]["config_dir"] = str(self.application)
        self.config.save_config()
        choices = self.manager.service_choices()["services"]
        choice = next(row for row in choices if row["id"] == "fixture")
        self.assertTrue(choice["available"])
        self.assertEqual(choice["required_service_ids"], ["other"])
        blocked = self.manager.preview(
            {
                "destination_id": "fixture",
                "scope": "services",
                "service_ids": ["fixture"],
            }
        )
        self.assertFalse(blocked["ready"])
        self.assertEqual(self.handler.events, [])
        job = self.backup(["fixture", "other"])
        self.assertEqual(
            set(job["service_scope"]["affected_process_names"]), {"App", "Other App"}
        )
        self.assertNotIn(("stop", "PostgreSQL"), self.handler.events)

    def test_exact_instance_restore_preserves_sibling_instance_configuration_and_files(
        self,
    ):
        first = self.config.config.pop("fixture")
        second = self.config.config.pop("other")
        self.config.config["fixture"] = {
            "instances": {"First": first, "Second": second}
        }
        self.config.save_config()
        job = self.backup(["fixture:First"])
        sibling_before = deepcopy(second)
        self.config.config["fixture"]["instances"]["First"]["env"][
            "API_KEY"
        ] = "reserved-current-instance"
        self.config.config["fixture"]["instances"]["Second"]["env"][
            "OTHER_KEY"
        ] = "latest-sibling"
        self.config.save_config()
        sibling_before["env"]["OTHER_KEY"] = "latest-sibling"
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(
            self.config.config["fixture"]["instances"]["Second"], sibling_before
        )
        self.assertEqual(
            self.config.config["fixture"]["instances"]["First"]["env"]["API_KEY"],
            "reserved-selected-archive-secret",
        )
        self.assertNotIn(("stop", "Other App"), self.handler.events)

    def test_schema_two_cannot_fall_through_as_whole_installation(self):
        job = self.backup()
        for version in (1, 99):
            path = self.rewrite_metadata(
                job,
                lambda metadata, version=version: metadata.update(
                    schema_version=version
                ),
                f"schema-{version}.tar",
            )
            before = list(self.handler.events)
            preview = self.manager.restore_preview({"archive_path": str(path)})
            self.assertFalse(preview["ready"])
            self.assertEqual(self.handler.events, before)
            self.assertEqual(self.query(), "before")

    def test_hostile_unselected_root_is_rejected_before_any_service_stops(self):
        job = self.backup()

        def change(metadata):
            application = next(
                row for row in metadata["roots"] if not row.get("service_internal")
            )
            application["path"] = str(self.other)

        path = self.rewrite_metadata(job, change)
        before = list(self.handler.events)
        preview = self.manager.restore_preview({"archive_path": str(path)})
        self.assertFalse(preview["ready"])
        self.assertEqual(self.handler.events, before)
        self.assertNotIn("reserved-selected-archive-secret", json.dumps(preview))
        self.assertEqual(
            self.pg_payload.read_bytes(), b"fixture shared PostgreSQL bytes"
        )

    def test_blocked_archive_scope_never_exposes_embedded_private_configuration(self):
        job = self.backup()

        def change(metadata):
            metadata["service_scope"]["configuration"] = {
                "API_KEY": "reserved-private-import-metadata"
            }
            metadata["service_scope"]["service_ids"] = ["unknown-service"]

        path = self.rewrite_metadata(job, change)
        preview = self.manager.restore_preview({"archive_path": str(path)})
        self.assertFalse(preview["ready"])
        self.assertNotIn("reserved-private-import-metadata", json.dumps(preview))

    def test_failure_after_file_replacement_rolls_back_only_selected_state(self):
        job = self.backup()
        current, auth = self.change_selected()
        preview = self.restore_preview(job)
        from utils import installation_backup_restore as transaction

        original = transaction.apply

        def fail_after_config(row, persist):
            original(row, persist)
            if row["root"]["roles"] == ["selected_configuration_merge"]:
                raise InstallationArchiveError(
                    "Reserved fixture crash before service commit"
                )

        with patch.object(transaction, "apply", side_effect=fail_after_config):
            interrupted = self.start_restore(preview)
        self.assertEqual(interrupted["status"], "recovery_required", interrupted)
        self.assertFalse(interrupted.get("service_restore_committed", False))
        self.assertIsNotNone(self.handler.process_names["App"].poll())
        self.manager.rollback(
            interrupted["job_id"],
            {"confirmation": interrupted["rollback_confirmation_text"]},
        )
        recovered = self.manager.get_job(interrupted["job_id"])["job"]
        self.assertEqual(recovered["status"], "rolled_back", recovered)
        self.assertFalse(recovered["restart_required"])
        self.assertEqual(self.query(), "live-after-backup")
        self.assertEqual(self.config.config["fixture"], current["fixture"])
        self.assert_unselected(current, auth)
        self.assertFalse(self.manager.has_blocking_job())

    def test_committed_resume_failure_finishes_new_state_and_never_rolls_it_back(self):
        job = self.backup()
        current, auth = self.change_selected()
        preview = self.restore_preview(job)
        with patch.object(
            self.manager,
            "_resume",
            side_effect=InstallationBackupError("Reserved fixture resume failure"),
        ):
            interrupted = self.start_restore(preview)
        self.assertEqual(interrupted["status"], "recovery_required", interrupted)
        self.assertTrue(interrupted["service_restore_committed"])
        self.assertEqual(
            interrupted["rollback_confirmation_text"],
            f"FINISH SERVICE RESTORE {interrupted['job_id']}",
        )
        self.assertEqual(self.query(), "before")
        with self.assertRaises(InstallationBackupError):
            self.manager.rollback(
                interrupted["job_id"],
                {"confirmation": f"ROLL BACK {interrupted['job_id']}"},
            )
        self.manager.rollback(
            interrupted["job_id"],
            {"confirmation": interrupted["rollback_confirmation_text"]},
        )
        recovered = self.manager.get_job(interrupted["job_id"])["job"]
        self.assertEqual(recovered["status"], "completed", recovered)
        self.assertEqual(self.query(), "before")
        self.assertEqual(
            self.config.config["fixture"]["env"]["API_KEY"],
            "reserved-selected-archive-secret",
        )
        self.assert_unselected(current, auth)
        self.assertFalse(self.manager.has_blocking_job())

    def test_new_controller_recovers_a_hard_crash_after_the_durable_commit(self):
        job = self.backup()
        current, auth = self.change_selected()
        with patch.object(
            self.manager,
            "_resume",
            side_effect=InstallationBackupError("Reserved fixture stop at commit"),
        ):
            interrupted = self.start_restore(self.restore_preview(job))
        identifier = interrupted["job_id"]
        # Model power loss after the durable commit save, before an exception
        # handler or terminal result can rewrite the still-running journal.
        self.manager._progress(
            identifier, "committing", "Reserved fixture hard crash", status="running"
        )
        restarted = fixtures.InstallationBackupManager(
            self.config, self.handler, storage=self.storage
        )
        restarted.workspace_root = self.root / "restarted-private-work"
        restarted._boot = ["reserved-new-controller", "reserved-start", "reserved-boot"]
        observed = restarted.status()["job"]
        self.assertEqual(observed["status"], "recovery_required")
        self.assertTrue(observed["service_restore_committed"])
        self.assertEqual(
            observed["rollback_confirmation_text"],
            f"FINISH SERVICE RESTORE {identifier}",
        )
        restarted.rollback(
            identifier, {"confirmation": observed["rollback_confirmation_text"]}
        )
        completed = restarted.get_job(identifier)["job"]
        self.assertEqual(completed["status"], "completed", completed)
        self.assertTrue(completed["restart_required"])
        self.assertTrue(restarted.has_blocking_job())
        self.assertIsNotNone(self.handler.process_names["App"].poll())
        self.assertEqual(self.query(), "before")
        self.assert_unselected(current, auth)
        reloaded = fixtures.InstallationBackupManager(
            self.config, self.handler, storage=self.storage
        )
        reloaded._boot = [
            "reserved-final-controller",
            "reserved-final-start",
            "reserved-final-boot",
        ]
        reloaded.initialize_controller()
        self.assertFalse(reloaded.get_job(identifier)["job"]["restart_required"])
        self.assertFalse(reloaded.has_blocking_job())

    def test_selected_schedule_scope_survives_unrelated_schedule_edits(self):
        self.manager.update_schedule(
            {"enabled": False, "scope": "services", "service_ids": ["fixture"]}
        )
        updated = self.manager.update_schedule(
            {"enabled": False, "interval_hours": 48}
        )["schedule"]
        self.assertEqual(updated["scope"], "services")
        self.assertEqual(updated["service_ids"], ["fixture"])
        reset = self.manager.update_schedule(
            {"enabled": False, "scope": "installation"}
        )["schedule"]
        self.assertEqual(reset["scope"], "installation")
        self.assertEqual(reset["service_ids"], [])

    def test_retention_partitions_service_bundles_whole_archives_and_manual_backups(
        self,
    ):
        self.config.config["installation_backup"] = {"enabled": False, "keep_last": 1}
        self.config.save_config()
        receipts = []

        def create(scope=None, retention="scheduled", kind="installation_backup"):
            metadata = {"kind": kind, "retention_scope": retention}
            if scope is not None:
                metadata["service_scope"] = scope
            with patch(
                "utils.backup_storage.time.time", return_value=1000 + len(receipts)
            ):
                receipt = self.storage.write_file(
                    "fixture",
                    "installation",
                    lambda handle: handle.write(b"reserved-retention-fixture"),
                    metadata=metadata,
                )
            receipts.append(receipt)
            return receipt

        selected = {"kind": "services", "service_ids": ["fixture", "other"]}
        whole = create()
        other = create({"kind": "services", "service_ids": ["other"]})
        manual = create(selected, retention="manual")
        expired = create({"kind": "services", "service_ids": ["other", "fixture"]})
        malformed = create({"kind": "services", "service_ids": []})
        unrelated = create(selected, kind="another-producer")
        latest = create(selected)
        self.manager._retention(latest)
        remaining = {
            row["reference"]
            for row in self.storage.list_artifacts("fixture", "installation")
        }
        self.assertNotIn(expired["reference"], remaining)
        for retained in (whole, other, manual, malformed, unrelated, latest):
            self.assertIn(retained["reference"], remaining)
        latest_whole = create()
        self.manager._retention(latest_whole)
        remaining = {
            row["reference"]
            for row in self.storage.list_artifacts("fixture", "installation")
        }
        self.assertNotIn(whole["reference"], remaining)
        for retained in (other, manual, malformed, unrelated, latest, latest_whole):
            self.assertIn(retained["reference"], remaining)

    def fake_database_adapter(self):
        values = {
            "fixture_db": "archived-first",
            "other_db": "archived-second",
            "unselected_db": "retain-unselected",
        }
        events = []

        class Adapter:
            def __init__(self, _config):
                pass

            def estimate(self, _specs):
                return 4096

            @staticmethod
            def _manifest(value):
                return deepcopy(value)

            def backup(self, specs, folder, progress=None):
                captured = {row["database"]: values[row["database"]] for row in specs}
                (folder / "fixture.dump").write_text(json.dumps(captured))
                (folder / "fixture.dump").chmod(0o600)
                events.append(("backup", specs[0]["database"]))
                return {
                    "databases": [
                        {**row, "bytes": (folder / "fixture.dump").stat().st_size}
                        for row in specs
                    ]
                }

            def prepare_restore(
                self,
                manifest,
                folder,
                _identifier,
                persist,
                *,
                progress=None,
                specs=None,
            ):
                if [
                    {key: row[key] for key in ("database", "role")}
                    for row in manifest["databases"]
                ] != specs:
                    raise InstallationArchiveError("Fixture database manifest mismatch")
                transaction = {
                    "phase": "prepared",
                    "specs": deepcopy(specs),
                    "old": {row["database"]: values[row["database"]] for row in specs},
                    "new": json.loads((folder / "fixture.dump").read_text()),
                }
                persist(transaction)
                events.append(("prepare", specs[0]["database"]))
                return transaction

            def apply_restore(self, transaction, persist):
                values.update(transaction["new"])
                transaction["phase"] = "applied"
                persist(transaction)
                events.append(("apply", transaction["specs"][0]["database"]))

            def finish_restore(self, transaction, persist):
                transaction["phase"] = "finished"
                persist(transaction)
                events.append(("finish", transaction["specs"][0]["database"]))

            def verify_server_binding(self, _transaction):
                pass

            def rollback_restore(self, transaction, persist):
                values.update(transaction["old"])
                transaction["phase"] = "rolled_back"
                persist(transaction)
                events.append(("rollback", transaction["specs"][0]["database"]))

        def selected(_config, key, _instance):
            database = {"fixture": "fixture_db", "other": "other_db"}.get(key)
            return [{"database": database, "role": "main"}] if database else []

        self.stack.enter_context(
            patch("utils.service_backup.service_databases", side_effect=selected)
        )
        self.stack.enter_context(
            patch("utils.service_backup.PostgresServiceBackup", Adapter)
        )
        return Adapter, values, events

    def test_multiple_database_sets_commit_independently_without_shared_cluster_replacement(
        self,
    ):
        _adapter, values, events = self.fake_database_adapter()
        job = self.backup(["fixture", "other"])
        self.assertEqual(job["service_scope"]["database_count"], 2)
        values.update(fixture_db="live-first", other_db="live-second")
        current, auth = self.change_selected()
        restored = self.start_restore(self.restore_preview(job))
        self.assertEqual(restored["status"], "completed", restored)
        self.assertEqual(
            values,
            {
                "fixture_db": "archived-first",
                "other_db": "archived-second",
                "unselected_db": "retain-unselected",
            },
        )
        self.assertEqual(
            events,
            [
                (phase, database)
                for phase in ("backup", "prepare", "apply", "finish")
                for database in ("fixture_db", "other_db")
            ],
        )
        self.assertEqual(self.auth_path.read_bytes(), auth)
        self.assertEqual(self.config.config["dumb"], current["dumb"])
        self.assertIs(self.handler.process_names["PostgreSQL"], self.postgres_process)
        self.assertNotIn(("stop", "PostgreSQL"), self.handler.events)
        self.assertEqual(
            self.pg_payload.read_bytes(), b"fixture shared PostgreSQL bytes"
        )

    def test_database_failure_rolls_back_all_prepared_sets_in_reverse_order(self):
        adapter, values, events = self.fake_database_adapter()
        job = self.backup(["fixture", "other"])
        values.update(fixture_db="live-first", other_db="live-second")
        current, auth = self.change_selected()
        preview = self.restore_preview(job)
        original = adapter.apply_restore

        def fail_second(instance, transaction, persist):
            original(instance, transaction, persist)
            if transaction["specs"][0]["database"] == "other_db":
                raise InstallationArchiveError(
                    "Reserved fixture second database failure"
                )

        with patch.object(adapter, "apply_restore", fail_second):
            interrupted = self.start_restore(preview)
        self.assertEqual(interrupted["status"], "recovery_required", interrupted)
        self.assertFalse(interrupted.get("service_restore_committed", False))
        self.manager.rollback(
            interrupted["job_id"],
            {"confirmation": interrupted["rollback_confirmation_text"]},
        )
        self.assertEqual(
            values,
            {
                "fixture_db": "live-first",
                "other_db": "live-second",
                "unselected_db": "retain-unselected",
            },
        )
        self.assertEqual(
            [row for row in events if row[0] == "rollback"],
            [("rollback", "other_db"), ("rollback", "fixture_db")],
        )
        self.assertEqual(self.query(), "live-after-backup")
        self.assertEqual(self.config.config["fixture"], current["fixture"])
        self.assertEqual(self.config.config["other"], current["other"])
        self.assertEqual(self.auth_path.read_bytes(), auth)
        self.assertIs(self.handler.process_names["PostgreSQL"], self.postgres_process)


class ServiceBackupOptionTests(unittest.TestCase):
    def test_non_http_callers_obey_exact_selection_bounds(self):
        self.assertEqual(
            scope_options({}), {"scope": "installation", "service_ids": []}
        )
        self.assertEqual(
            scope_options({"scope": "services", "service_ids": ["other", "fixture"]}),
            {"scope": "services", "service_ids": ["fixture", "other"]},
        )
        for options in (
            {"scope": "services"},
            {"scope": "services", "service_ids": ["fixture"] * 2},
            {"scope": "services", "service_ids": ["x" * 129]},
            {"scope": "services", "service_ids": [str(index) for index in range(65)]},
            {"scope": "installation", "service_ids": ["fixture"]},
            {"scope": "unknown"},
        ):
            with (
                self.subTest(options=options),
                self.assertRaises(InstallationArchiveError),
            ):
                scope_options(options)


if __name__ == "__main__":
    unittest.main()
