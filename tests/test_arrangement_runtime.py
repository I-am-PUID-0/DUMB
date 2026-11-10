import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import Mock, patch

from utils.arrangement_control import configure_tautulli
from utils.arrangement_plan import build_plan
from utils.arrangement_preflight import storage_checks
from utils.arrangement_runtime import ArrangementRuntime
from utils.cli_source_capabilities import cli_source_capability

DEFAULTS = json.loads(
    (Path(__file__).resolve().parents[1] / "utils/dumb_config.json").read_text()
)


class RuntimeBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = copy.deepcopy(DEFAULTS)
        self.manager = types.SimpleNamespace(config=self.config, save_config=Mock())
        self.runtime = ArrangementRuntime(
            self.manager, Mock(), Mock(), logging.getLogger(__name__)
        )

    def test_private_authorization_never_allows_cloud_metadata_addresses(self):
        from utils.arrangement_http import validate_endpoint

        for address in ("100.100.100.200", "168.63.129.16", "fd00:ec2::254"):
            with (
                self.subTest(address=address),
                patch("utils.arrangement_http._resolve", return_value=[address]),
            ):
                with self.assertRaises(ValueError):
                    validate_endpoint("http://example.invalid", allow_private=True)

    def test_provider_mount_preflight_checks_the_selected_container_fuse_need(self):
        self.manager.default_config_path = (
            Path(__file__).resolve().parents[1] / "utils/dumb_config.json"
        )
        cases = (
            ("decypharr", None, True),
            ("decypharr", "dfs", True),
            ("decypharr", "rclone", True),
            ("decypharr", "external_rclone", True),  # DUMB's rclone companion
            ("decypharr", "none", False),
            ("altmount", None, True),
            ("altmount", "dfs", True),
            ("altmount", "rclone", True),
            ("altmount", "external_rclone", False),  # External mount ownership
            ("altmount", "none", False),
        )
        for key, mode, required in cases:
            for available in (False, True):
                with self.subTest(service=key, mode=mode, available=available):
                    payload = {"core_services": [{"name": key}]}
                    if mode is not None:
                        payload["core_services"][0]["service_options"] = {
                            key: {"mount_type": mode}
                        }
                    before = copy.deepcopy(self.config)
                    plan = build_plan(payload, self.config, DEFAULTS, plan_id="a" * 32)
                    with (
                        patch(
                            "utils.arrangement_preflight.mount_inventory",
                            return_value=[],
                        ),
                        patch(
                            "utils.arrangement_preflight.storage_checks",
                            return_value=[],
                        ),
                        patch("utils.arrangement_ports.port_checks", return_value=[]),
                        patch(
                            "utils.arrangement_runtime.Path.exists",
                            return_value=available,
                        ),
                    ):
                        checks = self.runtime.preflight(plan, payload)
                    device = [c for c in checks if c["key"] == "fuse_device"]
                    self.assertEqual(len(device), int(required))
                    if required:
                        self.assertIs(device[0]["ok"], available)
                        self.assertTrue(device[0]["critical"])
                    self.assertEqual(self.config, before)
                    self.manager.save_config.assert_not_called()

    def test_protection_key_requires_explicit_consent_and_preserves_policy(self):
        action = {
            "source": "jellyfin",
            "service_key": "jellyfin",
            "action": "protection",
        }
        payload = {
            "arrangement": {"values": {"jellyfin::api_key": "example-dedicated-key"}}
        }
        verified = {"execution": "already_present", "verification": "passed"}
        result = self.runtime._protection_policy(
            action, payload, verified, read_only=False
        )
        self.assertEqual(result["execution"], "waiting_input")
        self.manager.save_config.assert_not_called()
        payload["arrangement"]["values"]["jellyfin::save_protection_key"] = True
        result = self.runtime._protection_policy(
            action, payload, verified, read_only=False
        )
        self.assertEqual(result["execution"], "applied")
        services = self.config["dumb"]["media_protection"]["services"]
        policy = next(
            p
            for p in services
            if p["process_name"] == self.config["jellyfin"]["process_name"]
        )
        self.assertFalse(policy["enabled"])
        policy.update(
            enabled=True,
            protected_mounts=["/mnt/library"],
            stop_when_idle_on_outage=False,
        )
        payload["arrangement"]["values"]["jellyfin::api_key"] = "rotated-example-key"
        self.runtime._protection_policy(action, payload, verified, read_only=False)
        self.assertTrue(policy["enabled"])
        self.assertEqual(policy["protected_mounts"], ["/mnt/library"])
        self.assertFalse(policy["stop_when_idle_on_outage"])

    def test_retained_pulsarr_metadata_input_is_saved_only_by_apply(self):
        action = {"source": "pulsarr", "service_key": "pulsarr", "action": "metadata"}
        payload = {
            "arrangement": {"values": {"pulsarr::tmdb_token": "example-metadata-token"}}
        }
        result = self.runtime._pulsarr_metadata(action, payload, read_only=True)
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse((self.config["pulsarr"].get("env") or {}).get("tmdbApiKey"))
        self.manager.save_config.assert_not_called()
        result = self.runtime._pulsarr_metadata(action, payload, read_only=False)
        self.assertEqual(result["execution"], "applied")
        self.assertEqual(
            self.config["pulsarr"]["env"]["tmdbApiKey"], "example-metadata-token"
        )
        self.assertEqual(result["verification"], "unavailable")
        self.assertEqual(result["evidence"]["restart_required"], "pulsarr")

    def test_retained_pulsarr_metadata_creates_missing_or_empty_env_only_on_apply(self):
        action = {"source": "pulsarr", "service_key": "pulsarr", "action": "metadata"}
        payload = {
            "arrangement": {"values": {"pulsarr::tmdb_token": "example-new-metadata"}}
        }
        for missing in (True, False):
            with self.subTest(missing=missing):
                self.manager.save_config.reset_mock()
                if missing:
                    self.config["pulsarr"].pop("env", None)
                else:
                    self.config["pulsarr"]["env"] = {}
                before = copy.deepcopy(self.config)
                result = self.runtime._pulsarr_metadata(action, payload, read_only=True)
                self.assertEqual(result["execution"], "waiting_input")
                self.assertEqual(self.config, before)
                self.manager.save_config.assert_not_called()
                result = self.runtime._pulsarr_metadata(
                    action, payload, read_only=False
                )
                self.assertEqual(result["execution"], "applied")
                self.assertEqual(
                    self.config["pulsarr"]["env"]["tmdbApiKey"], "example-new-metadata"
                )
                self.manager.save_config.assert_called_once()

    def test_metadata_save_failure_restores_missing_empty_and_existing_environment(
        self,
    ):
        action = {"source": "pulsarr", "service_key": "pulsarr", "action": "metadata"}
        payload = {
            "arrangement": {"values": {"pulsarr::tmdb_token": "example-new-metadata"}}
        }
        for previous in (
            None,
            {},
            {"native_setting": "preserved", "tmdbApiKey": "old-example"},
        ):
            with self.subTest(previous=previous):
                if previous is None:
                    self.config["pulsarr"].pop("env", None)
                else:
                    self.config["pulsarr"]["env"] = copy.deepcopy(previous)
                before = copy.deepcopy(self.config)
                self.manager.save_config.side_effect = RuntimeError(
                    "fixture save failure"
                )
                with self.assertRaises(RuntimeError):
                    self.runtime._pulsarr_metadata(action, payload, read_only=False)
                self.assertEqual(self.config, before)

    def test_authelia_readonly_readiness_never_creates_unrelated_pulsarr_environment(
        self,
    ):
        self.config["authelia"]["public_url"] = ""
        self.config["authelia"]["install_dir"] = self.temp.name
        self.config["pulsarr"].pop("env", None)
        self.runtime.api_state.get_status.return_value = "stopped"
        before = copy.deepcopy(self.config)
        result = self.runtime.inspect(
            {"source": "authelia", "service_key": "authelia", "action": "runtime"}, {}
        )
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(self.config, before)
        self.manager.save_config.assert_not_called()

    def test_cli_parent_configuration_uses_the_reassigned_standalone_port(self):
        from utils import setup

        config = {
            "process_name": "CLI Debrid",
            "battery_mode": "standalone",
            "config_dir": self.temp.name,
            "env": {"CLI_DEBRID_PORT": "5000"},
            "port": 5000,
            "platforms": [],
        }
        manager = Mock()
        manager.find_key_for_process.return_value = ("cli_debrid", None)
        manager.get_instance.return_value = config
        manager.get.side_effect = lambda key, *args: (
            {"enabled": True, "port": 5037}
            if key == "cli_battery"
            else args[0] if args else None
        )
        handler = Mock(setup_tracker=set(), setup_tracker_lock=threading.Lock())
        with (
            patch.object(setup, "CONFIG_MANAGER", manager),
            patch.object(
                setup, "_validate_cli_debrid_source", return_value=(True, None)
            ),
        ):
            result = setup._setup_project_inner(handler, "CLI Debrid", False, True)
        self.assertEqual(result, (True, None))
        self.assertEqual(config["env"]["CLI_DEBRID_BATTERY_PORT"], "5037")
        manager.save_config.assert_called()

    def test_server_config_copy_remediates_existing_broad_permissions(self):
        from utils import setup

        source, target = (
            Path(self.temp.name) / "source.json",
            Path(self.temp.name) / "server.json",
        )
        source.write_text(
            json.dumps(
                {"backendUrl": "http://127.0.0.1:8080", "apiKey": "example-private-key"}
            )
        )
        target.write_text("{}")
        target.chmod(0o644)
        setup.copy_server_config(source, target)
        self.assertEqual(json.loads(target.read_text()), json.loads(source.read_text()))
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        source.write_text("invalid")
        setup.copy_server_config(source, target)
        self.assertEqual(
            json.loads(target.read_text())["apiKey"], "example-private-key"
        )

    def test_retry_filters_successful_primary_services_and_keeps_private_payload_intact(
        self,
    ):
        self.manager.default_config_path = str(
            Path(__file__).resolve().parents[1] / "utils/dumb_config.json"
        )
        starts = []
        process = types.SimpleNamespace(
            UnifiedStartRequest=types.SimpleNamespace(model_validate=lambda data: data),
            _run_startup=lambda request, *_: starts.append(request) or {},
        )
        payload = {
            "core_services": [{"name": "plex"}],
            "optional_services": ["audiobookshelf"],
            "arrangement": {"values": {}},
            "_retry_resources": ["plex"],
        }
        original = copy.deepcopy(payload)
        with patch.dict("sys.modules", {"api.routers.process": process}):
            self.runtime.start(payload)
        self.assertEqual(payload, original)
        self.assertEqual(starts[0]["optional_services"], [])
        self.assertEqual(
            starts[0]["core_services"],
            [{"name": "plex", "service_options": {"plex": {}}}],
        )
        self.assertNotIn("arrangement", starts[0])
        self.assertNotIn("_retry_resources", starts[0])

    def test_retry_of_only_failed_dependency_does_not_reenter_successful_parent_setup(
        self,
    ):
        self.manager.default_config_path = str(
            Path(__file__).resolve().parents[1] / "utils/dumb_config.json"
        )
        self.config["postgres"]["enabled"] = True
        start = Mock()
        process = types.SimpleNamespace(
            UnifiedStartRequest=types.SimpleNamespace(model_validate=lambda data: data),
            _run_startup=start,
        )
        payload = {
            "core_services": [{"name": "riven_backend"}],
            "optional_services": [],
            "_retry_resources": ["postgres"],
        }
        with patch.dict("sys.modules", {"api.routers.process": process}):
            self.runtime.start(payload)
        start.assert_not_called()
        self.runtime.updater.auto_update.assert_called_once_with(
            self.config["postgres"]["process_name"], enable_update=False
        )

    def test_arr_recheck_reads_exact_urlbase_endpoint_category_root_and_permissions(
        self,
    ):
        requests = []
        state = {
            "port": 8080,
            "root": "/mnt/debrid/infinidysk_symlinks/radarr-hd",
            "category": "radarr-hd",
        }

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                requests.append(("GET", self.path))
                self.assert_auth()
                routes = {
                    "/native/api/v3/downloadclient": [
                        {
                            "id": 1,
                            "name": "NzbDAV",
                            "enable": True,
                            "fields": [
                                {"name": "host", "value": "127.0.0.1"},
                                {"name": "port", "value": state["port"]},
                                {"name": "category", "value": state["category"]},
                            ],
                        }
                    ],
                    "/native/api/v3/rootfolder": [{"path": state["root"]}],
                    "/native/api/v3/config/mediamanagement": {
                        "setPermissionsLinux": True,
                        "chmodFolder": "777",
                        "chmodFile": "666",
                    },
                }
                self.respond(routes.get(self.path), 200 if self.path in routes else 404)

            def do_POST(self):
                requests.append(("POST", self.path))
                self.assert_auth()
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if (
                    self.path != "/native/api/v3/downloadclient/test"
                    or body.get("id") != 1
                ):
                    self.respond({}, 400)
                else:
                    self.respond([], 200)

            def assert_auth(self):
                if self.headers.get("X-Api-Key") != "example-arr-key":
                    raise AssertionError("Missing authentication")

            def respond(self, body, status):
                encoded = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def cleanup():
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)
            self.assertFalse(thread.is_alive())

        self.addCleanup(cleanup)
        xml = Path(self.temp.name) / "config.xml"
        xml.write_text(
            "<Config><ApiKey>example-arr-key</ApiKey><UrlBase>/native</UrlBase></Config>"
        )
        cfg = self.config["radarr"]["instances"]["Default"]
        cfg.update(config_file=str(xml), port=server.server_port)
        cfg["instance_name"] = "HD"
        action = {
            "source": "radarr.Default",
            "service_key": "radarr",
            "instance_key": "Default",
        }
        expected = state["root"]
        stub = types.SimpleNamespace(
            expected_arr_root=lambda *_: expected,
            expected_arr_category=lambda *_: "radarr-hd",
        )
        with patch.dict("sys.modules", {"utils.nzbdav_settings": stub}):
            result = self.runtime._inspect_provider_client(action, cfg, "infinidysk")
            self.assertEqual(result["verification"], "passed")
            self.assertTrue(result["evidence"]["permission_readback"])
            state["root"] = "/mnt/wrong-root"
            self.assertEqual(
                self.runtime._inspect_provider_client(action, cfg, "infinidysk")[
                    "verification"
                ],
                "failed",
            )
            state["root"] = expected
            state["port"] = 9999
            self.assertEqual(
                self.runtime._inspect_provider_client(action, cfg, "infinidysk")[
                    "execution"
                ],
                "waiting_input",
            )
            state["port"] = 8080
            state["category"] = "radarr-other-instance"
            self.assertEqual(
                self.runtime._inspect_provider_client(action, cfg, "infinidysk")[
                    "verification"
                ],
                "failed",
            )
        self.assertTrue(
            all(method == "GET" or path.endswith("/test") for method, path in requests)
        )


