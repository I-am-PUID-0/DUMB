"""Connection reconciliation exercises saved files and stateful fake upstream APIs."""

import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import urllib.error

from utils import neutarr_settings as neutarr
from utils import prowlarr_settings as prowlarr
from utils import profilarr_settings as profilarr


class Config:
    def __init__(self, values):
        self.config = values

    def get(self, key, default=None):
        return self.config.get(key, default)


class NeutArrReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = Config(
            {
                "puid": os.getuid(),
                "pgid": os.getgid(),
                "neutarr": {
                    "instances": {
                        "automation": {
                            "enabled": True,
                            "env": {"NEUTARR_CONFIG_DIR": str(self.root)},
                        }
                    }
                },
                "radarr": {
                    "instances": {"movies": self.arr(7878), "uhd": self.arr(7879)}
                },
            }
        )
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(neutarr, "CONFIG_MANAGER", self.config).start()
        mock.patch.object(
            neutarr, "_OWNERSHIP_DIRECTORY", str(self.root / "ownership")
        ).start()
        mock.patch.object(
            neutarr, "_parse_arr_api_key", return_value="fixture-key"
        ).start()

    def arr(self, port):
        return {"enabled": True, "use_neutarr": True, "port": port}

    def read(self):
        return json.loads((self.root / "radarr.json").read_text())

    def test_first_pass_consumes_default_once_and_second_apply_is_idempotent(self):
        (self.root / "radarr.json").write_text(
            json.dumps(
                {
                    "keep": "policy",
                    "instances": [
                        {
                            "name": "Default",
                            "enabled": True,
                            "api_url": "",
                            "api_key": "",
                            "search_interval": 12,
                        }
                    ],
                }
            )
        )
        results = []
        self.assertEqual(neutarr.patch_neutarr_config(results), (True, None))
        first = self.read()
        self.assertEqual(
            [item["name"] for item in first["instances"]], ["movies", "uhd"]
        )
        self.assertEqual(first["instances"][0]["search_interval"], 12)
        self.assertEqual(len({item["_dumb_id"] for item in first["instances"]}), 2)
        self.assertEqual((self.root / "radarr.json").stat().st_mode & 0o777, 0o600)
        results.clear()
        self.assertEqual(neutarr.patch_neutarr_config(results), (True, None))
        self.assertEqual(self.read(), first)
        self.assertTrue(all(item.execution == "waiting_input" for item in results))

    def test_opt_out_retires_only_owned_connections_and_keeps_user_disable(self):
        neutarr.patch_neutarr_config()
        current = self.read()
        current["instances"][0]["enabled"] = False
        current["instances"][1]["enabled"] = True
        current["instances"].append(
            {"name": "personal", "enabled": True, "api_url": "https://example.invalid"}
        )
        (self.root / "radarr.json").write_text(json.dumps(current))
        self.config.config["radarr"]["instances"]["uhd"]["use_neutarr"] = False
        results = []
        neutarr.patch_neutarr_config(results)
        saved = {item["name"]: item for item in self.read()["instances"]}
        self.assertFalse(saved["movies"]["enabled"])
        self.assertFalse(saved["uhd"]["enabled"])
        self.assertTrue(saved["uhd"]["_dumb_retired"])
        self.assertTrue(saved["personal"]["enabled"])
        self.assertIn("waiting_input", [item.execution for item in results])

    def test_native_ui_save_drops_metadata_but_protected_ownership_survives(self):
        neutarr.patch_neutarr_config()
        current = self.read()
        # Equivalent to NeutArr SettingsForms.getFormSettings instance serializer.
        current["instances"] = [
            {key: item[key] for key in ("name", "api_url", "api_key", "enabled")}
            for item in current["instances"]
        ]
        current["instances"][0]["enabled"] = False
        (self.root / "radarr.json").write_text(json.dumps(current))
        self.config.config["radarr"]["instances"]["uhd"]["use_neutarr"] = False
        self.assertTrue(neutarr.patch_neutarr_config()[0])
        saved = {row["name"]: row for row in self.read()["instances"]}
        self.assertFalse(saved["movies"]["enabled"])
        self.assertFalse(saved["uhd"]["enabled"])
        self.assertEqual(saved["movies"]["_dumb_id"], "radarr.movies")
        self.assertTrue(saved["uhd"]["_dumb_retired"])

    def test_unowned_name_collision_is_not_adopted(self):
        entry = {
            "name": "movies",
            "enabled": False,
            "api_url": "https://example.invalid",
            "api_key": "user-fixture",
        }
        (self.root / "radarr.json").write_text(json.dumps({"instances": [entry]}))
        results = []
        neutarr.patch_neutarr_config(results)
        self.assertEqual(self.read()["instances"][0], entry)
        self.assertEqual(results[0].execution, "waiting_input")

    def test_malformed_existing_file_and_atomic_write_failure_are_reported(self):
        target = self.root / "radarr.json"
        target.write_text("{broken")
        self.assertFalse(neutarr.patch_neutarr_config()[0])
        self.assertEqual(target.read_text(), "{broken")
        target.write_text('{"instances": []}')
        before = target.read_bytes()
        with mock.patch.object(
            neutarr.os, "replace", side_effect=OSError("fixture failure")
        ):
            results = []
            self.assertFalse(neutarr.patch_neutarr_config(results)[0])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.root.glob(".radarr.*")), [])
        self.assertEqual(results[-1].execution, "failed")

    def test_native_url_base_and_missing_key_are_not_false_success(self):
        xml = self.root / "arr.xml"
        xml.write_text("<Config><UrlBase>/native/</UrlBase></Config>")
        self.config.config["radarr"]["instances"]["movies"]["config_file"] = str(xml)
        self.assertEqual(
            neutarr._collect_arr_instances()["radarr"][0]["url"],
            "http://127.0.0.1:7878/native",
        )
        with mock.patch.object(neutarr, "_parse_arr_api_key", return_value=""):
            results = []
            neutarr.patch_neutarr_config(results)
        self.assertTrue(all(item.execution == "waiting_dependency" for item in results))
        self.assertFalse((self.root / "radarr.json").exists())


