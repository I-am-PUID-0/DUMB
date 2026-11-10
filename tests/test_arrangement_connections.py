import copy
import json
from pathlib import Path
import tempfile
import unittest

from utils.arrangement_http import ApiResponse
from utils import arrangement_connections as connections


class FakeApplications:
    """Persist upstream configuration, including full replacement APIs and errors."""

    def __init__(self):
        self.rows = {
            family: {"radarr": [], "sonarr": []}
            for family in ("seerr", "pulsarr", "maintainerr")
        }
        self.calls = []
        self.api_failures = {}
        self.target_versions = {"radarr": "6.2.0", "sonarr": "4.0.0"}
        self.bazarr = {
            "general": {
                "use_radarr": False,
                "use_sonarr": False,
                "enabled_providers": ["user-provider"],
                "movie_default_enabled": False,
                "movie_default_profile": "",
            },
            "radarr": {
                "ip": "127.0.0.1",
                "port": 7878,
                "apikey": "",
                "base_url": "/",
                "ssl": False,
            },
            "sonarr": {
                "ip": "127.0.0.1",
                "port": 8989,
                "apikey": "",
                "base_url": "/",
                "ssl": False,
            },
        }
        self.languages = [
            {"code2": "en", "name": "English", "enabled": True},
            {"code2": "fr", "name": "French", "enabled": False},
        ]
        self.language_profiles = [{"profileId": 1, "name": "Preferred"}]
        self.failed_check = False
        self.bad_readback = False

    def factory(self, host, **kwargs):
        fake = self

        class Client:
            def request(self, method, path, **options):
                return fake.request(host, kwargs, method, path, options)

        return Client()

    def request(self, host, client_options, method, path, options):
        self.calls.append((host, method, path, copy.deepcopy(options), client_options))
        if path in self.api_failures:
            status, body = self.api_failures[path]
            return self.response(body, status)
        if ":7878" in host or ":8989" in host:
            family = "radarr" if ":7878" in host else "sonarr"
            if path == "/api/v3/system/status":
                return self.response(
                    {"appName": family, "version": self.target_versions[family]}
                )
            if path == "/api/v3/qualityprofile":
                return self.response(
                    [{"id": 3, "name": "HD"}, {"id": 4, "name": "UHD"}]
                )
            if path == "/api/v3/rootfolder":
                return self.response(
                    [
                        {
                            "id": 1,
                            "path": (
                                "/library/movies"
                                if family == "radarr"
                                else "/library/shows"
                            ),
                        }
                    ]
                )
            if path == "/api/v3/languageprofile":
                return self.response([{"id": 2, "name": "English"}])
        if ":6767" in host:
            if path == "/api/system/settings":
                if method == "POST":
                    for key, value in (options.get("data") or {}).items():
                        if key == "languages-enabled":
                            for row in self.languages:
                                row["enabled"] = row["code2"] in value
                            continue
                        _, section, field = key.split("-", 2)
                        if value in ("true", "false"):
                            value = value == "true"
                        elif field in {
                            "port",
                            "movie_default_profile",
                            "serie_default_profile",
                        }:
                            value = int(value)
                        self.bazarr[section][field] = value
                    return self.response(None, 204)
                return self.response(self.bazarr)
            if path == "/api/system/languages":
                return self.response(self.languages)
            if path == "/api/system/languages/profiles":
                return self.response(self.language_profiles)
        if ":6868" in host:
            if path == "/api/v1/status":
                return self.response({"version": "2.0.9", "arr": []})
            if path == "/api/v1/arr":
                return self.response([])
        family = (
            "seerr"
            if ":5055" in host
            else "pulsarr" if ":3003" in host else "maintainerr"
        )
        arr = "radarr" if "radarr" in path else "sonarr"
        is_test = (
            path.endswith("/test")
            or path.endswith("/test-connection")
            or "/test/" in path
        )
        if is_test:
            if family == "seerr":
                return self.response(
                    (
                        {"profiles": [], "rootFolders": []}
                        if not self.failed_check
                        else {"error": "fixture-secret"}
                    ),
                    200 if not self.failed_check else 500,
                )
            if family == "pulsarr":
                return self.response(
                    {"success": not self.failed_check, "message": "fixture-secret"}
                )
            return self.response(
                {
                    "status": "NOK" if self.failed_check else "OK",
                    "message": "fixture-secret",
                }
            )
        route = connections._path(family, arr)
        if path == route:
            if method == "GET":
                rows = self.rows[family][arr]
                if self.bad_readback and rows:
                    rows = [{**row, "apiKey": "wrong-fixture"} for row in rows]
                return self.response(rows)
            if method == "POST":
                row = copy.deepcopy(options["json"])
                row["id"] = (
                    max(
                        (item["id"] for item in self.rows[family][arr]),
                        default=-1 if family == "seerr" else 0,
                    )
                    + 1
                )
                self.rows[family][arr].append(row)
                return self.response({"id": row["id"]}, 201)
        if path.startswith(route + "/") and method == "PUT":
            identifier = int(path.rsplit("/", 1)[-1])
            row = {**copy.deepcopy(options["json"]), "id": identifier}
            self.rows[family][arr] = [
                row if item["id"] == identifier else item
                for item in self.rows[family][arr]
            ]
            return self.response(None, 204)
        raise AssertionError((host, method, path, options))

    @staticmethod
    def response(value, status=200):
        return ApiResponse(
            status, b"" if value is None else json.dumps(copy.deepcopy(value)).encode()
        )

    def writes(self):
        return [
            call
            for call in self.calls
            if call[1] in {"POST", "PUT", "PATCH", "DELETE"}
            and not (
                call[2].endswith("/test")
                or call[2].endswith("/test-connection")
                or "/test/" in call[2]
            )
        ]


class ArrangementConnectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.owner = self.root / "ownership"
        self.config = {
            "radarr": {"instances": {"Movies": self.arr("radarr", 7878, "/native")}},
            "sonarr": {"instances": {"Shows": self.arr("sonarr", 8989, "/shows")}},
            "seerr": {"instances": {"Requests": {"enabled": True, "port": 5055}}},
            "pulsarr": {"enabled": True, "port": 3003},
            "maintainerr": {"enabled": True, "port": 6246},
            "bazarr": {"enabled": True, "port": 6767},
            "profilarr": {"instances": {"Profiles": {"enabled": True, "port": 6868}}},
        }
        self.api = FakeApplications()

    def arr(self, family, port, base):
        path = self.root / (family + ".xml")
        path.write_text(
            f"<Config><ApiKey>fixture-{family}-key</ApiKey><UrlBase>{base}</UrlBase></Config>"
        )
        return {"enabled": True, "port": port, "config_file": str(path)}

    def payload(self, source, targets=("radarr.Movies",)):
        values = {
            f"{source}::api_key": "fixture-source-key",
            f"{source}::arr_instances": list(targets),
        }
        for target in targets:
            values[f"{source}::{target}::quality_profile"] = 3
            values[f"{source}::{target}::root_folder"] = (
                "/library/movies" if target.startswith("radarr.") else "/library/shows"
            )
        return {"arrangement": {"values": values}}

    def action(self, source, kind="arr_connections"):
        return {"source": source, "service_key": source.split(".")[0], "action": kind}

    def apply(self, source, payload=None, operation="apply", kind="arr_connections"):
        return connections.apply_connection(
            self.action(source, kind),
            self.config,
            payload or self.payload(source),
            operation,
            client_factory=self.api.factory,
            ownership_directory=str(self.owner),
        )

    def test_supported_consumers_round_trip_verify_native_base_and_repeat_without_writes(
        self,
    ):
        for source in ("seerr.Requests", "pulsarr", "maintainerr"):
            with self.subTest(source=source):
                result = self.apply(source)
                self.assertEqual(result["execution"], "applied", result)
                self.assertEqual(result["verification"], "passed")
                self.assertNotIn("fixture-", json.dumps(result))
                row = self.api.rows[source.split(".")[0]]["radarr"][0]
                if source.startswith("seerr"):
                    self.assertEqual(row["baseUrl"], "/native")
                    self.assertTrue(row["preventSearch"])
                    self.assertFalse(row["syncEnabled"])
                elif source == "pulsarr":
                    self.assertFalse(row["searchOnAdd"])
                    self.assertEqual(row["monitor"], "none")
                    self.assertFalse(row["isDefault"])
                else:
                    self.assertEqual(row["url"], "http://127.0.0.1:7878/native")
                before = len(self.api.writes())
                repeated = self.apply(source)
                self.assertEqual(repeated["execution"], "already_present", repeated)
                self.assertEqual(len(self.api.writes()), before)
        self.assertTrue(all(call[4].get("managed") for call in self.api.calls))
        for path in self.owner.iterdir():
            self.assertNotIn("fixture-", path.read_text())

    def test_readonly_inspect_never_writes_application_or_ownership(self):
        result = self.apply("seerr.Requests", operation="inspect")
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(self.api.writes(), [])
        self.assertFalse(self.owner.exists())
        self.assertTrue(all(call[1] == "GET" for call in self.api.calls))

    def test_pulsarr_native_text_profile_readback_is_idempotent_without_general_coercion(
        self,
    ):
        request = self.api.request

        def native_shape(host, client_options, method, path, options):
            result = request(host, client_options, method, path, options)
            if ":3003" in host and "/instances" in path:
                for rows in self.api.rows["pulsarr"].values():
                    for row in rows:
                        row["qualityProfile"] = str(row["qualityProfile"])
                if method == "GET":
                    arr = "radarr" if "radarr" in path else "sonarr"
                    return self.api.response(self.api.rows["pulsarr"][arr])
            return result

        self.api.request = native_shape
        first = self.apply("pulsarr")
        self.assertEqual(first["verification"], "passed", first)
        writes = len(self.api.writes())
        self.assertEqual(self.apply("pulsarr")["execution"], "already_present")
        self.assertEqual(len(self.api.writes()), writes)
        self.assertFalse(
            connections._connection_matches(
                {"qualityProfile": "03"}, {"qualityProfile": 3}, "pulsarr"
            )
        )
        self.assertFalse(
            connections._connection_matches(
                {"isDefault": "false"}, {"isDefault": False}, "pulsarr"
            )
        )

    def test_existing_policies_and_other_rows_survive_explicit_adoption(self):
        old = {
            "id": 7,
            "name": "Personal movies",
            "baseUrl": "http://127.0.0.1:7878/native",
            "apiKey": "old-fixture",
            "qualityProfile": 4,
            "rootFolder": "/library/movies",
            "searchOnAdd": False,
            "monitor": "none",
            "tags": ["keep"],
            "isDefault": False,
        }
        self.api.rows["pulsarr"]["radarr"] = [
            copy.deepcopy(old),
            {
                "id": 8,
                "name": "unrelated",
                "baseUrl": "https://example.invalid",
                "tags": ["custom"],
            },
        ]
        before = copy.deepcopy(self.api.rows)
        result = self.apply("pulsarr")
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(self.api.rows, before)
        payload = self.payload("pulsarr")
        proposal = self.apply("pulsarr", payload, operation="preview")
        payload["arrangement"]["values"]["pulsarr::adoption_preview_id"] = proposal[
            "evidence"
        ]["preview_id"]
        payload["arrangement"]["values"]["pulsarr::adopt_existing"] = True
        result = self.apply("pulsarr", payload)
        self.assertEqual(result["verification"], "passed", result)
        saved = self.api.rows["pulsarr"]["radarr"][0]
        self.assertEqual(saved["tags"], ["keep"])
        self.assertFalse(saved["searchOnAdd"])
        self.assertEqual(saved["qualityProfile"], 3)
        self.assertEqual(
            self.api.rows["pulsarr"]["radarr"][1], before["pulsarr"]["radarr"][1]
        )

    def test_correct_unowned_connection_is_verified_without_adopting_or_mutating(self):
        self.apply("maintainerr")
        for path in self.owner.iterdir():
            path.unlink()
        before = len(self.api.writes())
        result = self.apply("maintainerr")
        self.assertEqual(result["execution"], "already_present", result)
        self.assertEqual(len(self.api.writes()), before)
        self.assertEqual(list(self.owner.iterdir()), [])

    def test_default_route_cannot_be_stolen_from_an_existing_connection(self):
        self.api.rows["seerr"]["radarr"] = [
            {
                "id": 8,
                "name": "Existing default",
                "hostname": "example.invalid",
                "port": 7878,
                "baseUrl": "",
                "isDefault": True,
                "is4k": False,
            }
        ]
        payload = self.payload("seerr.Requests")
        payload["arrangement"]["values"][
            "seerr.Requests::radarr.Movies::is_default"
        ] = True
        result = self.apply("seerr.Requests", payload)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertEqual(self.api.writes(), [])
        payload["arrangement"]["values"]["seerr.Requests::radarr.Movies::is_4k"] = True
        self.assertEqual(
            self.apply("seerr.Requests", payload)["verification"], "passed"
        )

    def test_sonarr_v3_requires_language_and_v4_does_not_call_removed_endpoint(self):
        self.api.target_versions["sonarr"] = "3.0.10"
        payload = self.payload("seerr.Requests", ("sonarr.Shows",))
        result = self.apply("seerr.Requests", payload)
        self.assertEqual(result["execution"], "waiting_input")
        payload["arrangement"]["values"][
            "seerr.Requests::sonarr.Shows::language_profile"
        ] = 2
        self.assertEqual(
            self.apply("seerr.Requests", payload)["verification"], "passed"
        )
        self.api.target_versions["sonarr"] = "4.0.0"
        self.api.calls.clear()
        self.apply("pulsarr", self.payload("pulsarr", ("sonarr.Shows",)))
        self.assertFalse(
            any(call[2].endswith("/languageprofile") for call in self.api.calls)
        )
        row = self.api.rows["pulsarr"]["sonarr"][0]
        self.assertEqual(row["seasonMonitoring"], "none")
        self.assertEqual(row["monitorNewItems"], "none")

    def test_discovery_returns_real_profiles_roots_without_credentials(self):
        source = "seerr.Requests"
        fields = connections.discover_fields(
            self.action(source),
            self.config,
            self.payload(source),
            client_factory=self.api.factory,
        )
        by_key = {field["key"]: field for field in fields}
        self.assertEqual(
            by_key[f"{source}::radarr.Movies::quality_profile"]["options"][0],
            {"value": 3, "label": "HD"},
        )
        self.assertEqual(
            by_key[f"{source}::radarr.Movies::root_folder"]["options"][0]["value"],
            "/library/movies",
        )
        self.assertNotIn("fixture-", json.dumps(fields))
        self.assertEqual(self.api.writes(), [])

    def test_api_auth_schema_and_inband_failures_are_truthful_and_redacted(self):
        for code in (401, 403, 404):
            with self.subTest(code=code):
                self.api.api_failures["/api/v1/settings/radarr"] = (
                    code,
                    {"error": "fixture-secret"},
                )
                result = self.apply("seerr.Requests")
                self.assertEqual(result["execution"], "waiting_input")
                self.assertNotIn("fixture-secret", json.dumps(result))
        self.api.api_failures.clear()
        self.api.failed_check = True
        for source in ("seerr.Requests", "pulsarr", "maintainerr"):
            result = self.apply(source)
            self.assertEqual(result["execution"], "failed", result)
            self.assertEqual(self.api.writes(), [])
            self.assertNotIn("fixture-secret", json.dumps(result))

    def test_partial_failure_retains_completed_link_evidence_and_retry_identity(self):
        payload = self.payload("maintainerr", ("radarr.Movies", "sonarr.Shows"))
        self.api.api_failures["/api/settings/sonarr"] = (503, {})
        result = self.apply("maintainerr", payload)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(result["evidence"][0]["configuration"], "applied")
        self.assertEqual(result["evidence"][1]["execution"], "failed")
        self.api.api_failures.clear()
        self.assertEqual(self.apply("maintainerr", payload)["verification"], "passed")
        self.assertEqual(len(self.api.rows["maintainerr"]["radarr"]), 1)

    def test_bad_readback_does_not_report_verified_or_duplicate_on_retry(self):
        self.api.bad_readback = True
        result = self.apply("pulsarr")
        self.assertEqual(result["execution"], "failed")
        self.assertNotEqual(result["verification"], "passed")
        self.api.bad_readback = False
        self.assertEqual(self.apply("pulsarr")["verification"], "passed")
        self.assertEqual(len(self.api.rows["pulsarr"]["radarr"]), 1)

    def test_owned_endpoint_drift_requires_explicit_adoption(self):
        self.apply("pulsarr")
        self.api.rows["pulsarr"]["radarr"][0][
            "baseUrl"
        ] = "https://custom.example.invalid"
        result = self.apply("pulsarr")
        self.assertEqual(result["execution"], "waiting_input")
        payload = self.payload("pulsarr")
        proposal = self.apply("pulsarr", payload, operation="preview")
        payload["arrangement"]["values"]["pulsarr::adoption_preview_id"] = proposal[
            "evidence"
        ]["preview_id"]
        payload["arrangement"]["values"]["pulsarr::adopt_existing"] = True
        self.assertEqual(self.apply("pulsarr", payload)["verification"], "passed")

    def test_ownership_directory_rejects_symlinks_and_shared_permissions(self):
        self.owner.mkdir()
        self.owner.chmod(0o755)
        result = self.apply("pulsarr")
        self.assertEqual(result["execution"], "waiting_input")
        self.owner.rmdir()
        self.owner.symlink_to(self.root)
        result = self.apply("pulsarr")
        self.assertEqual(result["execution"], "waiting_input")

    def test_bazarr_connects_without_enabling_providers_and_keeps_existing_languages(
        self,
    ):
        payload = self.payload("bazarr")
        payload["arrangement"]["values"].update(
            {
                "bazarr::enable_integrations": True,
                "bazarr::languages": ["fr"],
                "bazarr::language_profile": 1,
            }
        )
        result = self.apply("bazarr", payload)
        self.assertIn(result["execution"], {"applied", "already_present"}, result)
        self.assertEqual(self.api.bazarr["radarr"]["base_url"], "/native")
        self.assertEqual(
            self.api.bazarr["general"]["enabled_providers"], ["user-provider"]
        )
        self.assertTrue(self.api.bazarr["general"]["use_radarr"])
        self.assertTrue(all(row["enabled"] for row in self.api.languages))
        self.assertEqual(self.api.bazarr["general"]["movie_default_profile"], 1)
        self.assertNotEqual(result["verification"], "passed")
        self.assertTrue(
            all(
                "rules" not in call[2] and "search" not in call[2]
                for call in self.api.calls
            )
        )

    def test_bazarr_repeat_with_languages_and_profile_does_not_rewrite(self):
        payload = self.payload("bazarr")
        payload["arrangement"]["values"].update(
            {
                "bazarr::enable_integrations": True,
                "bazarr::languages": ["fr"],
                "bazarr::language_profile": 1,
            }
        )
        self.assertEqual(self.apply("bazarr", payload)["execution"], "applied")
        before = len(self.api.writes())
        self.assertEqual(self.apply("bazarr", payload)["execution"], "already_present")
        self.assertEqual(len(self.api.writes()), before)

    def test_maintainerr_case_sensitive_native_route_is_not_written_incorrectly(self):
        path = Path(self.config["radarr"]["instances"]["Movies"]["config_file"])
        path.write_text(
            "<Config><ApiKey>fixture-key</ApiKey><UrlBase>/Native</UrlBase></Config>"
        )
        outcome = self.apply("maintainerr")
        self.assertEqual(outcome["execution"], "waiting_input")
        self.assertEqual(self.api.writes(), [])

    def test_bazarr_requires_activation_and_rejects_duplicate_arr_family(self):
        result = self.apply("bazarr")
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(self.api.bazarr["general"]["use_radarr"])
        self.assertIn("bazarr::enable_integrations", result["missing_inputs"])
        self.config["radarr"]["instances"]["Other"] = self.config["radarr"][
            "instances"
        ]["Movies"].copy()
        result = self.apply(
            "bazarr", self.payload("bazarr", ("radarr.Movies", "radarr.Other"))
        )
        self.assertEqual(result["execution"], "waiting_input")

    def test_profilarr_public_api_does_not_invent_profile_mutations(self):
        payload = self.payload("profilarr.Profiles")
        result = self.apply("profilarr.Profiles", payload, kind="profiles")
        self.assertEqual(result["execution"], "waiting_input")
        self.assertIn("does not support", result["reason"])
        self.assertEqual(result["automation"], "unsupported")
        self.assertTrue(all(call[1] == "GET" for call in self.api.calls))

    def test_unknown_services_and_unhandled_actions_return_none(self):
        self.assertIsNone(self.apply("plex", kind="libraries"))
        self.assertIsNone(self.apply("seerr.Requests", kind="account"))

    def test_unknown_api_contracts_do_not_claim_supported_automation(self):
        for status, body in ((404, {}), (200, {"unexpected": []})):
            with self.subTest(status=status):
                self.api.api_failures["/api/v1/settings/radarr"] = (status, body)
                result = self.apply("seerr.Requests")
                self.assertEqual(result["execution"], "waiting_input", result)
                self.assertEqual(result["automation"], "unknown", result)
                self.assertFalse(result["retryable"])
                self.assertNotEqual(result["verification"], "passed")
                self.assertEqual(self.api.writes(), [])
        self.api.api_failures.clear()
        self.api.api_failures["/api/settings/sonarr"] = (404, {})
        result = self.apply(
            "maintainerr",
            self.payload("maintainerr", ("radarr.Movies", "sonarr.Shows")),
        )
        self.assertEqual(result["automation"], "unknown")
        self.assertEqual(result["evidence"][0]["configuration"], "applied")
        self.assertEqual(result["evidence"][1]["automation"], "unknown")

    def test_browser_select_strings_resolve_to_native_numeric_profile_ids(self):
        self.api.target_versions["sonarr"] = "3.0.10"
        payload = self.payload("seerr.Requests", ("sonarr.Shows",))
        payload["arrangement"]["values"].update(
            {
                "seerr.Requests::sonarr.Shows::quality_profile": "3",
                "seerr.Requests::sonarr.Shows::language_profile": "2",
            }
        )
        self.assertEqual(
            self.apply("seerr.Requests", payload)["verification"], "passed"
        )
        seerr = self.api.rows["seerr"]["sonarr"][0]
        self.assertEqual(seerr["activeProfileId"], 3)
        self.assertIs(type(seerr["activeProfileId"]), int)
        self.assertEqual(seerr["activeLanguageProfileId"], 2)
        self.assertIs(type(seerr["activeLanguageProfileId"]), int)
        payload = self.payload("pulsarr")
        payload["arrangement"]["values"][
            "pulsarr::radarr.Movies::quality_profile"
        ] = "4"
        self.assertEqual(self.apply("pulsarr", payload)["verification"], "passed")
        self.assertEqual(self.api.rows["pulsarr"]["radarr"][0]["qualityProfile"], 4)
        self.assertIs(
            type(self.api.rows["pulsarr"]["radarr"][0]["qualityProfile"]), int
        )
        payload = self.payload("bazarr")
        payload["arrangement"]["values"].update(
            {"bazarr::language_profile": "1", "bazarr::enable_integrations": True}
        )
        result = self.apply("bazarr", payload)
        self.assertEqual(result["execution"], "applied", result)
        self.assertEqual(self.api.bazarr["general"]["movie_default_profile"], 1)
