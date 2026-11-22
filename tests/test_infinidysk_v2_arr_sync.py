"""Recurring native credentials retain exact managed-client ownership."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_infinidysk_v2_arr_clients as fixtures
from utils import infinidysk_v2_arr_sync as sync
from utils.infinidysk_v2_arr_clients import ArrClientReconciliationError, _fingerprint
from utils.integration_result import integration_targets


def coordinator_manager(test):
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    root = Path(temporary.name)
    return SimpleNamespace(
        config={"infinidysk": {"v2_runtime_dir": str(root / "runtime")}},
        file_path=str(root / "dumb_config.json"),
    )


class RecurringArrTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ManagedClientTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.native()
        self.fixture.config["infinidysk"].update(enabled=True)
        self.key = fixtures._NATIVE_KEY
        self.fixture.config["infinidysk"]["env"] = {
            "INFINIDYSK__SAB__API_KEY": self.key
        }
        self.fixture.accepted = {self.key}
        self.fixture.stored_key = self.key
        # A genuinely unrelated downloader uses another endpoint.
        next(
            field for field in self.fixture.other["fields"] if field["name"] == "port"
        )["value"] = 9191
        self.adopt = Mock(
            side_effect=lambda manager, service, effective: service["env"].get(
                "INFINIDYSK__SAB__API_KEY", effective
            )
        )
        self.create = Mock(side_effect=AssertionError("Creation was not expected"))

    def run_sync(self, **kwargs):
        return sync.sync_arr_clients(
            self.fixture.config,
            request=self.fixture.request,
            native_key=lambda *_: self.key,
            create_client=self.create,
            adopt_key=self.adopt,
            **kwargs,
        )

    def ledger(self):
        return self.fixture.root / ".dumb-integrations.json"

    def test_passing_legacy_client_binds_then_configured_key_change_repairs_same_id(
        self,
    ):
        original = copy.deepcopy(self.fixture.client)
        other = copy.deepcopy(self.fixture.other)
        self.assertFalse(self.run_sync()[0]["changed"])
        self.key = "second-native-fixture-sab-key"
        self.fixture.config["infinidysk"]["env"]["INFINIDYSK__SAB__API_KEY"] = self.key
        self.fixture.accepted = {self.key}
        self.assertTrue(self.run_sync()[0]["changed"])
        self.assertEqual(len(self.fixture.puts), 1)
        expected = copy.deepcopy(original)
        next(field for field in expected["fields"] if field["name"] == "apiKey")[
            "value"
        ] = self.key
        self.assertEqual(self.fixture.puts[0], expected)
        self.assertEqual(self.fixture.other, other)
        self.assertFalse(self.run_sync()[0]["changed"])
        self.assertEqual(len(self.fixture.puts), 1)
        raw = self.ledger().read_text()
        self.assertNotIn(self.key, raw)
        self.assertNotIn("arr-fixture-only-token", raw)
        self.assertEqual(json.loads(raw)[sync._OWNER]["client_id"], 4)

    def test_broken_unowned_client_is_never_adopted_or_changed(self):
        self.fixture.stored_key = fixtures._SOURCE_ALIAS
        with self.assertRaisesRegex(ArrClientReconciliationError, "no verified"):
            self.run_sync()
        self.assertFalse(self.ledger().exists())
        self.assertEqual(self.fixture.puts, [])
        self.create.assert_not_called()

    def test_existing_generic_ownership_authorizes_key_repair_without_name_adoption(
        self,
    ):
        values = sync._fields(self.fixture.client)
        self.ledger().write_text(
            json.dumps(
                {
                    "unrelated.integration": {"keep": True},
                    "infinidysk.usenet": {
                        "id": 4,
                        "fingerprint": _fingerprint(
                            {
                                "name": self.fixture.client["name"],
                                "implementation": self.fixture.client["implementation"],
                                "fields": {
                                    name: values.get(name)
                                    for name in sync._MANAGED_FIELDS
                                },
                            }
                        ),
                    },
                }
            )
        )
        self.ledger().chmod(0o600)
        self.fixture.stored_key = fixtures._SOURCE_ALIAS
        self.assertTrue(self.run_sync()[0]["changed"])
        self.assertEqual(
            json.loads(self.ledger().read_text())["unrelated.integration"],
            {"keep": True},
        )

    def test_resource_edit_after_binding_is_preserved_for_review(self):
        self.run_sync()
        self.fixture.client["tags"] = [22]
        with self.assertRaisesRegex(ArrClientReconciliationError, "scope changed"):
            self.run_sync()
        self.assertEqual(self.fixture.puts, [])
        self.assertEqual(self.fixture.client["tags"], [22])

    def test_unknown_client_on_same_endpoint_blocks_creation_and_adoption(self):
        next(
            field for field in self.fixture.other["fields"] if field["name"] == "port"
        )["value"] = 8181
        with self.assertRaises(ArrClientReconciliationError):
            self.run_sync()
        self.create.assert_not_called()
        self.assertFalse(self.ledger().exists())

    def test_new_core_link_can_create_when_no_client_or_endpoint_conflict_exists(self):
        request = self.fixture.request
        present = False

        def api(*args):
            if args[3:5] == ("downloadclient", "GET") and not present:
                return [copy.deepcopy(self.fixture.other)]
            return request(*args)

        def create(*_args):
            nonlocal present
            present = True
            return True, 4

        self.create.side_effect = create
        with patch.object(self.fixture, "request", side_effect=api):
            self.assertFalse(self.run_sync()[0]["changed"])
        self.create.assert_called_once()
        self.assertTrue(self.ledger().exists())

    def test_native_config_mismatch_precedes_all_arr_calls(self):
        self.adopt.return_value = "not-active-yet"
        self.adopt.side_effect = None
        with self.assertRaises(ArrClientReconciliationError):
            self.run_sync()
        self.assertEqual(self.fixture.requests, [])

    def test_configuration_review_guidance_is_not_lost_as_readiness_failure(self):
        from utils.infinidysk_v2_sab import NativeSabKeyError

        message = "Restart InfiniDysk before reconnecting managed Arr clients."
        self.adopt.side_effect = NativeSabKeyError(message)
        with self.assertRaisesRegex(NativeSabKeyError, "Restart InfiniDysk"):
            self.run_sync()
        self.assertEqual(self.fixture.requests, [])
        self.create.assert_not_called()

    def test_native_key_is_adopted_even_without_linked_arrs(self):
        self.fixture.config["sonarr"]["instances"]["InfiniDysk"]["enabled"] = False
        self.assertEqual(self.run_sync(), [])
        self.adopt.assert_called_once()
        self.assertEqual(self.fixture.requests, [])

    def test_explicit_integration_target_scope_protects_other_arrs(self):
        with integration_targets(["radarr.unrelated"]):
            self.assertEqual(self.run_sync(), [])
        self.assertEqual(self.fixture.requests, [])

    def test_unsafe_ledger_is_a_static_review_error_without_client_mutation(self):
        self.ledger().symlink_to(self.fixture.root / "config.xml")
        with self.assertRaises(ArrClientReconciliationError) as caught:
            self.run_sync()
        self.assertEqual(caught.exception.reason_code, "ownership_unverified")
        self.assertNotIn(str(self.fixture.root), str(caught.exception))
        self.assertEqual(self.fixture.requests, [])

    def test_shutdown_or_lost_reservation_after_native_response_prevents_adoption(self):
        self.fixture.config["sonarr"]["instances"]["InfiniDysk"]["enabled"] = False
        cancel = Mock(side_effect=[None, RuntimeError("reservation lost")])
        with self.assertRaisesRegex(RuntimeError, "reservation lost"):
            self.run_sync(check_cancel=cancel)
        self.adopt.assert_not_called()
        self.assertEqual(self.fixture.requests, [])

    def test_missing_arr_xml_is_a_bounded_readiness_dependency(self):
        (self.fixture.root / "config.xml").unlink()
        with self.assertRaises(ArrClientReconciliationError) as caught:
            self.run_sync()
        self.assertEqual(caught.exception.reason_code, "arr_unready")
        self.assertEqual(self.fixture.requests, [])

    def test_successful_migration_promotes_its_proof_for_later_startup(self):
        self.fixture.config["infinidysk"] = self.fixture.source
        self.fixture.accepted = {fixtures._SOURCE_ALIAS, fixtures._NATIVE_KEY}
        proof = self.fixture.capture()
        self.fixture.native()
        self.fixture.accepted = {fixtures._NATIVE_KEY}
        self.fixture.reconcile(proof)
        record = json.loads(self.ledger().read_text())[sync._OWNER]
        self.assertEqual(record["client_id"], 4)
        self.assertNotIn(fixtures._NATIVE_KEY, self.ledger().read_text())


class SchedulingTests(unittest.TestCase):
    def test_guarded_migration_defers_before_native_or_arr_calls(self):
        from utils.infinidysk_migration_admission import (
            InfiniDyskMigrationAdmissionError,
        )

        coordinator = sync.ArrSync(
            SimpleNamespace(shutting_down=False, logger=Mock()),
            coordinator_manager(self),
        )
        with (
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                side_effect=InfiniDyskMigrationAdmissionError("guarded"),
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ),
            patch.object(sync, "sync_arr_clients") as reconcile,
        ):
            changed, detail = coordinator.run()
        self.assertFalse(changed)
        self.assertIn("deferred", detail)
        reconcile.assert_not_called()

    def test_native_readiness_failure_schedules_bounded_retry_without_secret_logs(self):
        coordinator = sync.ArrSync(
            SimpleNamespace(shutting_down=False, logger=Mock()),
            coordinator_manager(self),
        )
        coordinator.retry = Mock()
        with (
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="fixture-token",
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ),
            patch.object(
                sync,
                "sync_arr_clients",
                side_effect=ArrClientReconciliationError("native_key_unavailable"),
            ),
        ):
            changed, detail = coordinator.run()
        self.assertFalse(changed)
        self.assertIn("native download-client key", detail)
        coordinator.retry.schedule.assert_called_once_with(
            coordinator.run, coordinator.shutdown
        )

    def test_startup_and_restart_share_one_controller_and_skip_unlinked_services(self):
        manager = {"infinidysk": {"enabled": True, "runtime_generation": "2"}}
        handler = SimpleNamespace(shutting_down=False, logger=Mock())
        with (
            patch("utils.config_loader.CONFIG_MANAGER", manager),
            patch.object(
                sync.BoundedIntegrationRetry, "schedule", return_value=True
            ) as schedule,
        ):
            self.assertFalse(
                sync.schedule_arr_sync(
                    handler, "sonarr", {"enabled": True, "core_service": "other"}
                )
            )
            self.assertTrue(
                sync.schedule_arr_sync(handler, "infinidysk", manager["infinidysk"])
            )
            controller = handler._native_arr_sync
            self.assertTrue(
                sync.schedule_arr_sync(
                    handler, "sonarr", {"enabled": True, "core_service": "infinidysk"}
                )
            )
            self.assertIs(controller, handler._native_arr_sync)
        self.assertEqual(schedule.call_count, 2)

    def test_genuine_restart_renews_an_exhausted_retry_chain(self):
        manager = {"infinidysk": {"enabled": True, "runtime_generation": "2"}}
        handler = SimpleNamespace(shutting_down=False, logger=Mock())
        coordinator = sync._coordinator(handler, manager)
        coordinator.retry._attempts = coordinator.retry.max_attempts
        with (
            patch("utils.config_loader.CONFIG_MANAGER", manager),
            patch.object(coordinator.retry, "schedule", return_value=True),
        ):
            self.assertTrue(
                sync.schedule_arr_sync(handler, "infinidysk", manager["infinidysk"])
            )
        self.assertEqual(coordinator.retry._attempts, 0)


class ConfigurationReviewTests(unittest.TestCase):
    def test_configuration_review_is_shown_without_readiness_retries(self):
        from utils.infinidysk_v2_sab import NativeSabKeyError

        coordinator = sync.ArrSync(
            SimpleNamespace(shutting_down=False, logger=Mock()),
            coordinator_manager(self),
        )
        coordinator.retry = Mock()
        message = "Use individual native SAB environment settings."
        outcomes = []
        with (
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="fixture-token",
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ),
            patch.object(
                sync, "sync_arr_clients", side_effect=NativeSabKeyError(message)
            ),
            patch.object(sync, "emit_action", side_effect=outcomes.append),
        ):
            self.assertEqual(coordinator.run(), (False, message))
        self.assertEqual(outcomes[0].execution, "waiting_input")
        self.assertEqual(outcomes[0].detail, message)
        self.assertFalse(outcomes[0].retryable)
        coordinator.retry.schedule.assert_not_called()


class NativeUpdateJournalTests(unittest.TestCase):
    def test_interrupted_native_update_blocks_sync_and_preserves_real_journal(self):
        from tests import test_infinidysk_v2_update as native_fixtures
        from utils.infinidysk_v2_update import NativeUpdateRecoveryRequired

        fixture = native_fixtures.NativeRuntimeUpdateTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.journals = fixture.root / "updates" / "infinidysk-v2"
        with self.assertRaises(NativeUpdateRecoveryRequired):
            fixture.update(
                start_candidate=Mock(side_effect=RuntimeError("fixture launch failure"))
            )
        journal, _ = fixture.journal()
        before = journal.read_bytes()
        manager = SimpleNamespace(
            config={"infinidysk": fixture.service},
            file_path=str(fixture.root / "dumb_config.json"),
        )
        coordinator = sync.ArrSync(
            SimpleNamespace(shutting_down=False, logger=Mock()), manager
        )
        coordinator.retry = Mock()
        with (
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="fixture-token",
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ),
            patch.object(sync, "sync_arr_clients") as reconcile,
        ):
            changed, detail = coordinator.run()
        self.assertFalse(changed)
        self.assertIn("runtime update needs recovery", detail)
        reconcile.assert_not_called()
        coordinator.retry.schedule.assert_not_called()
        self.assertEqual(journal.read_bytes(), before)

    def test_completed_native_update_allows_sync_through_real_journal_check(self):
        from tests import test_infinidysk_v2_update as native_fixtures

        fixture = native_fixtures.NativeRuntimeUpdateTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.journals = fixture.root / "updates" / "infinidysk-v2"
        fixture.update()
        manager = SimpleNamespace(
            config={"infinidysk": fixture.service},
            file_path=str(fixture.root / "dumb_config.json"),
        )
        coordinator = sync.ArrSync(
            SimpleNamespace(shutting_down=False, logger=Mock()), manager
        )
        with (
            patch(
                "utils.infinidysk_migration_admission.reserve_infinidysk_external_mutation",
                return_value="fixture-token",
            ),
            patch(
                "utils.infinidysk_migration_admission.release_infinidysk_external_mutation"
            ),
            patch.object(sync, "sync_arr_clients", return_value=[]) as reconcile,
        ):
            self.assertEqual(coordinator.run(), (False, None))
        reconcile.assert_called_once()


if __name__ == "__main__":
    unittest.main()
