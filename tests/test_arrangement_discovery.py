import copy
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import arrangement_discovery as adapter
from utils.arrangement_http import ApiError, ApiResponse
from utils.zurg_library_hook import (
    refresh_changed_paths,
    validate_hook_configuration,
    main,
)


class API:
    def __init__(self):
        self.calls = []
        self.public_id = "selected-machine"
        self.public_code = 200
        self.public_stage = None
        self.stats = {"total_items": 0, "total_metadata": 0}
        self.health = {"status": "healthy", "version": "v4"}

    def factory(self, url, **options):
        api = self

        class Client:
            def request(self, method, path, **kwargs):
                api.calls.append((url, options, method, path, kwargs))
                public = url.startswith("https:")
                if public and api.public_stage:
                    raise ApiError(stage=api.public_stage)
                status = api.public_code if public else 200
                if path == "/identity":
                    identifier = api.public_id if public else "selected-machine"
                    return ApiResponse(
                        status,
                        f'<MediaContainer machineIdentifier="{identifier}"/>'.encode(),
                    )
                if path == "/library/sections":
                    return ApiResponse(
                        200,
                        b'<MediaContainer><Directory key="1"><Location path="/media/movies"/></Directory><Directory key="2"><Location path="/media/shows"/></Directory><Directory key="3"><Location path="/other"/></Directory></MediaContainer>',
                    )
                if path == "/healthchecks/ping":
                    return ApiResponse(200, b"[today]: Pong!")
                value = api.stats if path == "/api/stats" else api.health
                return ApiResponse(status, json.dumps(value).encode())

        return Client()


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = API()
        self.native = self.root / "config.json"
        self.doc = {
            "Metadata Battery": {"url": "http://127.0.0.1:5001"},
            "UI Settings": {"enable_phalanx_db": False},
            "Provider": {"key": "preserved-private-key"},
            "Policies": {"run": False},
        }
        self.native.write_text(json.dumps(self.doc))
        self.config = {
            "cli_debrid": {
                "enabled": True,
                "config_dir": self.temp.name,
                "commit_sha": "db84f5e99181f5dc46cc0e3b170a6dd238f1f14d",
                "env": {
                    "USER_CONFIG": self.temp.name,
                    "CLI_DEBRID_BATTERY_PORT": "5001",
                },
            },
            "cli_battery": {"enabled": True, "port": 5001},
            "phalanx_db": {"enabled": True, "port": 8888},
            "plex": {"enabled": True, "port": 32400},
            "zilean": {
                "enabled": True,
                "port": 8181,
                "log_file": str(self.root / "zilean.log"),
            },
            "cloudflared": {"enabled": True},
            "zurg": {
                "instances": {
                    "Main": {
                        "enabled": True,
                        "config_dir": self.temp.name,
                        "config_file": str(self.root / "config.yml"),
                    }
                }
            },
        }
        self.values = {}
        self.action = {
            "source": "cli_debrid",
            "service_key": "cli_debrid",
            "action": "companion_connection",
        }

    def call(self, apply=False, stopped=False):
        return (adapter.apply_discovery if apply else adapter.inspect_discovery)(
            self.action,
            self.config,
            {"arrangement": {"values": self.values}, "_service_stopped": stopped},
            client_factory=self.api.factory,
        )

    def zurg(self):
        self.action = {
            "source": "zurg.Main",
            "service_key": "zurg",
            "action": "discovery",
        }
        (self.root / "version.txt").write_text("v1.0.0")
        (self.root / "config.yml").write_text(
            "# Preserve comment\nzurg: v1\ntoken: private-rd-token\ndirectories:\n  Movies:\n    filters:\n      - regex: movie\n"
        )

    def test_actual_native_battery_binding_and_healthy_api(self):
        before = self.native.read_bytes()
        result = self.call()
        self.assertEqual(
            ("already_present", "passed"), (result["execution"], result["verification"])
        )
        self.assertTrue(result["evidence"]["battery_api_ready"])
        self.assertEqual(before, self.native.read_bytes())
        self.assertEqual(["/api/stats"], [row[3] for row in self.api.calls])

    def test_wrong_existing_battery_url_needs_explicit_adoption_and_stop(self):
        self.doc["Metadata Battery"]["url"] = "http://localhost:50051"
        self.native.write_text(json.dumps(self.doc))
        before = self.native.read_bytes()
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.values["cli_debrid::adopt_companion_settings"] = True
        self.assertEqual("planned", self.call()["execution"])
        self.assertEqual("waiting_input", self.call(True)["execution"])
        self.assertEqual(before, self.native.read_bytes())
        self.assertEqual("applied", self.call(True, True)["execution"])
        actual = json.loads(self.native.read_text())
        self.assertEqual("http://127.0.0.1:5001", actual["Metadata Battery"]["url"])
        self.assertEqual(self.doc["Provider"], actual["Provider"])
        self.assertEqual(self.doc["Policies"], actual["Policies"])
        self.assertEqual("already_present", self.call()["execution"])

    def test_phalanx_is_explicit_and_health_checked_without_data_queries(self):
        self.config["cli_debrid"]["use_phalanx_db"] = True
        self.config["cli_debrid"]["env"].update(
            {"CLI_DEBRID_PHALANX_HOST": "127.0.0.1", "CLI_DEBRID_PHALANX_PORT": "8888"}
        )
        self.doc["UI Settings"]["enable_phalanx_db"] = True
        self.native.write_text(json.dumps(self.doc))
        result = self.call()
        self.assertEqual("already_present", result["execution"])
        self.assertTrue(result["evidence"]["phalanx_api_ready"])
        self.assertIn("/health", [row[3] for row in self.api.calls])
        self.assertFalse(any("entries" in row[3] for row in self.api.calls))
        self.config["cli_debrid"]["env"]["CLI_DEBRID_PHALANX_PORT"] = "9999"
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_phalanx_environment_sync_is_selected_scoped_and_failure_atomic(self):
        cfg = self.config["cli_debrid"]
        original = copy.deepcopy(cfg["env"])
        self.assertFalse(
            adapter.sync_cli_phalanx_environment(cfg, {"port": 9998})["changed"]
        )
        self.assertEqual(original, cfg["env"])
        cfg["use_phalanx_db"] = True
        plan = adapter.sync_cli_phalanx_environment(cfg, {"port": 9998}, apply=False)
        self.assertTrue(plan["changed"])
        self.assertEqual(original, cfg["env"])
        with patch.object(adapter, "write_ownership", side_effect=OSError):
            with self.assertRaises(OSError):
                adapter.sync_cli_phalanx_environment(cfg, {"port": 9998})
        self.assertEqual(original, cfg["env"])
        adapter.sync_cli_phalanx_environment(cfg, {"port": 9998})
        self.assertEqual("9998", cfg["env"]["CLI_DEBRID_PHALANX_PORT"])
        self.assertEqual(original["USER_CONFIG"], cfg["env"]["USER_CONFIG"])
        adapter.sync_cli_phalanx_environment(cfg, {"port": 9997})
        self.assertEqual("9997", cfg["env"]["CLI_DEBRID_PHALANX_PORT"])
        cfg["env"]["CLI_DEBRID_PHALANX_HOST"] = "custom.example.com"
        self.assertTrue(
            adapter.sync_cli_phalanx_environment(cfg, {"port": 9997})[
                "adoption_required"
            ]
        )
        self.assertEqual("custom.example.com", cfg["env"]["CLI_DEBRID_PHALANX_HOST"])

    def test_phalanx_explicit_binding_adoption_requires_healthy_target_stop_and_save(
        self,
    ):
        cfg = self.config["cli_debrid"]
        cfg["use_phalanx_db"] = True
        cfg["env"]["CLI_DEBRID_PHALANX_HOST"] = "custom.example.com"
        self.values["cli_debrid::adopt_companion_settings"] = True
        self.api.health = {"status": "not-ready", "version": "v4"}
        before = copy.deepcopy(cfg["env"])
        self.assertEqual("waiting_input", self.call()["execution"])
        self.api.health["status"] = "healthy"
        self.assertEqual("planned", self.call()["execution"])
        self.assertEqual(before, cfg["env"])
        manager = SimpleNamespace(
            save_config=lambda name: (_ for _ in ()).throw(OSError())
        )
        payload = {"arrangement": {"values": self.values}, "_service_stopped": True}
        result = adapter.apply_discovery(
            self.action,
            self.config,
            payload,
            client_factory=self.api.factory,
            manager=manager,
        )
        self.assertEqual("failed", result["execution"])
        self.assertEqual(before, cfg["env"])
        manager.save_config = lambda name: None
        result = adapter.apply_discovery(
            self.action,
            self.config,
            payload,
            client_factory=self.api.factory,
            manager=manager,
        )
        self.assertEqual("applied", result["execution"])
        self.assertEqual("127.0.0.1", cfg["env"]["CLI_DEBRID_PHALANX_HOST"])
        self.assertTrue(
            json.loads(self.native.read_text())["UI Settings"]["enable_phalanx_db"]
        )

    def test_embedded_layout_skips_standalone_service_and_rejects_unknown_contract(
        self,
    ):
        self.config["cli_debrid"][
            "commit_sha"
        ] = "c971c60336e3f85aad8f0624591885c7c1da83ce"
        self.config["cli_battery"]["enabled"] = False
        self.assertEqual("already_present", self.call()["execution"])
        self.assertEqual([], self.api.calls)
        self.config["cli_debrid"]["commit_sha"] = "f" * 40
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_battery_malformed_health_or_missing_generated_file_never_claims_ready(
        self,
    ):
        self.api.stats = {"total_items": True, "total_metadata": 0}
        self.assertEqual("waiting_input", self.call()["execution"])
        self.native.unlink()
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_zilean_api_health_is_separate_from_observed_ingestion(self):
        self.action = {
            "source": "zilean",
            "service_key": "zilean",
            "action": "ingestion",
        }
        for text, expected in (
            ("api ready", "unknown"),
            ("Found 123 files to parse", "running"),
            ("Found 123 files to parse\nDMM Internal Tasks Completed", "completed"),
        ):
            (self.root / "zilean.log").write_text(text)
            result = self.call()
            self.assertTrue(result["evidence"]["api_ready"])
            self.assertEqual(expected, result["evidence"]["ingestion_observed"])
            self.assertFalse(result["evidence"]["dataset_count_verified"])
            self.assertNotIn(text, json.dumps(result))

    def test_zurg_unknown_nightly_never_guesses_provider(self):
        self.zurg()
        result = self.call()
        self.assertEqual("realdebrid", result["evidence"]["provider"])
        self.assertFalse(result["evidence"]["credential_verified"])
        self.assertNotIn("private-rd-token", json.dumps(result))
        (self.root / "version.txt").write_text("2026.10.13.1200-nightly")
        self.assertEqual("unsupported", self.call()["execution"])

    def route(self):
        self.action = {
            "source": "cloudflared",
            "service_key": "cloudflared",
            "action": "public_route",
        }
        self.values = {
            "cloudflared::public_url": "https://media.example.com",
            "cloudflared::public_target": "plex",
        }

    def test_public_identity_match_has_no_outbound_credentials_or_dns_ownership_claim(
        self,
    ):
        self.route()
        result = self.call()
        self.assertEqual("passed", result["verification"])
        self.assertEqual("matched", result["evidence"]["application_identity"])
        self.assertEqual("unknown", result["evidence"]["dns_ownership"])
        remote = [row for row in self.api.calls if row[0].startswith("https:")]
        self.assertEqual({}, remote[0][4])
        self.assertNotIn("headers", remote[0][1])
        self.assertTrue(remote[0][1]["verify_tls"])
        self.assertFalse(remote[0][1]["allow_private"])

    def test_public_identity_mismatch_is_failure_and_auth_protection_is_unknown(self):
        self.route()
        self.api.public_id = "other-server"
        result = self.call()
        self.assertEqual(
            ("failed", "failed"), (result["execution"], result["verification"])
        )
        self.api.public_code = 401
        result = self.call()
        self.assertEqual("unavailable", result["verification"])
        self.assertEqual("unknown", result["evidence"]["application_identity"])

    def test_public_dns_tls_transport_stages_and_redirect_are_distinguished(self):
        self.route()
        for stage in ("dns", "tls", "connect", "timeout"):
            self.api.public_stage = stage
            result = self.call()
            self.assertEqual("waiting_dependency", result["execution"])
            self.assertEqual(stage, result["evidence"]["failed_stage"])
        self.api.public_stage = None
        self.api.public_code = 302
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_public_input_rejects_credentials_http_embedded_and_disabled_target(self):
        self.route()
        for origin in (
            "http://media.example.com",
            "https://user:secret@media.example.com",
            "https://media.example.com/ui/plex",
        ):
            self.values["cloudflared::public_url"] = origin
            self.assertEqual("waiting_input", self.call()["execution"])
        self.assertEqual([], self.api.calls)
        self.values["cloudflared::public_url"] = "https://media.example.com"
        self.config["plex"]["enabled"] = False
        self.assertEqual("waiting_input", self.call()["execution"])

    def test_hook_is_explicit_scoped_preserves_yaml_and_requires_controlled_stop(self):
        self.zurg()
        self.action["action"] = "library_hook"
        self.values = {
            "zurg.Main::enable_library_hook": True,
            "zurg.Main::media_server": "plex",
            "zurg.Main::scan_library_ids": "1,2",
            "zurg.Main::scan_mount_path": "/media",
        }
        before = (self.root / "config.yml").read_bytes()
        with patch.object(
            adapter,
            "_managed_plex",
            return_value=("http://127.0.0.1:32400", "plex-private-token", True),
        ):
            self.assertEqual("planned", self.call()["execution"])
            self.assertEqual("waiting_input", self.call(True)["execution"])
            self.assertEqual(before, (self.root / "config.yml").read_bytes())
            result = self.call(True, True)
            self.assertEqual("applied", result["execution"])
            self.assertFalse(result["evidence"]["scan_triggered"])
            self.assertEqual("already_present", self.call()["execution"])
        content = (self.root / "config.yml").read_text()
        self.assertIn("# Preserve comment", content)
        self.assertIn("regex: movie", content)
        self.assertNotIn("plex-private-token", content)
        private = self.root / ".dumb-zurg-library-hook.json"
        self.assertEqual(0o600, private.stat().st_mode & 0o777)
        self.assertFalse(any("refresh" in row[3] for row in self.api.calls))

    def test_user_hook_and_nonoverlapping_or_unknown_library_are_preserved(self):
        self.zurg()
        self.action["action"] = "library_hook"
        self.values = {
            "zurg.Main::enable_library_hook": True,
            "zurg.Main::media_server": "plex",
            "zurg.Main::scan_library_ids": "1",
            "zurg.Main::scan_mount_path": "/media",
        }
        native = self.root / "config.yml"
        native.write_text(native.read_text() + "on_library_update: custom-user-hook\n")
        before = native.read_bytes()
        with patch.object(
            adapter,
            "_managed_plex",
            return_value=("http://127.0.0.1:32400", "plex-private-token", True),
        ):
            self.assertEqual("waiting_input", self.call(True, True)["execution"])
            self.values["zurg.Main::adopt_library_hook"] = True
            self.values["zurg.Main::scan_library_ids"] = "3"
            self.assertEqual("waiting_input", self.call(True, True)["execution"])
            self.values["zurg.Main::scan_library_ids"] = "999"
            self.assertEqual("waiting_input", self.call(True, True)["execution"])
        self.assertEqual(before, native.read_bytes())


