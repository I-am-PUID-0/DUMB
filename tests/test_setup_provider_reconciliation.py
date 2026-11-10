"""Regression coverage for first-run provider edges without live applications."""

import copy
import json
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from utils import altmount_settings as alt
from utils import decypharr_settings as dec
from utils import integration_result as results
from utils import nzbdav_settings as nzb
from utils import riven_settings as riven
from utils.integration_ownership import fingerprint, read_ownership, write_ownership


class IntegrationResultTests(unittest.TestCase):
    def test_nested_collectors_keep_independent_results_and_legacy_changed_boolean(
        self,
    ):
        outer, inner = [], []
        unchanged = results.ActionResult(
            "a", "radarr", "root", execution="already_present"
        )
        applied = results.ActionResult("b", "radarr", "root", changed=True)
        with results.integration_results(outer):
            results.emit_action(unchanged)
            with results.integration_results(inner):
                results.emit_action(applied)
            results.emit_action(unchanged)
        self.assertEqual(outer, [unchanged, unchanged])
        self.assertEqual(inner, [applied])
        self.assertFalse(unchanged)
        self.assertTrue(applied)

    def test_retry_callback_can_reschedule_and_stops_at_budget(self):
        retry = results.BoundedIntegrationRetry(max_attempts=3, max_delay_s=30)
        pending, calls, delays = [], [], []

        def thread_factory(*, target, daemon):
            self.assertTrue(daemon)
            return types.SimpleNamespace(start=lambda: pending.append(target))

        def callback():
            calls.append(True)
            retry.schedule(callback, lambda: False, delay_s=10)

        with (
            mock.patch.object(results.threading, "Thread", side_effect=thread_factory),
            mock.patch.object(results.time, "sleep", side_effect=delays.append),
        ):
            self.assertTrue(retry.schedule(callback, lambda: False, delay_s=10))
            self.assertFalse(retry.schedule(callback, lambda: False))
            while pending:
                pending.pop(0)()
            self.assertEqual(len(calls), 3)
            self.assertEqual(delays, [10, 20, 30])
            self.assertIsNone(retry.next_attempt_at)
            self.assertFalse(retry.schedule(callback, lambda: False))
            retry.reset()
            self.assertTrue(retry.schedule(callback, lambda: False))
            # Do not leave a fake pending callback for a following test.
            pending.clear()

    def test_shutdown_before_runner_suppresses_callback(self):
        retry = results.BoundedIntegrationRetry()
        pending = []
        stopped = [False]
        callback = mock.Mock()
        with (
            mock.patch.object(
                results.threading,
                "Thread",
                side_effect=lambda **kw: types.SimpleNamespace(
                    start=lambda: pending.append(kw["target"])
                ),
            ),
            mock.patch.object(results.time, "sleep"),
        ):
            self.assertTrue(retry.schedule(callback, lambda: stopped[0]))
            stopped[0] = True
            pending.pop()()
        callback.assert_not_called()
        self.assertFalse(retry.schedule(callback, lambda: True))

    def test_thread_start_failure_does_not_leave_permanent_pending_flag(self):
        retry = results.BoundedIntegrationRetry()
        with mock.patch.object(
            results.threading.Thread, "start", side_effect=RuntimeError("fixture")
        ):
            with self.assertRaises(RuntimeError):
                retry.schedule(lambda: None, lambda: False)
        self.assertFalse(retry._pending)
        self.assertIsNone(retry.next_attempt_at)

    def test_capture_forwards_outer_collector_and_keeps_complete_attempt(self):
        outer = []
        good = results.ActionResult(
            "ok", "decypharr", "root", execution="already_present"
        )
        pending = results.ActionResult(
            "wait", "decypharr", "client", execution="waiting_dependency"
        )
        with results.integration_results(outer):
            with results.capture_integration_results() as captured:
                results.emit_action(good)
                results.emit_action(pending)
        self.assertEqual(captured, [good, pending])
        self.assertEqual(outer, captured)

    def test_retry_budget_renews_only_after_whole_typed_reconciliation_succeeds(self):
        retry = mock.Mock()
        good = results.ActionResult(
            "ok", "decypharr", "root", execution="already_present"
        )
        pending = results.ActionResult(
            "wait", "decypharr", "client", execution="waiting_dependency"
        )
        for legacy, outcomes in (
            ((False, None), []),
            ((False, "error"), [good]),
            ((True, None), [good, pending]),
        ):
            results.reset_retry_after_success(retry, legacy, outcomes)
        retry.reset.assert_not_called()
        results.reset_retry_after_success(retry, (False, None), [good])
        retry.reset.assert_called_once_with()

    def test_provider_wrappers_forward_results_and_reset_only_after_success(self):
        for module, method_name, retry_name in (
            (dec, "patch_decypharr_config", "_DECYPHARR_RETRY"),
            (nzb, "patch_nzbdav_config", "_NZBDAV_RETRY"),
            (alt, "patch_altmount_arr_integration", "_ALT_RETRY"),
        ):
            with self.subTest(module=module.__name__):

                def attempt():
                    results.emit_action(
                        results.ActionResult(
                            "fixture", "provider", "client", execution="already_present"
                        )
                    )
                    return False, None

                outcomes = []
                with (
                    mock.patch.object(
                        module, "_" + method_name, side_effect=lambda **kw: attempt()
                    ),
                    mock.patch.object(module, retry_name) as retry,
                ):
                    self.assertEqual(
                        getattr(module, method_name)(result_collector=outcomes),
                        (False, None),
                    )
                    retry.reset.assert_called_once_with()
                self.assertEqual(len(outcomes), 1)

    def test_auth_errors_are_not_retryable_and_never_expose_exception_detail(self):
        error = urllib.error.HTTPError(
            "https://example.invalid/private-token",
            401,
            "password=fixture-secret",
            {},
            None,
        )
        outcome = results.request_failure(error)
        self.assertEqual(outcome["execution"], "waiting_input")
        self.assertFalse(outcome["retryable"])
        self.assertNotIn("fixture-secret", json.dumps(outcome))
        self.assertNotIn("private-token", json.dumps(outcome))


class ProviderEdgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.xml = self.root / "config.xml"
        self.xml.write_text(
            "<Config><ApiKey>arr-fixture</ApiKey><UrlBase>/radarr</UrlBase></Config>"
        )
        self.instance = {
            "enabled": True,
            "port": 7878,
            "config_file": str(self.xml),
            "core_service": "decypharr",
        }
        self.config = {"radarr": {"instances": {"Movies": self.instance}}}
        self.manager = types.SimpleNamespace(
            get=lambda key, default=None: self.config.get(key, default)
        )
        for module in (dec, nzb, alt):
            self.enterContext(mock.patch.object(module, "CONFIG_MANAGER", self.manager))
            self.enterContext(
                mock.patch.object(module, "_shutdown_requested", return_value=False)
            )
        self.enterContext(mock.patch.object(dec, "_schedule_decypharr_retry"))
        self.enterContext(mock.patch.object(nzb, "_schedule_nzbdav_retry"))
        self.enterContext(mock.patch.object(alt, "_schedule_altmount_retry"))

    def test_missing_media_root_creation_is_a_required_failed_outcome(self):
        entry = {
            "Host": "http://127.0.0.1:7878",
            "ApiKey": "private-fixture",
            "Instance": "Movies",
            "Category": "radarr-movies",
        }
        observed = []
        with (
            mock.patch.object(
                nzb, "_collect_arr_entries", return_value=([entry], [], [], [])
            ),
            mock.patch.object(
                nzb,
                "_ensure_symlink_roots",
                side_effect=PermissionError("private filesystem detail"),
            ),
            mock.patch.object(
                nzb,
                "_read_nzbdav_config_values",
                return_value=(None, "Application not ready"),
            ),
            results.integration_results(observed),
        ):
            nzb._patch_nzbdav_config_admitted()
        required = [
            item for item in observed if item.action == "media_directory_ownership"
        ]
        self.assertEqual(len(required), 1)
        self.assertEqual(required[0].execution, "failed")
        self.assertTrue(required[0].retryable)
        self.assertNotIn("private filesystem detail", required[0].detail)

    def test_readonly_category_helpers_match_each_native_provider_recipe(self):
        self.instance["instance_name"] = "Movies 4K"
        self.assertEqual(
            dec.expected_arr_category(self.config, "radarr", "Movies"),
            "radarr:Movies 4K",
        )
        self.assertEqual(
            alt.expected_arr_category(self.config, "radarr", "Movies"),
            "radarr:Movies 4K",
        )
        self.assertEqual(
            nzb.expected_arr_category(self.config, "radarr", "Movies"),
            "radarr-movies-4k",
        )

    def test_discovered_arr_urls_include_native_base_path(self):
        self.enterContext(mock.patch.object(dec, "has_core_service", return_value=True))
        self.enterContext(mock.patch.object(nzb, "has_core_service", return_value=True))
        self.enterContext(mock.patch.object(alt, "has_core_service", return_value=True))
        self.assertEqual(
            dec._collect_arr_entries({})[0]["host"], "http://127.0.0.1:7878/radarr"
        )
        self.assertEqual(
            nzb._collect_arr_entries()[0][0]["Host"], "http://127.0.0.1:7878/radarr"
        )
        self.assertEqual(
            alt._collect_arr_entries()[0]["host"], "http://127.0.0.1:7878/radarr"
        )

    def test_root_folder_unchanged_is_distinct_from_failure(self):
        for module in (dec, nzb):
            with (
                self.subTest(module=module.__name__),
                mock.patch.object(
                    module, "_arr_req", return_value=[{"path": "/library/movies"}]
                ) as request,
            ):
                outcome = module._ensure_arr_rootfolder(
                    "http://127.0.0.1:7878", "fixture", "/library/movies"
                )
                self.assertFalse(outcome)
                self.assertEqual(outcome.execution, "already_present")
                self.assertEqual(outcome.verification, "passed")
                request.assert_called_once()

    def test_root_folder_write_requires_readback(self):
        for module in (dec, nzb):
            with (
                self.subTest(module=module.__name__),
                mock.patch.object(module, "_arr_req", side_effect=[[], {"id": 1}, []]),
            ):
                outcome = module._ensure_arr_rootfolder(
                    "http://127.0.0.1:7878", "fixture", "/library/movies"
                )
                self.assertEqual(outcome.execution, "failed")
                self.assertEqual(outcome.verification, "failed")
                self.assertTrue(outcome.retryable)

    def test_lidarr_missing_profiles_requires_input_without_retrying_blindly(self):
        with (
            mock.patch.object(dec, "_arr_req", return_value=[]),
            mock.patch.object(dec, "_get_lidarr_rootfolder_payload", return_value=None),
        ):
            outcome = dec._ensure_arr_rootfolder(
                "http://127.0.0.1:7878", "fixture", "/music", api_version="v1"
            )
        self.assertEqual(outcome.execution, "waiting_input")
        self.assertFalse(outcome.retryable)

    def test_permissions_verify_chmod_fields_and_preserve_unrelated_settings(self):
        current = {
            "setPermissions": False,
            "chmodFolder": "755",
            "chmodFile": "644",
            "recycleBin": "/user/bin",
        }
        unchanged = {
            **current,
            "setPermissions": True,
            "chmodFolder": "777",
            "chmodFile": "666",
        }
        for module in (dec, nzb):
            with (
                self.subTest(module=module.__name__),
                mock.patch.object(
                    module,
                    "_arr_req",
                    side_effect=[current, {}, {**unchanged, "chmodFolder": "755"}],
                ) as request,
            ):
                outcome = module._ensure_arr_permissions(
                    "http://127.0.0.1:7878", "fixture"
                )
                self.assertEqual(
                    request.call_args_list[1].args[3]["recycleBin"], "/user/bin"
                )
                self.assertEqual(outcome.execution, "failed")
                self.assertEqual(outcome.verification, "failed")

    def test_retry_handles_typed_transient_failure_but_not_successful_noop(self):
        failed = results.ActionResult(
            "edge", "decypharr", "root", execution="failed", retryable=True
        )
        unchanged = results.ActionResult(
            "edge", "decypharr", "root", execution="already_present"
        )
        for module in (dec, nzb):
            collected = []
            fn = mock.Mock(side_effect=[failed, unchanged])
            with (
                self.subTest(module=module.__name__),
                results.integration_results(collected),
                mock.patch.object(module.time, "sleep"),
            ):
                outcome = module._with_retries(fn)
            self.assertEqual(outcome.execution, "already_present")
            self.assertEqual(fn.call_count, 2)
            self.assertEqual(collected, [unchanged])

    def test_permanent_typed_failure_is_not_retried(self):
        failed = results.ActionResult(
            "edge", "decypharr", "root", execution="waiting_input"
        )
        fn = mock.Mock(return_value=failed)
        with results.integration_results([]):
            self.assertEqual(dec._with_retries(fn), failed)
        fn.assert_called_once()

    def test_auth_rejection_stops_readiness_poll_without_sleep_or_retry(self):
        error = urllib.error.HTTPError("http://127.0.0.1:7878", 401, "secret", {}, None)
        observed = []
        with (
            mock.patch.object(dec, "_arr_req", side_effect=error),
            mock.patch.object(dec.time, "sleep") as sleep,
            results.integration_results(observed),
        ):
            self.assertFalse(dec._wait_for_arr("http://127.0.0.1:7878", "fixture"))
        sleep.assert_not_called()
        self.assertEqual(observed[-1].execution, "waiting_input")
        dec._schedule_decypharr_retry.assert_not_called()

    def test_decypharr_preserves_foreign_registrations_and_retires_only_auto(self):
        custom = {
            "name": "radarr:Foreign",
            "host": "https://example.invalid",
            "token": "user",
            "extra": True,
        }
        managed = {
            "name": "radarr:Old",
            "host": "http://127.0.0.1:7999",
            "source": "auto",
        }
        self.assertEqual(dec._merge_decypharr_arrs([custom, managed], []), [custom])
        desired = [
            {
                "name": custom["name"],
                "host": "http://127.0.0.1:7878",
                "token": "new",
                "source": "auto",
            }
        ]
        self.assertEqual(dec._merge_decypharr_arrs([custom], desired), [custom])

    def test_infinidysk_preserves_user_fields_and_retires_owned_entries_only(self):
        foreign = {
            "Host": "http://127.0.0.1:7879",
            "ApiKey": "user",
            "QueueRules": ["keep"],
        }
        desired = {
            "Host": "http://127.0.0.1:7878",
            "ApiKey": "first",
            "Category": "movies",
            "Instance": "Movies",
        }
        owned = {}
        merged = nzb._merge_instances([foreign], [desired], owned)
        merged[1]["UserPolicy"] = True
        updated = nzb._merge_instances(
            merged, [{**desired, "ApiKey": "rotated"}], owned
        )
        self.assertEqual(updated[0], foreign)
        self.assertTrue(updated[1]["UserPolicy"])
        self.assertEqual(nzb._merge_instances(updated, [], owned), [foreign])

    def test_altmount_does_not_prune_unowned_loopback_or_reenable_owned_entries(self):
        foreign = {
            "name": "radarr:Foreign",
            "url": "http://127.0.0.1:7999",
            "enabled": True,
        }
        entries = [
            {
                "service": "radarr",
                "name": "radarr:Movies",
                "host": "http://127.0.0.1:7878",
                "token": "key",
            }
        ]
        instances, owned = [foreign], {}
        self.assertTrue(alt._sync_arr_instances(instances, "radarr", entries, owned))
        instances[1]["enabled"] = False
        self.assertFalse(alt._sync_arr_instances(instances, "radarr", entries, owned))
        self.assertFalse(instances[1]["enabled"])
        self.assertTrue(alt._sync_arr_instances(instances, "radarr", [], owned))
        self.assertEqual(instances, [foreign])

    def _download_client_fixture(self):
        clients = []
        requests = []
        desired = {
            "name": "decypharr",
            "enable": True,
            "implementation": "QBittorrent",
            "protocol": "torrent",
            "priority": 1,
            "tags": [],
            "fields": [
                {"name": "host", "value": "127.0.0.1"},
                {"name": "port", "value": 8282},
                {"name": "password", "value": "private-fixture"},
            ],
        }

        def request(url, key, method="GET", data=None):
            self.assertNotIn("/tag", url)
            requests.append((method, url, copy.deepcopy(data)))
            if url.endswith("/test"):
                return []
            if method == "GET":
                return copy.deepcopy(
                    clients if url.endswith("/downloadclient") else clients[0]
                )
            if method == "POST":
                item = {**copy.deepcopy(data), "id": 1}
                clients.append(item)
                return copy.deepcopy(item)
            clients[0] = copy.deepcopy(data)
            return copy.deepcopy(data)

        def apply(**kwargs):
            return dec._upsert_managed_download_client(
                "http://127.0.0.1:7878",
                "arr-key",
                desired,
                provider="decypharr",
                managed_fields={"host", "port", "password"},
                request_fn=request,
                **kwargs,
            )

        return clients, requests, desired, apply

    def test_download_client_sidecar_ownership_does_not_change_routing_tags(self):
        clients, requests, desired, apply = self._download_client_fixture()
        observed = []
        with results.integration_results(observed):
            self.assertEqual(apply(), (True, 1))
        path = self.root / ".dumb-integrations.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("private-fixture", path.read_text())
        self.assertEqual(clients[0]["tags"], [])
        clients[0].update(tags=[42], priority=20, removeCompletedDownloads=False)
        desired["fields"][2]["value"] = "rotated-fixture"
        self.assertEqual(apply(), (True, 1))
        self.assertEqual(clients[0]["tags"], [42])
        self.assertEqual(clients[0]["priority"], 20)
        self.assertFalse(clients[0]["removeCompletedDownloads"])
        before = len([item for item in requests if item[0] == "PUT"])
        self.assertEqual(apply(), (False, 1))
        self.assertEqual(len([item for item in requests if item[0] == "PUT"]), before)
        self.assertEqual(observed[-1].verification, "passed")

    def test_download_client_preserves_unowned_and_user_edited_connections(self):
        clients, requests, desired, apply = self._download_client_fixture()
        clients.append({**copy.deepcopy(desired), "id": 1})
        with self.assertRaises(dec.ManagedIntegrationConflict):
            apply()
        self.assertFalse(any(item[0] == "PUT" for item in requests))
        clients.clear()
        apply()
        clients[0]["fields"][0]["value"] = "custom.example.invalid"
        before = copy.deepcopy(clients)
        with self.assertRaises(dec.ManagedIntegrationConflict):
            apply()
        self.assertEqual(clients, before)

    def test_download_client_preserves_deliberate_disable(self):
        clients, requests, desired, apply = self._download_client_fixture()
        apply()
        clients[0]["enable"] = False
        observed = []
        with results.integration_results(observed):
            self.assertEqual(apply(), (False, 1))
        self.assertFalse(clients[0]["enable"])
        self.assertEqual(observed[-1].execution, "waiting_input")

    def test_download_client_connection_test_has_separate_verification(self):
        _clients, requests, _desired, apply = self._download_client_fixture()
        observed = []
        with results.integration_results(observed):
            apply(test_connection=True)
        self.assertTrue(requests[-1][1].endswith("/test"))
        self.assertEqual(observed[-1].action, "download_client_torrent_connection")
        self.assertEqual(observed[-1].verification, "passed")

    def test_private_ownership_rejects_symlinks_without_changing_target(self):
        original = self.root / "original"
        original.write_text('{"preserve": true}')
        link = self.root / "linked"
        link.symlink_to(original)
        with self.assertRaises(ValueError):
            read_ownership(link)
        with self.assertRaises(ValueError):
            write_ownership(link, {})
        self.assertEqual(original.read_text(), '{"preserve": true}')

    def test_selected_target_scope_blocks_other_outbound_arr_calls(self):
        other = {
            "enabled": True,
            "port": 7880,
            "config_file": str(self.root / "other.xml"),
            "core_service": "decypharr",
        }
        self.config["radarr"]["instances"]["Other"] = other
        with results.integration_targets({"radarr.Movies"}):
            self.assertTrue(
                results.arr_target_allowed("http://127.0.0.1:7878/radarr", self.manager)
            )
            self.assertFalse(
                results.arr_target_allowed("http://127.0.0.1:7880", self.manager)
            )
            for module in (dec, nzb):
                with mock.patch.object(module, "_arr_req") as request:
                    self.assertEqual(
                        module._ensure_arr_rootfolder(
                            "http://127.0.0.1:7880", "key", "/library/other"
                        ).execution,
                        "skipped",
                    )
                    self.assertEqual(
                        module._ensure_arr_permissions(
                            "http://127.0.0.1:7880", "key"
                        ).execution,
                        "skipped",
                    )
                    self.assertFalse(
                        module._wait_for_arr("http://127.0.0.1:7880", "key")
                    )
                    request.assert_not_called()
            request = mock.Mock()
            self.assertEqual(
                dec._upsert_managed_download_client(
                    "http://127.0.0.1:7880",
                    "key",
                    {},
                    provider="decypharr",
                    managed_fields=set(),
                    request_fn=request,
                ),
                (False, None),
            )
            request.assert_not_called()
        self.assertTrue(
            results.arr_target_allowed("http://127.0.0.1:7880", self.manager)
        )

    def test_target_scope_does_not_shrink_native_provider_arr_registrations(self):
        self.config["radarr"]["instances"]["Other"] = {
            "enabled": True,
            "port": 7880,
            "config_file": str(self.xml),
            "core_service": "decypharr",
        }
        with (
            mock.patch.object(dec, "has_core_service", return_value=True),
            results.integration_targets({"radarr.Movies"}),
        ):
            entries = dec._collect_arr_entries({})
            self.assertEqual(len(entries), 2)
            merged = dec._merge_decypharr_arrs([], entries)
            self.assertEqual(len(merged), 2)

    def test_missing_generated_arr_key_does_not_erase_existing_provider_credentials(
        self,
    ):
        existing = {
            "name": "radarr:Movies",
            "host": "http://127.0.0.1:7878",
            "token": "preserve",
            "source": "auto",
        }
        desired = {**existing, "token": ""}
        self.assertEqual(dec._merge_decypharr_arrs([existing], [desired]), [existing])
        self.assertEqual(dec._merge_decypharr_arrs([], [desired]), [])
        alt_entry = {
            "service": "radarr",
            "name": "radarr:Movies",
            "host": "http://127.0.0.1:7878",
            "token": "first",
        }
        instances, owned = [], {}
        alt._sync_arr_instances(instances, "radarr", [alt_entry], owned)
        before = copy.deepcopy(instances)
        self.assertFalse(
            alt._sync_arr_instances(
                instances, "radarr", [{**alt_entry, "token": ""}], owned
            )
        )
        self.assertEqual(instances, before)
        nzb_entry = {
            "Host": "http://127.0.0.1:7878",
            "ApiKey": "first",
            "Instance": "Movies",
            "Category": "movies",
        }
        owned = {}
        registered = nzb._merge_instances([], [nzb_entry], owned)
        self.assertEqual(
            nzb._merge_instances(
                registered, [], owned, retain_hosts={"http://127.0.0.1:7878"}
            ),
            registered,
        )
        self.assertEqual(nzb._merge_instances(registered, [], owned), [])

    def test_missing_arr_key_is_readiness_not_rejected_credentials(self):
        observed = []
        with (
            results.integration_results(observed),
            mock.patch.object(dec, "_arr_req") as request,
        ):
            self.assertFalse(dec._wait_for_arr("http://127.0.0.1:7878", ""))
        request.assert_not_called()
        self.assertEqual(observed[-1].execution, "waiting_dependency")
        dec._schedule_decypharr_retry.assert_called_once()

    def test_user_ownership_conflict_is_not_retried_as_a_transient_error(self):
        function = mock.Mock(
            side_effect=dec.ManagedIntegrationConflict("safe decision needed")
        )
        with (
            mock.patch.object(dec.time, "sleep") as sleep,
            self.assertRaises(dec.ManagedIntegrationConflict),
        ):
            dec._with_retries(function)
        function.assert_called_once()
        sleep.assert_not_called()

    def test_read_only_root_recipe_matches_shared_and_legacy_namespace(self):
        self.instance.update(core_services=["decypharr", "infinidysk"])
        links = lambda instance: instance.get("core_services") or [
            instance.get("core_service")
        ]
        with (
            mock.patch.object(dec, "get_core_services", side_effect=links),
            mock.patch.object(nzb, "get_core_services", side_effect=links),
        ):
            self.assertEqual(
                dec.expected_arr_root(self.config, "radarr", "Movies"),
                "/mnt/debrid/combined_symlinks/radarr-movies",
            )
            self.assertEqual(
                nzb.expected_arr_root(self.config, "radarr", "Movies"),
                "/mnt/debrid/combined_symlinks/radarr-movies",
            )
            self.instance["core_services"] = ["infinidysk"]
            self.config["infinidysk"] = {"config_dir": "/nzbdav"}
            self.assertEqual(
                nzb.expected_arr_root(self.config, "radarr", "Movies"),
                "/mnt/debrid/nzbdav-symlinks/radarr-movies",
            )