class StoragePreflightTests(unittest.TestCase):
    def test_port_preview_distinguishes_free_occupied_and_duplicate_listeners(self):
        from utils.arrangement_ports import port_checks

        payload = {
            "core_services": [
                {"name": "radarr", "instance_name": "HD"},
                {"name": "radarr", "instance_name": "UHD"},
            ]
        }
        plan = build_plan(payload, DEFAULTS, DEFAULTS, plan_id="a" * 32)
        current = copy.deepcopy(DEFAULTS)
        before = copy.deepcopy(current)
        checks = port_checks(plan, payload, current, DEFAULTS, available=lambda _: True)
        self.assertEqual([check["ok"] for check in checks], [True, None])
        self.assertEqual(current, before)
        occupied = port_checks(
            plan, payload, current, DEFAULTS, available=lambda _: False
        )
        self.assertTrue(all(check["ok"] is None for check in occupied))
        self.assertIn("host mapping", occupied[0]["detail"])

    def test_remote_mount_stays_unknown_without_stat_or_enumeration(self):
        current = copy.deepcopy(DEFAULTS)
        current["audiobookshelf"]["config_dir"] = "/network/config"
        payload = {"core_services": [], "optional_services": ["audiobookshelf"]}
        plan = build_plan(payload, current, DEFAULTS, plan_id="a" * 32)
        with patch(
            "utils.arrangement_preflight.Path.lstat",
            side_effect=AssertionError("remote path touched"),
        ):
            # Root ancestor is local and may be checked; the remote mount itself
            # must return unknown before any file access.
            with patch(
                "utils.arrangement_preflight._probe",
                wraps=__import__(
                    "utils.arrangement_preflight", fromlist=["_probe"]
                )._probe,
            ):
                checks = storage_checks(
                    plan, payload, current, DEFAULTS, mounts=[("/", "nfs4")]
                )
        self.assertTrue(checks)
        self.assertTrue(all(item["ok"] is None for item in checks))

    def test_existing_local_service_permissions_and_capacity_are_measured(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private"
            path.mkdir(mode=0o700)
            current = copy.deepcopy(DEFAULTS)
            current.update(puid=12345, pgid=12345)
            current["audiobookshelf"]["config_dir"] = str(path)
            payload = {"core_services": [], "optional_services": ["audiobookshelf"]}
            plan = build_plan(payload, current, DEFAULTS, plan_id="a" * 32)
            check = next(
                c
                for c in storage_checks(
                    plan, payload, current, DEFAULTS, mounts=[("/", "overlay")]
                )
                if c["path"] == str(path)
            )
            self.assertIs(check["ok"], False)
            self.assertFalse(check["content_checked"])

    def test_audited_source_modes_and_moving_branch_uncertainty(self):
        self.assertEqual(
            cli_source_capability(
                {"commit_sha": "c971c60336e3f85aad8f0624591885c7c1da83ce"}
            )["battery_mode"],
            "embedded",
        )
        self.assertEqual(
            cli_source_capability(
                {"release_version_enabled": True, "release_version": "v0.6.07"}
            )["battery_mode"],
            "standalone",
        )
        self.assertIsNone(
            cli_source_capability({"branch_enabled": True, "branch": "dev"})[
                "battery_mode"
            ]
        )


class ControlledTautulliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "config.ini"
        self.path.write_text("[General]\napi_key = example-key\n[PMS]\npms_token =\n")
        self.original = self.path.read_bytes()
        self.process = Mock(pid=123456)
        self.process.poll.return_value = None
        self.handler = types.SimpleNamespace(
            process_names={"Tautulli": self.process},
            _process_group_alive=lambda _: False,
        )

        def stop(*_, **__):
            self.process.poll.return_value = 0
            self.handler.process_names.clear()

        self.handler.stop_process = Mock(side_effect=stop)
        self.updater = types.SimpleNamespace(
            auto_update=Mock(return_value=(object(), None))
        )
        self.config = {
            "tautulli": {"process_name": "Tautulli", "config_file": str(self.path)}
        }
        self.action = {
            "source": "tautulli",
            "service_key": "tautulli",
            "action": "backend_connection",
        }
        self.payload = {
            "arrangement": {"values": {"tautulli::restart_after_config": True}}
        }
        self.inspect = Mock(
            side_effect=[{"execution": "planned"}, {"verification": "passed"}]
        )

    def apply(self, action, config, payload):
        self.assertTrue(payload["_service_stopped"])
        self.assertIsNotNone(self.process.poll())
        self.path.write_text("[PMS]\npms_token = example-token\n")
        return {"execution": "waiting_dependency"}

    def execute(self, **overrides):
        args = {
            "handler": self.handler,
            "updater": self.updater,
            "inspect": self.inspect,
            "apply": self.apply,
            **overrides,
        }
        with patch(
            "utils.arrangement_control.os.getpgid", return_value=os.getpgrp() + 100000
        ):
            return configure_tautulli(self.action, self.config, self.payload, **args)

    def test_applies_only_after_verified_stop_and_restarts_exact_configured_target(
        self,
    ):
        result = self.execute()
        self.assertEqual(result["verification"], "passed")
        self.updater.auto_update.assert_called_once_with(
            "Tautulli", enable_update=False
        )
        self.assertNotIn("_service_stopped", self.payload)

    def test_failed_stop_never_writes_configuration(self):
        self.handler.stop_process.side_effect = None
        apply = Mock()
        result = self.execute(apply=apply)
        self.assertEqual(result["execution"], "failed")
        apply.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)
        self.updater.auto_update.assert_not_called()

    def test_failed_apply_restores_previous_private_configuration_and_restarts(self):
        def fail(*args):
            self.apply(*args)
            raise RuntimeError("example failure")

        result = self.execute(apply=fail)
        self.assertEqual(result["execution"], "failed")
        self.assertEqual(self.path.read_bytes(), self.original)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.updater.auto_update.assert_called_once_with(
            "Tautulli", enable_update=False
        )

    def test_already_verified_connection_never_stops_or_restarts(self):
        result = self.execute(
            inspect=Mock(
                return_value={"execution": "already_present", "verification": "passed"}
            )
        )
        self.assertEqual(result["verification"], "passed")
        self.handler.stop_process.assert_not_called()
        self.updater.auto_update.assert_not_called()

    def test_compensation_keeps_unrelated_settings_flushed_during_shutdown(self):
        stopped_snapshot = self.original + b"\n[Notifications]\non_stop = true\n"
        previous_stop = self.handler.stop_process.side_effect

        def flush_and_stop(*args, **kwargs):
            self.path.write_bytes(stopped_snapshot)
            previous_stop(*args, **kwargs)

        self.handler.stop_process.side_effect = flush_and_stop

        def fail(*args):
            self.apply(*args)
            raise RuntimeError("example failure")

        self.execute(apply=fail)
        self.assertEqual(self.path.read_bytes(), stopped_snapshot)
        self.assertFalse(Path(str(self.path) + ".dumb-setup-recovery").exists())

    def test_untracked_restart_failure_retains_private_baseline_without_live_overwrite(
        self,
    ):
        self.updater.auto_update.return_value = (None, "example failure")
        result = self.execute()
        self.assertEqual(result["execution"], "failed")
        backup = Path(str(self.path) + ".dumb-setup-recovery")
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertIn("example-token", self.path.read_text())

    def test_input_needed_after_restart_does_not_become_an_automatic_retry(self):
        result = self.execute(
            inspect=Mock(
                side_effect=[
                    {"execution": "planned"},
                    {
                        "execution": "waiting_input",
                        "verification": "not_checked",
                        "retryable": False,
                        "reason": "Enable the application's API.",
                    },
                ]
            )
        )
        self.assertEqual(result["execution"], "waiting_input")
        self.assertFalse(result["retryable"])


