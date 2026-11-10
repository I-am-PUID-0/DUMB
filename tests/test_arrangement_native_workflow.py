import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from utils.arrangement_http import ApiResponse
from utils.arrangement_workflow import NativeWorkflow, ProbeError, _digest


class NativeWorkflowContractTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        (root / "auth.json").write_text(
            json.dumps({"api_token": "private-native-provider"})
        )
        (root / "config.json").write_text(json.dumps({"url_base": "/dec"}))
        self.action = {
            "source": "external_arr.movies",
            "service_key": "radarr",
            "action": "workflow_test",
        }
        self.config = {
            "decypharr": {
                "enabled": True,
                "port": 8282,
                "config_dir": str(root),
                "config_file": str(root / "config.json"),
            },
            "jellyfin": {"enabled": True},
        }
        self.payload = {
            "arrangement": {
                "values": {
                    "setup::external_arrs": [
                        {
                            "id": "movies",
                            "service_key": "radarr",
                            "url": "https://arr.example.invalid",
                            "api_key": "private-arr-key",
                            "authorize_changes": True,
                        }
                    ],
                    "external_arr.movies::decypharr::client_url": "https://provider.example.invalid:8443/proxy/dec",
                }
            }
        }
        self.options = {
            "provider": "decypharr",
            "media_server": "jellyfin",
            "item_id": 7,
            "max_download_bytes": 4096,
            "cleanup_after": True,
        }
        self.calls = []
        self.clients = [
            {
                "id": 11,
                "enable": True,
                "implementation": "Sabnzbd",
                "fields": [
                    {"name": key, "value": value}
                    for key, value in (
                        ("host", "provider.example.invalid"),
                        ("port", 8443),
                        ("useSsl", True),
                        ("urlBase", "/proxy/dec/sabnzbd"),
                        ("apiKey", "********"),
                        ("movieCategory", "movies-test"),
                    )
                ],
            }
        ]
        self.mappings = [
            {
                "host": "provider.example.invalid",
                "remotePath": "/remote/downloads",
                "localPath": "/mounted/downloads",
            }
        ]

    def factory(self, host, **kwargs):
        fixture = self

        class Client:
            base_url = host
            headers = kwargs.get("headers", {})

            def request(self, method, route, **options):
                fixture.calls.append((host, method, route, copy.deepcopy(options)))
                if route.endswith("/system/status"):
                    document = {"appName": "Radarr", "version": "6.4.4"}
                elif route.endswith("/downloadclient"):
                    document = fixture.clients
                elif route.endswith("/remotepathmapping"):
                    document = fixture.mappings
                elif route == "/api":
                    document = {"version": "4.5.0"}
                else:
                    return ApiResponse(404, b"{}")
                return ApiResponse(200, json.dumps(document).encode())

        return Client()

    def native(self):
        return NativeWorkflow(
            self.action, self.config, self.payload, self.options, self.factory
        )

    def test_external_client_matches_public_base_but_uses_private_native_provider_key(
        self,
    ):
        native = self.native()
        with patch(
            "utils.arrangement_media._client",
            return_value=SimpleNamespace(base_url="http://media", headers={}),
        ):
            native.connect()
        self.assertEqual(native.provider_key, "private-native-provider")
        self.assertEqual(native.provider.base_url, "http://127.0.0.1:8282/dec/sabnzbd")
        native.sab("version")
        self.assertEqual(
            self.calls[-1][3]["params"]["apikey"], "private-native-provider"
        )
        self.assertNotIn("private-native-provider", json.dumps(native.binding()))
        self.assertEqual(
            native.import_folder("/remote/downloads/job/file"),
            "/mounted/downloads/job/file",
        )

    def test_native_remote_mapping_uses_longest_boundary_and_refuses_ambiguity(self):
        native = self.native()
        native.remote_mappings = [
            {"remotePath": "/remote", "localPath": "/mounted"},
            {"remotePath": "/remote/downloads", "localPath": "/library/downloads"},
        ]
        self.assertEqual(
            native.import_folder("/remote/downloads/job"), "/library/downloads/job"
        )
        self.assertEqual(native.import_folder("/remote-other/job"), "/remote-other/job")
        native.remote_mappings.append(
            {"remotePath": "/remote/downloads", "localPath": "/different"}
        )
        with self.assertRaises(ProbeError):
            native.import_folder("/remote/downloads/job")

    def observed_native(self, files):
        native = self.native()
        native.connect = lambda write=False: None
        native.binding = lambda: {"fixture": True}
        native.item = lambda: {"id": 7}
        native.files = lambda item: files
        native.remote_mappings = self.mappings
        native.refresh_media = lambda record, removed=False: None
        native._playback_endpoint = lambda record: None
        native._jobs = lambda marker: []
        return native

    def record(self):
        return {
            "job_id": "own-job",
            "marker": "unique-marker",
            "download": {"path": "/remote/downloads/job"},
            "baseline": {"binding": {"fixture": True}, "path": "/media/test"},
        }

    def test_observe_only_import_never_submits_native_commands(self):
        native = self.observed_native([])
        native.arr = SimpleNamespace(
            request=lambda *args, **kwargs: self.fail(
                "No forced import request is permitted"
            )
        )
        record, checkpoints = self.record(), []
        native.begin_import(record, lambda: checkpoints.append(copy.deepcopy(record)))
        self.assertTrue(record["import_intent"]["automatic"])
        self.assertEqual(record["import_folder"], "/mounted/downloads/job")
        self.assertEqual(len(checkpoints), 1)

    def test_automatic_existing_file_is_adopted_only_with_exact_native_history(self):
        row = {
            "id": 9,
            "path": "/media/test/movie.mkv",
            "size": 100,
            "dateAdded": "fixture",
        }
        native = self.observed_native([row])
        native.arr = SimpleNamespace()
        record = self.record()
        event = {"downloadId": "own-job", "data": {"importedPath": row["path"]}}
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            native.begin_import(record, lambda: None)
        self.assertEqual(record["imported"]["id"], 9)
        record = self.record()
        event["downloadId"] = "someone-elses-job"
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            with self.assertRaisesRegex(ProbeError, "preserved"):
                native.begin_import(
                    record, lambda: self.fail("Unowned file must not be adopted")
                )
        self.assertNotIn("imported", record)

    def test_fast_auto_import_cleanup_discovers_owned_file_without_import_intent(self):
        row = {
            "id": 9,
            "path": "/media/test/movie.mkv",
            "size": 100,
            "dateAdded": "fixture",
        }
        files = [row]
        native = self.observed_native(files)
        calls = []

        class Client:
            def request(self, method, path):
                calls.append((method, path))
                if method == "DELETE":
                    files.clear()
                    return ApiResponse(200, b"{}")
                return (
                    ApiResponse(200, json.dumps(row).encode())
                    if files
                    else ApiResponse(404, b"{}")
                )

        native.arr = Client()
        event = {"downloadId": "own-job", "data": {"importedPath": row["path"]}}
        record = self.record()
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            native.cleanup(record)
        self.assertEqual(record["imported"]["fingerprint"], _digest(row))
        self.assertEqual(
            calls,
            [
                ("GET", "/api/v3/moviefile/9"),
                ("DELETE", "/api/v3/moviefile/9"),
                ("GET", "/api/v3/moviefile/9"),
            ],
        )

    def test_active_or_incompletely_observed_queue_preserves_provider_source(self):
        native = self.observed_native([])
        native.arr = SimpleNamespace()
        native._jobs = lambda marker: self.fail(
            "Provider cleanup must wait for a fully observed idle Arr"
        )
        for queue in (
            {"totalRecords": 1, "records": [{"downloadId": "own-job"}]},
            {"totalRecords": 2, "records": [{"downloadId": "another-job"}]},
            {"totalRecords": True, "records": []},
            {"records": []},
        ):
            with (
                self.subTest(queue=queue),
                patch("utils.arrangement_workflow._get", return_value=queue),
            ):
                with self.assertRaisesRegex(ProbeError, "preserved"):
                    native.cleanup(self.record())

    def test_prepare_requires_complete_queue_and_reserved_unmonitored_item(self):
        native = self.observed_native([])
        native.item = lambda: {
            "id": 7,
            "title": "Movie",
            "year": 2020,
            "path": "/media/test",
            "monitored": False,
        }
        native.sab = lambda *args, **kwargs: {}
        native.client_record = {"id": 11}
        native.media = SimpleNamespace()
        queues = (
            {"totalRecords": 2, "records": [{"movieId": 8}]},
            {"totalRecords": 1, "records": [{"movieId": 7}]},
            {"totalRecords": True, "records": []},
        )
        for queue in queues:

            def get(_client, route, **kwargs):
                return (
                    {"appName": "Radarr"} if route.endswith("/system/status") else queue
                )

            with (
                self.subTest(queue=queue),
                patch("utils.arrangement_workflow._get", side_effect=get),
                patch("utils.arrangement_workflow._request") as writes,
            ):
                with self.assertRaises(ProbeError):
                    native.prepare()
                writes.assert_not_called()

    def test_cleanup_does_not_delete_unowned_automatic_file(self):
        row = {
            "id": 9,
            "path": "/media/test/movie.mkv",
            "size": 100,
            "dateAdded": "fixture",
        }
        native = self.observed_native([row])
        native.arr = SimpleNamespace(
            request=lambda *args, **kwargs: self.fail("No deletion before ownership")
        )
        event = {"downloadId": "different-job", "data": {"importedPath": row["path"]}}
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            with self.assertRaisesRegex(ProbeError, "preserved"):
                native.cleanup(self.record())