class RivenDirectInputTests(unittest.TestCase):
    def test_source_mode_uses_direct_provider_input_without_legacy_mounts(self):
        for selector in ({"branch_enabled": True}, {"commit_sha": "a" * 40}):
            config = {
                "riven_backend": {
                    **selector,
                    "api_keys": {"Real-Debrid": "direct-fixture"},
                }
            }
            values = riven.parse_config_keys(config)
            self.assertEqual(
                values["DOWNLOADERS_REAL_DEBRID_API_KEY"], "direct-fixture"
            )
            self.assertIsNone(config["riven_backend"]["wait_for_dir"])
            self.assertNotIn("rclone", config)

    def test_explicit_provider_input_takes_precedence_over_legacy_mount_token(self):
        config = {
            "riven_backend": {"api_keys": {"realdebrid": "direct"}},
            "rclone": {
                "instances": {
                    "riven": {
                        "enabled": True,
                        "core_service": "riven_backend",
                        "key_type": "realdebrid",
                        "api_key": "legacy",
                        "mount_dir": "/mnt",
                        "mount_name": "riven",
                    }
                }
            },
        }
        with mock.patch.object(riven, "has_core_service", return_value=True):
            values = riven.parse_config_keys(config)
        self.assertEqual(values["DOWNLOADERS_REAL_DEBRID_API_KEY"], "direct")
        self.assertEqual(config["riven_backend"]["wait_for_dir"], "/mnt/riven/__all__")

    def test_managed_plex_token_is_used_only_for_managed_plex_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            preferences = Path(tmp) / "Preferences.xml"
            preferences.write_text('<Preferences PlexOnlineToken="managed-fixture" />')
            config = {
                "plex": {
                    "enabled": True,
                    "port": 32401,
                    "config_file": str(preferences),
                },
                "dumb": {},
            }
            self.assertEqual(
                riven.resolve_plex_connection(config),
                ("http://127.0.0.1:32401", "managed-fixture"),
            )
            config["dumb"] = {
                "plex_address": "https://plex.example.invalid",
                "plex_token": "external-fixture",
            }
            self.assertEqual(
                riven.resolve_plex_connection(config),
                ("https://plex.example.invalid", "external-fixture"),
            )
            config["dumb"].pop("plex_token")
            self.assertEqual(
                riven.resolve_plex_connection(config),
                ("https://plex.example.invalid", ""),
            )

    def test_frontend_server_config_is_written_privately_and_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            riven.save_server_config("http://127.0.0.1:8080", "private-fixture", tmp)
            target = Path(tmp) / "server.json"
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                json.loads(target.read_text())["apiKey"], "private-fixture"
            )
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