APP_SCHEMA = {
    "implementation": "Radarr",
    "implementationName": "Radarr",
    "configContract": "RadarrSettings",
    "fields": [
        {"name": "baseUrl", "value": ""},
        {"name": "apiKey", "value": ""},
        {"name": "prowlarrUrl", "value": ""},
        {"name": "syncCategories", "value": [2000]},
    ],
}


class FakeProwlarr:
    def __init__(self):
        self.apps = {}
        self.tags = []
        self.calls = []
        self.reject_readback = False

    def request(self, url, token, method="GET", data=None, **kwargs):
        self.calls.append((url, method, copy.deepcopy(data)))
        resource = url.split("/api/v1/", 1)[-1]
        if resource == "tag":
            if method == "POST":
                tag = {"id": len(self.tags) + 1, "label": data["label"]}
                self.tags.append(tag)
                return copy.deepcopy(tag)
            return copy.deepcopy(self.tags)
        if resource == "applications":
            if method == "POST":
                app = copy.deepcopy(data)
                app["id"] = max(self.apps, default=0) + 1
                self.apps[app["id"]] = app
                return copy.deepcopy(app)
            return copy.deepcopy(list(self.apps.values()))
        if resource.startswith("applications/"):
            identifier = int(resource.rsplit("/", 1)[-1])
            if method == "PUT":
                self.apps[identifier] = copy.deepcopy(data)
            if self.reject_readback and method == "GET":
                return {}
            return copy.deepcopy(self.apps[identifier])
        raise AssertionError((url, method))


class ProwlarrReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.instance = {
            "enabled": True,
            "port": 9696,
            "config_dir": str(self.root),
            "arr_instances": ["radarr.movies"],
        }
        self.config = Config({"prowlarr": {"instances": {"indexers": self.instance}}})
        self.entries = [
            {
                "service": "radarr",
                "instance": "movies",
                "host": "http://127.0.0.1:7878/base",
                "api_key": "fixture-movies",
                "core_services": ["altmount"],
            },
            {
                "service": "radarr",
                "instance": "uhd",
                "host": "http://127.0.0.1:7879",
                "api_key": "fixture-uhd",
                "core_services": ["infinidysk"],
            },
        ]
        self.api = FakeProwlarr()
        self.addCleanup(mock.patch.stopall)
        for name, value in (
            ("CONFIG_MANAGER", self.config),
            ("_parse_arr_api_key", mock.Mock(return_value="fixture-token")),
            ("_wait_for_prowlarr", mock.Mock(return_value=True)),
            ("_wait_for_arr", mock.Mock(return_value=True)),
            ("_get_prowlarr_schemas", mock.Mock(return_value=[APP_SCHEMA])),
            (
                "_collect_arr_entries",
                mock.Mock(side_effect=lambda: copy.deepcopy(self.entries)),
            ),
            ("_prowlarr_req", self.api.request),
        ):
            mock.patch.object(prowlarr, name, value).start()

    def run_patch(self):
        results = []
        outcome = prowlarr.patch_prowlarr_apps(results)
        return outcome, results

    def test_explicit_routing_creates_exact_link_and_operational_workflow_tags_only(
        self,
    ):
        outcome, results = self.run_patch()
        self.assertTrue(outcome[0], outcome)
        self.assertEqual(len(self.api.apps), 1)
        saved = next(iter(self.api.apps.values()))
        self.assertEqual(saved["name"], "Radarr (movies)")
        self.assertEqual([tag["label"] for tag in self.api.tags], ["altmount"])
        self.assertEqual(saved["tags"], [1])
        ownership = json.loads((self.root / ".dumb-setup-ownership.json").read_text())
        self.assertEqual(ownership["applications"], {"radarr.movies": saved["id"]})
        self.assertNotIn("fixture", json.dumps(ownership))
        self.assertEqual(results[-1].execution, "applied")
        self.assertEqual(results[-1].verification, "not_checked")
        self.assertEqual(self.run_patch()[1][-1].execution, "already_present")
        self.assertEqual(
            len(
                [
                    call
                    for call in self.api.calls
                    if call[1] == "POST" and call[0].endswith("/applications")
                ]
            ),
            1,
        )

    def test_compatibility_all_and_explicit_empty_retire_only_owned(self):
        self.instance["arr_instances"] = None
        self.assertTrue(self.run_patch()[0][0])
        self.assertEqual(len(self.api.apps), 2)
        self.api.apps[99] = {"id": 99, "name": "personal", "enable": True, "tags": []}
        self.instance["arr_instances"] = []
        self.assertTrue(self.run_patch()[0][0])
        self.assertFalse(self.api.apps[1]["enable"])
        self.assertFalse(self.api.apps[2]["enable"])
        self.assertTrue(self.api.apps[99]["enable"])

    def test_owned_policy_and_user_fields_are_preserved_on_endpoint_change(self):
        self.run_patch()
        app = self.api.apps[1]
        app["syncLevel"] = "addOnly"
        app["fields"].append({"name": "userPolicy", "value": "keep"})
        app["fields"][-2]["value"] = [2999]
        app["tags"].append(40)
        self.entries[0]["host"] = "http://127.0.0.1:7888/newbase"
        outcome, _ = self.run_patch()
        self.assertTrue(outcome[0], outcome)
        saved = self.api.apps[1]
        self.assertEqual(saved["syncLevel"], "addOnly")
        self.assertIn({"name": "userPolicy", "value": "keep"}, saved["fields"])
        self.assertIn(40, saved["tags"])
        self.assertEqual(
            next(
                field["value"]
                for field in saved["fields"]
                if field["name"] == "baseUrl"
            ),
            self.entries[0]["host"],
        )
        saved["enable"] = False
        self.assertFalse(self.run_patch()[0][0])
        self.assertFalse(self.api.apps[1]["enable"])

    def test_unowned_collision_and_unknown_assignment_need_input_without_writes(self):
        self.api.apps[7] = {
            "id": 7,
            "name": "Radarr (movies)",
            "fields": [],
            "enable": False,
        }
        before = copy.deepcopy(self.api.apps)
        outcome, results = self.run_patch()
        self.assertFalse(outcome[0])
        self.assertEqual(self.api.apps, before)
        self.assertEqual(results[-1].execution, "waiting_input")
        self.instance["arr_instances"] = ["radarr.missing"]
        outcome, results = self.run_patch()
        self.assertFalse(outcome[0])
        self.assertEqual(results[0].execution, "waiting_input")

    def test_missing_key_waits_and_failed_readback_does_not_report_success(self):
        self.entries[0]["api_key"] = ""
        outcome, results = self.run_patch()
        self.assertFalse(outcome[0])
        self.assertEqual(results[-1].execution, "waiting_dependency")
        self.assertFalse(self.api.apps)
        self.entries[0]["api_key"] = "fixture-key"
        self.api.reject_readback = True
        outcome, results = self.run_patch()
        self.assertFalse(outcome[0])
        self.assertEqual(results[-1].execution, "failed")
        self.assertNotIn("fixture", results[-1].detail)

    def test_api_fallback_only_for_unavailable_routes_not_auth_or_invalid_write(self):
        calls = []

        def versioned(url, token, method="GET", data=None):
            calls.append(url)
            if "/v1/" in url:
                raise urllib.error.HTTPError(url, 404, "missing", {}, None)
            return []

        with mock.patch.object(prowlarr, "_prowlarr_req", side_effect=versioned):
            self.assertEqual(
                prowlarr._api_request(
                    "http://127.0.0.1:9696/native", "", "applications"
                ),
                [],
            )
        self.assertTrue(calls[-1].endswith("/native/api/v3/applications"))
        for status in (401, 400):
            with mock.patch.object(
                prowlarr,
                "_prowlarr_req",
                side_effect=urllib.error.HTTPError(
                    "http://example.invalid", status, "", {}, None
                ),
            ) as request:
                with self.assertRaises(urllib.error.HTTPError):
                    prowlarr._api_request(
                        "http://127.0.0.1:9696", "", "applications", "POST", {}
                    )
                self.assertEqual(request.call_count, 1)

    def test_owned_indexer_keeps_custom_url_user_tags_and_disable(self):
        existing = {
            "id": 4,
            "name": "Zilean",
            "enable": False,
            "implementation": "Torznab",
            "configContract": "TorznabSettings",
            "tags": [44],
            "fields": [
                {"name": "baseUrl", "value": "https://custom.example.invalid/torznab"},
                {"name": "apiKey", "value": "custom-fixture"},
            ],
        }
        ownership = {"applications": {}, "indexers": {"zilean": 4}}
        desired = {
            **copy.deepcopy(existing),
            "tags": [1],
            "enable": True,
            "fields": [{"name": "baseUrl", "value": "http://127.0.0.1:8182/torznab"}],
        }
        with mock.patch.object(
            prowlarr, "_apply_indexer", return_value=(True, 4)
        ) as apply:
            self.assertEqual(
                prowlarr._ensure_managed_indexer(
                    "", "", desired, existing, ownership, "zilean"
                ),
                (True, "applied"),
            )
        payload = apply.call_args.args[2]
        self.assertEqual(payload["fields"], existing["fields"])
        self.assertEqual(payload["tags"], [1, 44])
        self.assertFalse(payload["enable"])


