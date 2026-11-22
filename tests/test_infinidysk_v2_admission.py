"""Cross-generation migration admission, without touching runtime or databases."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from utils import arr_postgres_migration as legacy_pg
from utils import infinidysk_migration as namespace
from utils import infinidysk_migration_admission as admission
from utils import infinidysk_v2_migration as major
from utils import infinidysk_v2_postgres as native_pg


class NativeMigrationAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.namespace = namespace.InfiniDyskMigrationManager(root / "namespace.json")
        self.postgres = legacy_pg.ArrPostgresMigrationManager(root / "postgres")
        self.major = Mock()
        self.native = Mock()
        for manager in (self.major, self.native):
            manager.has_blocking_job.return_value = False
            manager.has_recovery_pending_job.return_value = False
        for module, name, value in (
            (namespace, "INFINIDYSK_MIGRATION_MANAGER", self.namespace),
            (legacy_pg, "POSTGRES_MIGRATION_MANAGER", self.postgres),
            (major, "INFINIDYSK_V2_MIGRATION_MANAGER", self.major),
            (native_pg, "INFINIDYSK_V2_POSTGRES_MANAGER", self.native),
        ):
            replacement = patch.object(module, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.assertFalse(admission._V2_PENDING)
        self.assertFalse(admission._EXTERNAL_MUTATIONS)
        self.assertFalse(admission._INSTALLATION_CONTROL_WRITES)
        self.addCleanup(self.assertFalse, admission._V2_PENDING)
        self.addCleanup(self.assertFalse, admission._EXTERNAL_MUTATIONS)
        self.addCleanup(self.assertFalse, admission._INSTALLATION_CONTROL_WRITES)

    def legacy_job(self, service_key="infinidysk", status="running"):
        identifier = uuid.uuid4()
        payload = {
            "job_id": identifier.hex,
            "service_key": service_key,
            "process_name": "Legacy service",
            "status": status,
            "rollback_available": status in {"failed", "rollback_failed"},
            "worker_pid": os.getpid(),
            "worker_id": self.postgres._worker_id,
        }
        self.postgres._create_job(identifier, payload)
        return payload

    def create_legacy_postgres(self, service_key="infinidysk"):
        config = Mock()
        config.find_key_for_process.return_value = (service_key, None)
        return self.postgres.create_job(
            config,
            Mock(),
            Mock(),
            Mock(),
            "Legacy service",
            "cutover",
            False,
            "CONFIRM",
            True,
            True,
            True,
        )

    def test_old_namespace_and_provider_start_refuse_each_native_owner_before_work(
        self,
    ):
        for owner in (self.major, self.native):
            with self.subTest(owner=owner):
                owner.has_blocking_job.return_value = True
                with (
                    patch.object(self.namespace, "_load_preflight") as preflight,
                    patch.object(self.namespace, "_save_state") as save,
                    patch.object(self.postgres, "_create_job_admitted") as create,
                ):
                    for operation in (
                        lambda: self.namespace.start_full_namespace_job(
                            "unused", True, Mock(), Mock(), Mock()
                        ),
                        lambda: self.namespace.apply_full_namespace(
                            "unused", True, Mock(), Mock()
                        ),
                        self.namespace.apply_brand_cutover,
                    ):
                        with self.assertRaises(namespace.InfiniDyskMigrationError):
                            operation()
                    for service_key in ("infinidysk", "radarr"):
                        with self.assertRaises(legacy_pg.ArrPostgresMigrationError):
                            self.create_legacy_postgres(service_key)
                preflight.assert_not_called()
                save.assert_not_called()
                create.assert_not_called()
                owner.has_blocking_job.return_value = False

    def test_old_provider_rollback_refuses_each_native_owner_for_all_services(self):
        for owner in (self.major, self.native):
            owner.has_blocking_job.return_value = True
            for service_key in ("infinidysk", "radarr"):
                with self.subTest(owner=owner, service=service_key):
                    payload = self.legacy_job(service_key, "failed")
                    with (
                        patch.object(
                            self.postgres,
                            "_reserve_rollback",
                            side_effect=AssertionError("rollback admission bypassed"),
                        ) as reserve,
                        self.assertRaises(legacy_pg.ArrPostgresMigrationError),
                    ):
                        self.postgres.rollback_job(
                            payload["job_id"], "ROLLBACK", Mock(), Mock(), Mock()
                        )
                    reserve.assert_not_called()
            owner.has_blocking_job.return_value = False

    def test_native_jobs_refuse_legacy_namespace_active_or_recovery(self):
        for kind in ("major", "postgres"):
            for status in ("running", "rollback_attention_required"):
                with (
                    self.subTest(kind=kind, status=status),
                    patch.object(
                        self.namespace, "get_job", return_value={"status": status}
                    ),
                ):
                    with self.assertRaises(admission.InfiniDyskMigrationAdmissionError):
                        with admission.admit_v2_job(kind):
                            self.fail("Native job bypassed legacy namespace admission")

    def test_native_jobs_refuse_all_legacy_provider_active_and_guarded_rollback(self):
        for service_key in ("infinidysk", "radarr"):
            for status in ("running", "failed", "rollback_failed"):
                payload = self.legacy_job(service_key, status)
                for kind in ("major", "postgres"):
                    with self.subTest(service=service_key, status=status, kind=kind):
                        with self.assertRaises(
                            admission.InfiniDyskMigrationAdmissionError
                        ):
                            with admission.admit_v2_job(kind):
                                self.fail(
                                    "Native job bypassed legacy provider admission"
                                )
                self.postgres._new_job_path(uuid.UUID(payload["job_id"])).unlink()

    def test_owner_context_bypasses_only_its_kind_and_never_another_thread(self):
        for kind, own, other in (
            ("major", self.major, self.native),
            ("postgres", self.native, self.major),
        ):
            with self.subTest(kind=kind):
                own.has_blocking_job.return_value = True
                own.has_recovery_pending_job.return_value = True
                observed = []
                with admission.v2_migration_owner(kind):
                    self.assertFalse(admission.infinidysk_v2_jobs_active())
                    self.assertFalse(admission.infinidysk_v2_recovery_pending())
                    worker = threading.Thread(
                        target=lambda: observed.append(
                            admission.infinidysk_v2_jobs_active()
                        )
                    )
                    worker.start()
                    worker.join(timeout=2)
                    self.assertFalse(worker.is_alive())
                    other.has_blocking_job.return_value = True
                    self.assertTrue(admission.infinidysk_v2_jobs_active())
                    other.has_blocking_job.return_value = False
                self.assertEqual(observed, [True])
                self.assertTrue(admission.infinidysk_v2_jobs_active())
                self.assertTrue(admission.infinidysk_v2_recovery_pending())
                own.has_blocking_job.return_value = False
                own.has_recovery_pending_job.return_value = False

    def race(self, left, right):
        barrier = threading.Barrier(3)
        outcomes = []
        errors = []

        def attempt(label, callback):
            try:
                barrier.wait(timeout=2)
                callback()
                outcomes.append((label, "accepted"))
            except (
                admission.InfiniDyskMigrationAdmissionError,
                legacy_pg.ArrPostgresMigrationError,
            ):
                outcomes.append((label, "blocked"))
            except BaseException as error:
                errors.append(error)

        workers = [
            threading.Thread(target=attempt, args=("left", left)),
            threading.Thread(target=attempt, args=("right", right)),
        ]
        for worker in workers:
            worker.start()
        barrier.wait(timeout=2)
        for worker in workers:
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive(), "Admission thread did not exit")
        self.assertEqual(errors, [])
        self.assertEqual(
            sorted(outcome for _, outcome in outcomes), ["accepted", "blocked"]
        )

    def test_simultaneous_legacy_provider_and_native_start_have_one_winner(self):
        def native_start():
            with admission.admit_v2_job("major"):
                self.major.has_blocking_job.return_value = True

        def legacy_start():
            self.create_legacy_postgres()

        with patch.object(
            self.postgres,
            "_create_job_admitted",
            side_effect=lambda *args: self.legacy_job(),
        ):
            self.race(native_start, legacy_start)

    def test_simultaneous_major_and_native_provider_start_have_one_winner(self):
        def start(kind, manager):
            with admission.admit_v2_job(kind):
                manager.has_blocking_job.return_value = True

        self.race(
            lambda: start("major", self.major), lambda: start("postgres", self.native)
        )

    def test_pending_native_admission_blocks_external_mutation_before_job_persist(self):
        with admission.admit_v2_job("major"):
            # An unrelated request thread must see the reservation even before
            # the manager has written its first durable job record.
            outcomes = []

            def reserve():
                try:
                    token = admission.reserve_infinidysk_external_mutation(
                        "config write"
                    )
                except admission.InfiniDyskMigrationAdmissionError:
                    outcomes.append("blocked")
                else:
                    admission.release_infinidysk_external_mutation(token)
                    outcomes.append("accepted")

            worker = threading.Thread(target=reserve)
            worker.start()
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes, ["blocked"])
        token = admission.reserve_infinidysk_external_mutation("config write")
        try:
            with self.assertRaises(admission.InfiniDyskMigrationAdmissionError):
                with admission.admit_v2_job("major"):
                    self.fail("Native start bypassed an existing API reservation")
        finally:
            admission.release_infinidysk_external_mutation(token)

    def test_background_persistence_does_not_block_v1_or_v2_migration_admission(self):
        from utils.installation_backup import installation_persistence_work

        for label in (
            "Metrics history persistence",
            "Notification persistence",
            "Seerr synchronization cycle",
            # Reservation type, rather than its diagnostic label, is authority.
            "native runtime update: background fixture",
        ):
            with self.subTest(label=label):
                with installation_persistence_work(label) as admitted:
                    self.assertTrue(admitted)
                    self.assertTrue(admission.installation_persistence_active())
                    self.assertFalse(admission.infinidysk_external_mutation_active())
                    self.assertEqual(len(admission._INSTALLATION_CONTROL_WRITES), 1)
                    for kind in ("major", "postgres"):
                        with admission.admit_v2_job(kind):
                            self.assertIn(kind, admission._V2_PENDING)
                    with patch.object(
                        self.postgres, "_create_job_admitted", return_value="admitted"
                    ) as work:
                        self.assertEqual(self.create_legacy_postgres(), "admitted")
                        work.assert_called_once()
                self.assertFalse(admission._INSTALLATION_CONTROL_WRITES)
                self.assertFalse(admission.installation_persistence_active())

    def test_actual_lifecycle_reservation_still_blocks_with_background_writer(self):
        control = admission.reserve_installation_control_write(
            "Seerr synchronization cycle"
        )
        external = admission.reserve_infinidysk_external_mutation(
            "Metrics history persistence"
        )
        try:
            admission.release_installation_control_write(control)
            self.assertTrue(admission.installation_persistence_active())
            self.assertTrue(admission.infinidysk_external_mutation_active())
            with self.assertRaises(admission.InfiniDyskMigrationAdmissionError):
                with admission.admit_v2_job("postgres"):
                    self.fail("An actual lifecycle reservation was bypassed")
            with (
                patch.object(self.postgres, "_create_job_admitted") as work,
                self.assertRaises(legacy_pg.ArrPostgresMigrationError),
            ):
                self.create_legacy_postgres()
            work.assert_not_called()
        finally:
            admission.release_installation_control_write(control)
            admission.release_infinidysk_external_mutation(external)

    def test_control_writers_remain_allowed_during_native_work_but_not_backup(self):
        self.major.has_blocking_job.return_value = True
        try:
            token = admission.reserve_installation_control_write(
                "Notification persistence"
            )
            try:
                self.assertTrue(admission.installation_persistence_active())
                self.assertEqual(len(admission._INSTALLATION_CONTROL_WRITES), 1)
            finally:
                admission.release_installation_control_write(token)
        finally:
            self.major.has_blocking_job.return_value = False
        with (
            patch.object(admission, "_installation_blocked", return_value=True),
            self.assertRaises(admission.InfiniDyskMigrationAdmissionError),
        ):
            admission.reserve_installation_control_write("Notification persistence")
        self.assertFalse(admission._INSTALLATION_CONTROL_WRITES)

    def test_releasing_lifecycle_does_not_release_an_independent_control_writer(self):
        external = admission.reserve_infinidysk_external_mutation(
            "service startup/update: fixture"
        )
        control = admission.reserve_installation_control_write(
            "Seerr synchronization cycle"
        )
        try:
            admission.release_infinidysk_external_mutation(external)
            admission.release_infinidysk_external_mutation(control)
            self.assertTrue(admission.installation_persistence_active())
            self.assertFalse(admission.infinidysk_external_mutation_active())
            self.assertIn(control, admission._INSTALLATION_CONTROL_WRITES)
            with admission.admit_v2_job("postgres"):
                pass
        finally:
            admission.release_installation_control_write(control)
            admission.release_infinidysk_external_mutation(external)

    def test_pending_reason_does_not_probe_later_guards_or_clear_existing_owner(self):
        admission._V2_PENDING.add("postgres")
        try:
            with (
                patch.object(
                    admission,
                    "infinidysk_namespace_migration_active",
                    side_effect=AssertionError("A later guard was evaluated"),
                ),
                self.assertRaises(
                    admission.InfiniDyskMigrationAdmissionError
                ) as caught,
            ):
                with admission.admit_v2_job("postgres"):
                    self.fail("An existing pending operation was bypassed")
            self.assertEqual(
                caught.exception.reason_code, admission.AdmissionReason.PENDING.value
            )
            self.assertIn("starting or finishing", caught.exception.public_message)
            self.assertEqual(admission._V2_PENDING, {"postgres"})
        finally:
            admission._V2_PENDING.discard("postgres")

    def test_admission_reports_first_blocker_in_original_short_circuit_order(self):
        guards = (
            (
                admission,
                "infinidysk_namespace_migration_active",
                admission.AdmissionReason.NAMESPACE,
            ),
            (
                admission,
                "infinidysk_postgres_migration_active",
                admission.AdmissionReason.LEGACY_POSTGRES,
            ),
            (
                self.postgres,
                "has_namespace_conflicting_job",
                admission.AdmissionReason.SERVICE_DATABASE,
            ),
            (
                admission,
                "infinidysk_v2_jobs_active",
                admission.AdmissionReason.NATIVE_MIGRATION,
            ),
            (admission, "_installation_blocked", admission.AdmissionReason.BACKUP),
        )
        for active, (_module, _name, expected) in enumerate(guards):
            with self.subTest(reason=expected):
                calls = []
                replacements = []
                for index, (module, name, _reason) in enumerate(guards):

                    def check(index=index):
                        calls.append(index)
                        return index == active

                    replacement = patch.object(module, name, side_effect=check)
                    replacement.start()
                    replacements.append(replacement)
                try:
                    with self.assertRaises(
                        admission.InfiniDyskMigrationAdmissionError
                    ) as caught:
                        with admission.admit_v2_job("postgres"):
                            self.fail("An active owner was bypassed")
                    self.assertEqual(caught.exception.reason_code, expected.value)
                    self.assertEqual(calls, list(range(active + 1)))
                    self.assertEqual(
                        str(caught.exception), admission.EXTERNAL_MUTATION_BLOCKER
                    )
                    self.assertFalse(admission._V2_PENDING)
                finally:
                    for replacement in reversed(replacements):
                        replacement.stop()

    def test_retained_same_kind_owner_has_distinct_reason_and_existing_bypass_remains(
        self,
    ):
        for kind, manager, expected in (
            ("major", self.major, admission.AdmissionReason.MAJOR_MIGRATION),
            ("postgres", self.native, admission.AdmissionReason.NATIVE_POSTGRES),
        ):
            with self.subTest(kind=kind):
                manager.has_blocking_job.return_value = True
                try:
                    with self.assertRaises(
                        admission.InfiniDyskMigrationAdmissionError
                    ) as caught:
                        with admission.admit_v2_job(kind):
                            self.fail("An active same-kind owner was bypassed")
                    self.assertEqual(caught.exception.reason_code, expected.value)
                    with admission.admit_v2_job(kind, existing=True):
                        self.assertIn(kind, admission._V2_PENDING)
                finally:
                    manager.has_blocking_job.return_value = False
                self.assertFalse(admission._V2_PENDING)

    def test_external_labels_produce_only_static_safe_categories(self):
        cases = (
            (
                "InfiniDysk managed Arr credential synchronization",
                admission.AdmissionReason.ARR_SYNC,
            ),
            (
                "native runtime update: private-fixture-name",
                admission.AdmissionReason.RUNTIME_UPDATE,
            ),
            (
                "service startup/update: private-fixture-name",
                admission.AdmissionReason.SERVICE_LIFECYCLE,
            ),
            ("Managed arrangement setup", admission.AdmissionReason.SETUP),
            (
                "API POST /private-fixture-path/fixture-token",
                admission.AdmissionReason.API_REQUEST,
            ),
            (
                "InfiniDysk configuration synchronization",
                admission.AdmissionReason.CONFIGURATION,
            ),
            (
                "symlink repair private-fixture-path",
                admission.AdmissionReason.MAINTENANCE,
            ),
            (
                "rclone optimizer apply private-fixture-name",
                admission.AdmissionReason.MAINTENANCE,
            ),
            (
                "unknown private-fixture-path password=fixture-token",
                admission.AdmissionReason.EXTERNAL,
            ),
        )
        for label, expected in cases:
            with self.subTest(reason=expected):
                token = admission.reserve_infinidysk_external_mutation(label)
                try:
                    with (
                        patch.object(
                            admission,
                            "_installation_blocked",
                            side_effect=AssertionError("A later guard was evaluated"),
                        ),
                        self.assertRaises(
                            admission.InfiniDyskMigrationAdmissionError
                        ) as caught,
                    ):
                        with admission.admit_v2_job("postgres"):
                            self.fail("An external mutation was bypassed")
                    self.assertEqual(caught.exception.reason_code, expected.value)
                    self.assertNotIn("private-fixture", caught.exception.public_message)
                    self.assertNotIn("fixture-token", caught.exception.public_message)
                    self.assertNotIn(label, caught.exception.public_message)
                finally:
                    admission.release_infinidysk_external_mutation(token)

    def test_mixed_external_categories_have_a_generic_static_message(self):
        labels = {
            "one": "API POST /private-fixture-path",
            "two": "native runtime update: private-fixture-name",
        }
        with (
            patch.dict(admission._EXTERNAL_MUTATIONS, labels),
            self.assertRaises(admission.InfiniDyskMigrationAdmissionError) as caught,
        ):
            with admission.admit_v2_job("postgres"):
                self.fail("Mixed external mutations were bypassed")
        self.assertEqual(
            caught.exception.reason_code, admission.AdmissionReason.EXTERNAL.value
        )
        self.assertNotIn("private-fixture", caught.exception.public_message)

    def test_private_diagnostic_or_unknown_reason_never_becomes_public_message(self):
        for reason in (
            admission.AdmissionReason.CONFLICT,
            "private-fixture-reason",
            None,
        ):
            error = admission.InfiniDyskMigrationAdmissionError(
                "private-fixture-diagnostic", reason=reason
            )
            self.assertEqual(str(error), "private-fixture-diagnostic")
            self.assertEqual(
                error.public_message,
                "Another lifecycle or migration operation owns admission.",
            )
            self.assertEqual(
                error.reason_code, admission.AdmissionReason.CONFLICT.value
            )

    def test_onboarding_options_cannot_switch_populated_v1_or_rebind_native_postgres(
        self,
    ):
        code = textwrap.dedent("""
            import copy
            from pathlib import Path
            from tempfile import TemporaryDirectory
            from types import SimpleNamespace
            from unittest.mock import Mock, patch
            from fastapi import HTTPException
            from api.routers import process
            with TemporaryDirectory() as scratch:
                legacy = {'runtime_generation': '1', 'config_dir': scratch}
                Path(scratch, 'db.sqlite').write_bytes(b'retained v1 source')
                manager = SimpleNamespace(config={'infinidysk': legacy, 'postgres': {}}, get=lambda key, section=None: scratch if key == 'data_root' and section is None else None)
                before = copy.deepcopy(legacy)
                with patch.object(process, 'CONFIG_MANAGER', manager), \
                     patch.object(process, 'infinidysk_postgres_migration_active', return_value=False), \
                     patch.object(process, 'infinidysk_namespace_migration_active', return_value=False):
                    try:
                        process.apply_service_options(legacy, {'runtime_generation': '2'}, Mock(), service_key='infinidysk')
                    except HTTPException as error:
                        assert error.status_code == 400
                    else:
                        raise AssertionError('Raw onboarding options bypassed guided major migration')
                    assert legacy == before
                    assert Path(scratch, 'db.sqlite').read_bytes() == b'retained v1 source'
                    native = {'runtime_generation': '2', 'v2_data_dir': scratch}
                    manager.config['infinidysk'] = native
                    postgres = {'host': 'localhost', 'port': 5432, 'user': 'example'}
                    Path(scratch, 'database.toml').write_text('url = "postgresql://localhost:5432/native"')
                    before = copy.deepcopy(postgres)
                    try:
                        process.apply_service_options(postgres, {'port': 5544}, Mock(), service_key='postgres')
                    except HTTPException as error:
                        assert error.status_code == 409
                    else:
                        raise AssertionError('Raw onboarding options rebound active native provider')
                    assert postgres == before
        """)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_api_owner_authority_is_scoped_and_payload_cannot_grant_other_kind(self):
        # Router tests elsewhere install module doubles. Use a fresh interpreter
        # to verify the real dispatcher while keeping runtime/config isolated.
        code = textwrap.dedent("""
            from unittest.mock import Mock, patch
            from fastapi import HTTPException
            from api.routers import infinidysk_v2 as router
            from utils import infinidysk_migration_admission as admission
            from utils import infinidysk_v2_migration as major
            from utils import infinidysk_v2_postgres as postgres
            manager = Mock()
            def rollback(payload):
                assert getattr(admission._V2_CONTEXT, 'owners') == {'major'}
                assert not admission.infinidysk_v2_jobs_active()
                return {'job': {'status': 'rolled_back'}}
            manager.rollback.side_effect = rollback
            with patch.object(router, '_manager', return_value=manager), \
                 patch.object(router, 'get_project_version', return_value='2.24.0'), \
                 patch.object(admission, 'infinidysk_namespace_migration_active', return_value=False), \
                 patch.object(admission, 'infinidysk_postgres_migration_active', return_value=False), \
                 patch.object(major.INFINIDYSK_V2_MIGRATION_MANAGER, 'has_blocking_job', return_value=True), \
                 patch.object(postgres.INFINIDYSK_V2_POSTGRES_MANAGER, 'has_blocking_job', return_value=False):
                assert router._invoke('major', 'rollback', {}, None, None, mutation=True)['job']['status'] == 'rolled_back'
                manager.rollback.reset_mock()
                with patch.object(postgres.INFINIDYSK_V2_POSTGRES_MANAGER, 'has_blocking_job', return_value=True):
                    try:
                        router._invoke('major', 'rollback', {'owner': 'postgres'}, None, None, mutation=True)
                    except HTTPException as error:
                        assert error.status_code == 409
                    else:
                        raise AssertionError('Payload bypassed another native job')
                manager.rollback.assert_not_called()
                try:
                    router._invoke('major', 'start', {'existing': True, 'owner': 'major'}, None, None, mutation=True)
                except HTTPException as error:
                    assert error.status_code == 409
                else:
                    raise AssertionError('Payload bypassed same-kind existing job')
                manager.start.assert_not_called()
            assert not admission._V2_PENDING
            assert not getattr(admission._V2_CONTEXT, 'owners', frozenset())
        """)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
