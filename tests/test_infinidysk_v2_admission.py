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
        self.addCleanup(self.assertFalse, admission._V2_PENDING)
        self.addCleanup(self.assertFalse, admission._EXTERNAL_MUTATIONS)

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