class JellyfinProtectionCompatibilityTests(unittest.TestCase):
    def adapter(self):
        from utils.media_protection import MediaBrowserAdapter

        return MediaBrowserAdapter(
            "jellyfin",
            "Jellyfin",
            {"port": 8096},
            {"api_key": "example-protection-key"},
            Mock(),
        )

    def response(self, value):
        return Mock(content=b"{}", json=Mock(return_value=value))

    def test_camelcase_playback_and_running_scan_remain_busy_with_canonical_auth(self):
        from utils import media_protection

        with patch.object(
            media_protection.requests,
            "request",
            create=True,
            side_effect=[
                self.response(
                    [
                        {
                            "id": "session",
                            "nowPlayingItem": {
                                "id": "playing",
                                "name": "Example video",
                            },
                        }
                    ]
                ),
                self.response(
                    [{"id": "scan", "key": "RefreshLibrary", "state": "Running"}]
                ),
            ],
        ) as request:
            result = self.adapter().activity()
        self.assertEqual(result["state"], "busy")
        self.assertEqual((result["active_sessions"], result["active_scans"]), (1, 1))
        self.assertEqual(
            request.call_args_list[0].kwargs["headers"]["Authorization"],
            'MediaBrowser Token="example-protection-key"',
        )

    def test_conflicting_activity_aliases_fail_closed(self):
        from utils import media_protection

        with patch.object(
            media_protection.requests,
            "request",
            create=True,
            return_value=self.response(
                [{"NowPlayingItem": None, "nowPlayingItem": {"id": "playing"}}]
            ),
        ):
            result = self.adapter().activity()
        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["active_sessions"])

    def test_unknown_numeric_scan_state_cannot_be_treated_as_idle(self):
        from utils import media_protection

        with patch.object(
            media_protection.requests,
            "request",
            create=True,
            side_effect=[
                self.response([]),
                self.response([{"key": "RefreshLibrary", "state": 2}]),
            ],
        ):
            self.assertEqual(self.adapter().activity()["state"], "unknown")

    def test_header_validation_does_not_echo_bad_private_credentials(self):
        adapter = self.adapter()
        adapter.api_key = "example-secret\r\ninvalid"
        result = adapter.activity()
        self.assertEqual(result["state"], "unknown")
        self.assertNotIn("example-secret", json.dumps(result))

    def test_scan_guard_preserves_unknown_options_in_camelcase_library_response(self):
        from utils import media_protection

        values = [
            self.response([]),
            self.response(
                [
                    {
                        "itemId": "library",
                        "name": "Movies",
                        "locations": ["/media"],
                        "libraryOptions": {
                            "enableRealtimeMonitor": True,
                            "customVendorOption": "preserve",
                        },
                    }
                ]
            ),
            self.response(None),
        ]
        with patch.object(
            media_protection.requests, "request", side_effect=values, create=True
        ) as request:
            snapshot = self.adapter().enter_scan_guard()
        self.assertEqual(snapshot["libraries"][0]["id"], "library")
        written = request.call_args_list[-1].kwargs["json"]["LibraryOptions"]
        self.assertFalse(written["EnableRealtimeMonitor"])
        self.assertNotIn("enableRealtimeMonitor", written)
        self.assertEqual(written["customVendorOption"], "preserve")


if __name__ == "__main__":
    unittest.main()