class FastNativeCompletionTests(unittest.TestCase):
    def test_provider_history_removed_after_native_import_does_not_trigger_reenqueue_or_forced_import(
        self,
    ):
        from utils.arrangement_workflow import workflow_action

        class Native:
            def __init__(self):
                self.enqueues = self.cleanups = 0

            def connect(self, write=False):
                pass

            def binding(self):
                return {"fixture": True}

            def prepare(self, write=False, marker="fixture"):
                return {
                    "binding": self.binding(),
                    "path": "/media/test",
                    "files": [],
                    "job_name": "Movie (2020) [" + marker + "]",
                }

            def enqueue(self, *args):
                self.enqueues += 1
                return "own-job"

            def downloaded(self, record):
                return None

            def imported(self, record):
                return {
                    "id": 9,
                    "path": "/media/test/movie.mkv",
                    "size": 100,
                    "fingerprint": "native-proof",
                }

            def begin_import(self, *args):
                raise AssertionError(
                    "Already proved automatic import must not submit another import"
                )

            def refresh_media(self, record):
                pass

            def playback(self, record):
                return b"fixture"

            def cleanup(self, record):
                self.cleanups += 1

        native = Native()
        action = {
            "source": "radarr.Test",
            "service_key": "radarr",
            "action": "workflow_test",
            "_setup_run_id": "fast-native-fixture",
        }
        values = {
            "radarr.Test::workflow_probe": {
                "provider": "infinidysk",
                "media_server": "jellyfin",
                "item_id": 7,
                "source_url": "https://example.invalid/test.nzb",
            }
        }
        payload = {"arrangement": {"values": values}}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("utils.arrangement_workflow.NativeWorkflow", return_value=native),
            patch("utils.arrangement_workflow._nzb", return_value=(b"fixture", 100)),
            patch("utils.arrangement_workflow._decoder", return_value=1),
        ):
            preview = workflow_action(
                action, {}, payload, "preview", directory=directory
            )
            values["radarr.Test::workflow_preview_id"] = preview["evidence"][
                "preview_id"
            ]
            values["radarr.Test::workflow_probe"].update(
                authorize_download_import_scan=True, authorize_reserved_test_item=True
            )
            result = workflow_action(action, {}, payload, "apply", directory=directory)
            self.assertEqual(result["verification"], "passed", result)
            self.assertTrue(
                any(
                    "Native Arr history" in row["label"]
                    for row in result["evidence"]["checks"]
                )
            )
            self.assertEqual(
                workflow_action(action, {}, payload, "apply", directory=directory)[
                    "verification"
                ],
                "passed",
            )
            self.assertEqual((native.enqueues, native.cleanups), (1, 1))