class MediaDirectoryOwnershipTests(unittest.TestCase):
    def test_existing_foreign_root_and_contents_are_never_chowned_or_chmodded(self):
        from utils.integration_ownership import ensure_media_directory
        import os

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "media"
            root.mkdir(mode=0o750)
            child = root / "existing.media"
            child.write_text("preserve")
            child.chmod(0o640)
            before = (
                root.stat().st_uid,
                root.stat().st_gid,
                root.stat().st_mode,
                child.stat().st_mode,
            )
            with mock.patch("utils.integration_ownership.os.chown") as chown:
                result = ensure_media_directory(root, os.getuid() + 1, os.getgid() + 1)
            chown.assert_not_called()
            self.assertTrue(result["ownership_mismatch"])
            self.assertFalse(result["created"])
            self.assertEqual(
                (
                    root.stat().st_uid,
                    root.stat().st_gid,
                    root.stat().st_mode,
                    child.stat().st_mode,
                ),
                before,
            )

    def test_new_directories_keep_disclosed_recipe_without_touching_existing_parent(
        self,
    ):
        from utils.integration_ownership import ensure_media_directory
        import os

        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp)
            old_mode = parent.stat().st_mode
            target = parent / "new" / "movies"
            result = ensure_media_directory(target, os.getuid(), os.getgid())
            self.assertTrue(result["created"])
            self.assertFalse(result["ownership_mismatch"])
            self.assertEqual(target.stat().st_mode & 0o777, 0o777)
            self.assertEqual(target.parent.stat().st_mode & 0o777, 0o777)
            self.assertEqual(parent.stat().st_mode, old_mode)

    def test_existing_symlink_root_is_preserved_without_traversal(self):
        from utils.integration_ownership import ensure_media_directory

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "target"
            target.mkdir()
            link = root / "link"
            link.symlink_to(target, target_is_directory=True)
            result = ensure_media_directory(link / "uncreated")
            self.assertTrue(result["ownership_mismatch"])
            self.assertFalse((target / "uncreated").exists())