class HookReceiverTests(unittest.TestCase):
    def setUp(self):
        self.api = API()
        self.cfg = {
            "url": "http://127.0.0.1:32400",
            "api_key": "private-token",
            "mount_path": "/media",
            "libraries": {"1": ["/media/movies"], "2": ["/media/shows"]},
        }

    def test_scans_only_changed_paths_within_explicit_library_roots(self):
        count = refresh_changed_paths(
            self.cfg,
            [
                "movies/new film",
                "shows/new show",
                "unselected/music",
                "movies/new film",
            ],
            client_factory=self.api.factory,
        )
        self.assertEqual(2, count)
        self.assertEqual(
            ["/library/sections/1/refresh", "/library/sections/2/refresh"],
            [row[3] for row in self.api.calls],
        )
        self.assertEqual(
            "/media/movies/new film", self.api.calls[0][4]["params"]["path"]
        )
        self.assertEqual(
            {"X-Plex-Token": "private-token"}, self.api.calls[0][1]["headers"]
        )
        self.assertNotIn("private-token", str(self.api.calls[0][4]))

    def test_traversal_absolute_backslash_and_control_paths_rejected_before_requests(
        self,
    ):
        for value in (
            "../other",
            "/other",
            "movies/../../other",
            "movie\\other",
            "movie\nname",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                refresh_changed_paths(
                    self.cfg, [value], client_factory=self.api.factory
                )
        self.assertEqual([], self.api.calls)

    def test_no_overlap_or_world_readable_secret_file_never_runs(self):
        self.cfg["mount_path"] = "/unrelated"
        with self.assertRaises(ValueError):
            validate_hook_configuration(self.cfg)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "hook.json"
            path.write_text(json.dumps(self.cfg))
            path.chmod(0o644)
            self.assertEqual(1, main([str(path), "movie"]))