class ProfilarrV1ReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config_root = self.root / "config"
        self.config_root.mkdir()
        self.db = self.config_root / "profilarr.db"
        with sqlite3.connect(self.db) as conn:
            conn.execute("""CREATE TABLE arr_config (
                id INTEGER PRIMARY KEY, name TEXT UNIQUE, type TEXT, tags TEXT,
                arr_server TEXT, api_key TEXT, data_to_sync TEXT,
                sync_method TEXT, sync_interval INTEGER, import_as_unique INTEGER)""")
        self.instance = {"config_dir": str(self.root), "core_service": "infinidysk"}
        self.entry = {
            "name": "radarr:movies",
            "type": "radarr",
            "tags": ["dumb:auto", "Radarr"],
            "arr_server": "http://127.0.0.1:7878/native",
            "api_key": "fixture-key",
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(profilarr, "CONFIG_MANAGER", Config({})).start()
        mock.patch.object(
            profilarr, "detect_profilarr_layout", return_value="v1"
        ).start()
        mock.patch.object(
            profilarr, "_ensure_profilarr_db", return_value=(True, None)
        ).start()
        mock.patch.object(
            profilarr, "_ensure_default_repo", return_value=(True, None)
        ).start()
        mock.patch.object(
            profilarr,
            "_build_arr_entries",
            side_effect=lambda cores: [copy.deepcopy(self.entry)],
        ).start()
        mock.patch.object(profilarr, "_eligible_arr_names", return_value=set()).start()
        mock.patch.object(profilarr, "_ensure_config_ownership").start()
        mock.patch.object(profilarr, "_ensure_repo_ownership").start()
        self.initial_sync = mock.patch.object(profilarr, "_run_initial_sync").start()

    def insert(self, name, tags):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                """INSERT INTO arr_config
            (name,type,tags,arr_server,api_key,data_to_sync,sync_method,sync_interval,import_as_unique)
            VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    name,
                    "radarr",
                    json.dumps(tags),
                    "https://old.example.invalid",
                    "old-fixture",
                    '{"profiles":["chosen"],"customFormats":[]}',
                    "scheduled",
                    120,
                    1,
                ),
            )

    def rows(self):
        with sqlite3.connect(self.db) as conn:
            conn.row_factory = sqlite3.Row
            return {
                row["name"]: dict(row)
                for row in conn.execute("SELECT * FROM arr_config")
            }

    def test_connection_updates_preserve_profiles_policy_tags_and_do_not_auto_sync(
        self,
    ):
        self.insert(self.entry["name"], ["dumb:auto", "personal"])
        self.assertTrue(profilarr.sync_profilarr_arr_configs(self.instance)[0])
        row = self.rows()[self.entry["name"]]
        self.assertEqual(row["arr_server"], self.entry["arr_server"])
        self.assertEqual(json.loads(row["data_to_sync"])["profiles"], ["chosen"])
        self.assertEqual(row["sync_method"], "scheduled")
        self.assertEqual(row["sync_interval"], 120)
        self.assertEqual(row["import_as_unique"], 1)
        self.assertIn("personal", json.loads(row["tags"]))
        self.initial_sync.assert_not_called()

    def test_unowned_collision_is_not_adopted(self):
        self.insert(self.entry["name"], ["personal"])
        before = self.rows()
        results = []
        self.assertFalse(
            profilarr.sync_profilarr_arr_configs(self.instance, results)[0]
        )
        self.assertEqual(self.rows(), before)
        self.assertEqual(results[-1].execution, "waiting_input")

    def test_empty_selection_cleans_only_managed_and_pending_keys_preserve_links(self):
        self.insert("radarr:old", ["dumb:auto"])
        self.insert("radarr:personal", ["personal"])
        with mock.patch.object(profilarr, "_build_arr_entries", return_value=[]):
            self.assertTrue(profilarr.sync_profilarr_arr_configs(self.instance)[0])
        self.assertEqual(set(self.rows()), {"radarr:personal"})
        self.insert("radarr:pending", ["dumb:auto"])
        with (
            mock.patch.object(profilarr, "_build_arr_entries", return_value=[]),
            mock.patch.object(
                profilarr, "_eligible_arr_names", return_value={"radarr:pending"}
            ),
        ):
            self.assertFalse(profilarr.sync_profilarr_arr_configs(self.instance)[0])
        self.assertIn("radarr:pending", self.rows())

    def test_repository_error_is_not_success_and_sqlite_failure_is_reported(self):
        with mock.patch.object(
            profilarr, "_ensure_default_repo", return_value=(False, "fixture failure")
        ):
            self.assertFalse(profilarr.sync_profilarr_arr_configs(self.instance)[0])
        self.db.unlink()
        self.db.write_bytes(b"not a database")
        results = []
        self.assertFalse(
            profilarr.sync_profilarr_arr_configs(self.instance, results)[0]
        )
        self.assertEqual(results[-1].execution, "failed")
