"""A v1 accepted alias must not strand the same saved client after v2 cutover."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from utils.infinidysk_v2_arr_clients import (
    ArrClientReconciliationError,
    _fingerprint,
    capture_arr_bindings,
    reconcile_arr_bindings,
)

_SOURCE_ALIAS = "legacy-frontend-fixture-alias"
_NATIVE_KEY = "retained-native-fixture-sab-key"
_PLAN = "a" * 64


class ManagedClientTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        xml = self.root / "config.xml"
        xml.write_text("<Config><ApiKey>arr-fixture-only-token</ApiKey></Config>")
        self.entry = {
            "enabled": True,
            "core_service": "infinidysk",
            "process_name": "Sonarr InfiniDysk",
            "instance_name": "InfiniDysk",
            "port": 8990,
            "config_file": str(xml),
        }
        self.source = {
            "runtime_generation": "1",
            "process_name": "InfiniDysk",
            "backend_port": 8181,
            "frontend_port": 3000,
            "env": {"FRONTEND_BACKEND_API_KEY": _SOURCE_ALIAS},
        }
        self.config = {
            "infinidysk": self.source,
            "sonarr": {"instances": {"InfiniDysk": self.entry}},
        }
        self.consumers = [
            {
                "service_key": "sonarr",
                "instance": "InfiniDysk",
                "process_name": "Sonarr InfiniDysk",
                "config_fingerprint": _fingerprint(self.entry),
            }
        ]
        self.names = ["Sonarr InfiniDysk"]
        self.client = {
            "id": 4,
            "name": "InfiniDysk",
            "enable": True,
            "implementation": "Sabnzbd",
            "configContract": "SabnzbdSettings",
            "protocol": "usenet",
            "priority": 7,
            "tags": [9, 2],
            "removeCompletedDownloads": False,
            "removeFailedDownloads": False,
            "fields": [
                {"name": "host", "value": "127.0.0.1"},
                {"name": "port", "value": 8181},
                {"name": "useSsl", "value": False},
                {"name": "urlBase", "value": ""},
                {"name": "apiKey", "value": "********", "privacy": "apiKey"},
                {"name": "username", "value": "fixture-user"},
                {"name": "password", "value": "********", "privacy": "password"},
                {"name": "tvCategory", "value": "sonarr-infinidysk"},
                {"name": "futureField", "value": {"retain": ["all", "values"]}},
            ],
        }
        self.other = copy.deepcopy(self.client)
        self.other.update(id=99, name="Unrelated downloader")
        self.stored_key = _SOURCE_ALIAS
        self.accepted = {_SOURCE_ALIAS, _NATIVE_KEY}
        self.requests, self.puts = [], []
        self.success = None
        self.persist_put = True
        self.before_put_edit = False

    def field(self, name):
        return next(field for field in self.client["fields"] if field["name"] == name)

    def request(self, origin, token, api, path, method="GET", payload=None):
        self.assertEqual(origin, f"http://127.0.0.1:{self.entry['port']}")
        self.assertEqual(token, "arr-fixture-only-token")
        self.assertEqual(api, "v3")
        self.requests.append((method, path, copy.deepcopy(payload)))
        if method == "GET" and path == "downloadclient":
            return [copy.deepcopy(self.client), copy.deepcopy(self.other)]
        if method == "GET" and path == "downloadclient/4":
            if self.before_put_edit and any(
                call[0] == "POST"
                and call[2]
                and any(
                    field.get("value") == _NATIVE_KEY
                    for field in call[2].get("fields", [])
                )
                for call in self.requests
            ):
                self.client["priority"] = 11
            return copy.deepcopy(self.client)
        if method == "POST" and path == "downloadclient/test":
            self.assertEqual(payload["id"], 4)
            supplied = next(
                field["value"]
                for field in payload["fields"]
                if field["name"].lower() == "apikey"
            )
            effective = self.stored_key if supplied == "********" else supplied
            if effective not in self.accepted:
                raise OSError(
                    "Private response included a URL and a fixture credential"
                )
            return self.success
        if method == "PUT" and path == "downloadclient/4":
            self.puts.append(copy.deepcopy(payload))
            if self.persist_put:
                self.stored_key = next(
                    field["value"]
                    for field in payload["fields"]
                    if field["name"].lower() == "apikey"
                )
            return copy.deepcopy(self.client)
        raise AssertionError(f"Unexpected Arr operation: {method} {path}")

    def capture(self):
        return capture_arr_bindings(
            self.config,
            self.source,
            self.consumers,
            self.names,
            plan_fingerprint=_PLAN,
            request=self.request,
        )

    def native(self):
        self.config["infinidysk"] = {
            **self.source,
            "runtime_generation": "2",
            "env": {},
        }
        self.accepted = {_NATIVE_KEY}

    def reconcile(self, proof, key=_NATIVE_KEY):
        return reconcile_arr_bindings(
            self.config,
            self.source,
            self.consumers,
            self.names,
            proof,
            plan_fingerprint=_PLAN,
            request=self.request,
            native_key=lambda service, data_root: key,
        )

    def test_v1_alias_becomes_same_id_native_key_only_and_retry_is_idempotent(self):
        before = json.dumps(self.client, separators=(",", ":"))
        other = copy.deepcopy(self.other)
        proof = self.capture()
        self.assertNotIn(_SOURCE_ALIAS, json.dumps(proof))
        self.assertNotIn(_NATIVE_KEY, json.dumps(proof))
        self.assertNotIn("arr-fixture-only-token", json.dumps(proof))
        self.native()
        result = self.reconcile(proof)
        self.assertEqual(
            result,
            [
                {
                    "process_name": "Sonarr InfiniDysk",
                    "client_id": 4,
                    "changed": True,
                    "connection_verified": True,
                }
            ],
        )
        self.assertEqual(self.stored_key, _NATIVE_KEY)
        self.assertEqual(len(self.puts), 1)
        written = copy.deepcopy(self.puts[0])
        next(field for field in written["fields"] if field["name"] == "apiKey")[
            "value"
        ] = "********"
        self.assertEqual(json.dumps(written, separators=(",", ":")), before)
        self.assertEqual(self.other, other)
        self.assertFalse(self.reconcile(proof)[0]["changed"])
        self.assertEqual(len(self.puts), 1)
        self.assertFalse(
            any(
                method == "POST" and path != "downloadclient/test"
                for method, path, _ in self.requests
            )
        )

    def test_wrong_source_key_fails_before_any_repair_or_credential_change(self):
        self.stored_key = "unrelated-user-fixture-key"
        with self.assertRaisesRegex(ArrClientReconciliationError, "v1 connection test"):
            self.capture()
        self.assertEqual(self.puts, [])
        self.assertEqual(self.stored_key, "unrelated-user-fixture-key")

    def test_unselected_or_unrelated_core_arr_never_requests_credentials(self):
        key = Mock(side_effect=AssertionError("must not reveal a native key"))
        for names, core in (([], "infinidysk"), (self.names, "decypharr")):
            self.entry["core_service"] = core
            self.consumers[0]["config_fingerprint"] = _fingerprint(self.entry)
            proof = capture_arr_bindings(
                self.config,
                self.source,
                self.consumers,
                names,
                plan_fingerprint=_PLAN,
                request=self.request,
            )
            self.assertEqual(proof["records"], [])
            self.assertEqual(
                reconcile_arr_bindings(
                    self.config,
                    self.source,
                    self.consumers,
                    names,
                    proof,
                    plan_fingerprint=_PLAN,
                    native_key=key,
                    request=self.request,
                ),
                [],
            )
        self.assertEqual(self.requests, [])

    def test_name_alone_wrong_endpoint_category_disabled_or_duplicate_is_not_adopted(
        self,
    ):
        original = copy.deepcopy(self.client)
        for name, value in (
            ("host", "192.0.2.1"),
            ("port", 9999),
            ("urlBase", "/unowned"),
            ("tvCategory", "user-category"),
        ):
            self.client = copy.deepcopy(original)
            self.field(name)["value"] = value
            with (
                self.subTest(field=name),
                self.assertRaises(ArrClientReconciliationError),
            ):
                self.capture()
        self.client = copy.deepcopy(original)
        self.client["enable"] = False
        with self.assertRaises(ArrClientReconciliationError):
            self.capture()
        self.client = copy.deepcopy(original)
        self.client["protocol"] = "torrent"
        with self.assertRaises(ArrClientReconciliationError):
            self.capture()
        self.client = copy.deepcopy(original)
        self.source.pop("backend_port")
        with self.assertRaises(ArrClientReconciliationError):
            self.capture()
        self.source["backend_port"] = 8181
        self.client = copy.deepcopy(original)
        self.other = {**copy.deepcopy(original), "id": 8, "name": "NzbDAV"}
        with self.assertRaises(ArrClientReconciliationError):
            self.capture()
        self.assertEqual(self.puts, [])

    def test_missing_or_different_plan_proof_never_mutates_existing_client(self):
        proof = self.capture()
        self.native()
        for candidate in (
            None,
            {**proof, "plan_fingerprint": "b" * 64},
            {**proof, "records": []},
        ):
            with (
                self.subTest(proof=candidate),
                self.assertRaises(ArrClientReconciliationError),
            ):
                self.reconcile(candidate)
        self.assertEqual(self.puts, [])

    def test_changed_noncredential_settings_or_arr_identity_are_preserved_for_review(
        self,
    ):
        proof = self.capture()
        self.native()
        self.client["priority"] = 11
        with self.assertRaises(ArrClientReconciliationError):
            self.reconcile(proof)
        self.assertEqual(self.client["priority"], 11)
        self.assertEqual(self.puts, [])

    def test_edit_during_candidate_test_is_detected_before_put(self):
        proof = self.capture()
        self.native()
        self.before_put_edit = True
        with self.assertRaises(ArrClientReconciliationError):
            self.reconcile(proof)
        self.assertEqual(self.client["priority"], 11)
        self.assertEqual(self.puts, [])

    def test_wrong_revealed_key_cannot_change_any_saved_credentials(self):
        proof = self.capture()
        self.native()
        with self.assertRaises(ArrClientReconciliationError) as caught:
            self.reconcile(proof, "wrong-native-fixture-key")
        self.assertNotIn(_SOURCE_ALIAS, str(caught.exception))
        self.assertNotIn("wrong-native-fixture-key", str(caught.exception))
        self.assertEqual(self.puts, [])

    def test_saved_readback_test_must_pass_and_retry_uses_same_id(self):
        proof = self.capture()
        self.native()
        self.persist_put = False
        with self.assertRaises(ArrClientReconciliationError):
            self.reconcile(proof)
        self.assertEqual(len(self.puts), 1)
        self.persist_put = True
        self.assertTrue(self.reconcile(proof)[0]["connection_verified"])
        self.assertEqual([row["id"] for row in self.puts], [4, 4])

    def test_sonarr_json_and_radarr_empty_success_contracts_are_supported(self):
        for reply in ({}, None, []):
            with self.subTest(reply=reply):
                self.success = reply
                self.stored_key, self.accepted = _SOURCE_ALIAS, {
                    _SOURCE_ALIAS,
                    _NATIVE_KEY,
                }
                proof = self.capture()
                self.native()
                self.assertTrue(self.reconcile(proof)[0]["connection_verified"])

    def test_partial_success_retry_does_not_rewrite_the_already_repaired_client(self):
        second = ManagedClientTests(methodName="runTest")
        second.setUp()
        self.addCleanup(second.doCleanups)
        second.entry.update(process_name="Sonarr Secondary", port=8991)
        self.config["sonarr"]["instances"]["Secondary"] = second.entry
        consumers = self.consumers + [
            {
                "service_key": "sonarr",
                "instance": "Secondary",
                "process_name": "Sonarr Secondary",
                "config_fingerprint": _fingerprint(second.entry),
            }
        ]
        names = self.names + ["Sonarr Secondary"]

        def request(origin, *args):
            target = self if origin.endswith(":8990") else second
            return target.request(origin, *args)

        proof = capture_arr_bindings(
            self.config,
            self.source,
            consumers,
            names,
            plan_fingerprint=_PLAN,
            request=request,
        )
        self.native()
        second.native()
        second.persist_put = False

        def reconcile():
            return reconcile_arr_bindings(
                self.config,
                self.source,
                consumers,
                names,
                proof,
                plan_fingerprint=_PLAN,
                request=request,
                native_key=lambda service, data_root: _NATIVE_KEY,
            )

        with self.assertRaises(ArrClientReconciliationError) as caught:
            reconcile()
        self.assertEqual(caught.exception.process_name, "Sonarr Secondary")
        self.assertEqual(self.stored_key, _NATIVE_KEY)
        self.assertEqual(len(self.puts), 1)
        second.persist_put = True
        self.assertTrue(all(row["connection_verified"] for row in reconcile()))
        self.assertEqual(len(self.puts), 1)
        self.assertEqual(len(second.puts), 2)

    def test_consumer_recovery_holds_and_retries_without_repeating_native_import(self):
        from utils.infinidysk_v2_consumer_recovery import (
            ConsumerRecoveryError,
            InfiniDyskV2ConsumerRecovery,
        )

        proof = self.capture()
        self.native()
        manager = SimpleNamespace(
            executor=SimpleNamespace(_running=lambda name: True),
            _config=lambda: self.config,
            _write=Mock(),
            _job_path=lambda identifier: self.root / identifier,
        )

        def reconcile(*args, **kwargs):
            return reconcile_arr_bindings(
                *args,
                **kwargs,
                request=self.request,
                native_key=lambda service, data_root: _NATIVE_KEY,
            )

        controller = InfiniDyskV2ConsumerRecovery(
            manager, reconcile_arr_clients=reconcile
        )
        controller._owner = Mock()
        controller._validate = Mock(
            return_value=(self.config["infinidysk"], {}, self.names)
        )
        controller._ready = Mock(return_value=True)
        controller._start_prepared = Mock()
        job = {
            "job_id": "fixture",
            "plan_fingerprint": _PLAN,
            "activation_exposed": True,
            "activation_committed": True,
            "arr_client_bindings": proof,
            "consumer_recovery": {"schema": 1},
            "plan": {
                "mode": "migrate",
                "source": {"service": self.source, "process_name": "InfiniDysk"},
                "inventory": {"consumers": self.consumers},
            },
        }
        self.persist_put = False
        with self.assertRaises(ConsumerRecoveryError):
            controller.resume(job)
        self.assertEqual(
            job["consumer_recovery"]["failed"][0]["reason_code"],
            "arr_client_unverified",
        )
        self.assertTrue(job["activation_exposed"])
        self.persist_put = True
        result = controller.resume(job)
        self.assertEqual(result["failed"], [])
        self.assertTrue(result["arr_clients"][0]["connection_verified"])
        self.assertEqual([row["id"] for row in self.puts], [4, 4])

    def test_prequiesce_failure_never_stops_the_existing_services(self):
        from utils.infinidysk_v2_execution import InfiniDyskV2Executor

        capture = Mock(side_effect=ArrClientReconciliationError("source_unverified"))
        executor = InfiniDyskV2Executor(
            SimpleNamespace(), self.config, capture_arr_clients=capture
        )
        executor._execution_ids = Mock(return_value=(1001, 1001))
        executor._names = Mock(return_value=self.names)
        executor._running = Mock(return_value=True)
        executor._quiesce = Mock(side_effect=AssertionError("must not stop services"))
        plan = {
            "mode": "migrate",
            "source": {"service": self.source},
            "inventory": {"consumers": self.consumers},
        }
        with self.assertRaises(ArrClientReconciliationError):
            executor.verify_arr_clients_before_stop(plan, {"plan_fingerprint": _PLAN})
        executor._quiesce.assert_not_called()


class ArrClientManagerBoundaryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_infinidysk_v2_migration import InfiniDyskV2MigrationTests

        self.fixture = InfiniDyskV2MigrationTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manager = self.fixture.manager
        self.source = SimpleNamespace(pid=54321, poll=lambda: None)
        self.fixture.handler.processes = {
            "InfiniDysk": {"name": "InfiniDysk", "process_obj": self.source}
        }
        self.fixture.handler.process_names = {"InfiniDysk": self.source}
        self.fixture.handler.stop_process = Mock()
        self.fixture.handler.start_process = Mock()
        self.fixture.config.config["infinidysk"]["enabled"] = True
        self.fixture.config.save_config()
        self.fixture.executor.rollback = Mock(
            side_effect=AssertionError("No rollback before execution")
        )

    def test_failed_saved_client_check_never_enters_execution_or_rollback(self):
        before = (self.fixture.source / "db.sqlite").read_bytes()
        self.fixture.executor.verify_arr_clients_before_stop = Mock(
            side_effect=ArrClientReconciliationError("source_unverified")
        )
        _preview, request = self.fixture.preview_request()
        job = self.fixture.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "failed")
        self.assertIn("before applications were paused", job["message"])
        self.assertEqual(self.fixture.executor.calls, [])
        self.fixture.executor.rollback.assert_not_called()
        self.fixture.handler.stop_process.assert_not_called()
        self.fixture.handler.start_process.assert_not_called()
        self.assertIsNone(self.source.poll())
        self.assertEqual((self.fixture.source / "db.sqlite").read_bytes(), before)

    def test_cancel_during_client_baseline_never_enters_execution_or_rollback(self):
        def cancel(_plan, workspace):
            self.manager._cancel_requested.add(self.manager._active_id)
            workspace["check_cancel"]()

        self.fixture.executor.verify_arr_clients_before_stop = Mock(side_effect=cancel)
        _preview, request = self.fixture.preview_request()
        job = self.fixture.wait(self.manager.rehearse(request))
        self.assertEqual(job["status"], "cancelled")
        self.assertIn("before applications were paused", job["message"])
        self.assertEqual(self.fixture.executor.calls, [])
        self.fixture.executor.rollback.assert_not_called()
        self.fixture.handler.stop_process.assert_not_called()
        self.fixture.handler.start_process.assert_not_called()
        self.assertIsNone(self.source.poll())

    def test_new_v2_environment_removes_legacy_alias_without_changing_source(self):
        plan = self.manager._plan({"mode": "migrate", "mount_backend": "rclone"})
        self.assertIn("FRONTEND_BACKEND_API_KEY", plan["source"]["service"]["env"])
        self.assertNotIn("FRONTEND_BACKEND_API_KEY", plan["target"]["service"]["env"])
        self.assertIn(
            "FRONTEND_BACKEND_API_KEY", self.fixture.config.get("infinidysk")["env"]
        )


if __name__ == "__main__":
    unittest.main()
