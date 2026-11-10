import copy
import json
import tempfile
import unittest
from unittest.mock import patch
from defusedxml.common import DefusedXmlException

from utils.arrangement_http import ApiResponse
from utils.arrangement_workflow import (
    NativeWorkflow,
    ProbeError,
    WorkflowProbe,
    _decoder,
    _digest,
    _nzb,
    workflow_action,
)

ACTION = {
    "source": "radarr.Test",
    "service_key": "radarr",
    "action": "workflow_test",
    "_setup_run_id": "example-run",
}
OPTIONS = {
    "provider": "infinidysk",
    "media_server": "jellyfin",
    "source_url": "https://example.invalid/test.nzb",
    "item_id": 7,
}
FRAME = b"P6\n2 2\n255\n" + bytes((255, 0, 0)) * 4


class NativeFixture:
    def __init__(self):
        self.enqueues = self.imports = self.cleanups = self.refreshes = 0
        self.ambiguous = self.cleanup_failure = False

    def connect(self, write=False):
        pass

    def prepare(self, write=False, marker="DUMB.Setup.Probe"):
        return {
            "binding": {"arr": "one"},
            "path": "/media/test",
            "files": [],
            "title": "Example",
            "job_name": "Example.Movie.2020." + marker,
        }

    def binding(self):
        return {"arr": "one"}

    def enqueue(self, manifest, marker):
        self.enqueues += 1
        if self.ambiguous:
            raise ProbeError("The enqueue response was lost.")
        return "job-one"

    def recover_job(self, marker):
        return "job-one"

    def downloaded(self, record):
        return {"path": "/downloads/test", "job_id": record["job_id"]}

    def begin_import(self, record, checkpoint):
        if not record.get("import_intent"):
            self.imports += 1
            record["import_intent"] = {"download_id": record["job_id"]}
            checkpoint()
        record["command_id"] = 3
        checkpoint()

    def imported(self, record):
        return {"id": 5, "path": "/media/test/test.ppm", "size": len(FRAME)}

    def refresh_media(self, record):
        self.refreshes += 1

    def playback(self, record):
        return FRAME

    def cleanup(self, record):
        self.cleanups += 1
        if self.cleanup_failure:
            raise ProbeError("Cleanup readback is pending.")


class WorkflowJournalTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.native = NativeFixture()
        self.payload = {
            "arrangement": {
                "values": {"radarr.Test::workflow_probe": copy.deepcopy(OPTIONS)}
            }
        }
        self.addCleanup(patch.stopall)
        patch(
            "utils.arrangement_workflow.NativeWorkflow", return_value=self.native
        ).start()
        patch(
            "utils.arrangement_workflow._nzb", return_value=(b"manifest", 400)
        ).start()

    def act(self, operation, **kwargs):
        return workflow_action(
            ACTION, {}, self.payload, operation, directory=self.directory.name, **kwargs
        )

    def authorize(self):
        preview = self.act("preview")
        self.payload["arrangement"]["values"]["radarr.Test::workflow_preview_id"] = (
            preview["evidence"]["preview_id"]
        )
        self.payload["arrangement"]["values"]["radarr.Test::workflow_probe"][
            "authorize_download_import_scan"
        ] = True
        self.payload["arrangement"]["values"]["radarr.Test::workflow_probe"][
            "authorize_reserved_test_item"
        ] = True
        return preview

    def test_preview_and_recheck_never_enqueue(self):
        self.assertEqual(self.act("preview")["execution"], "planned")
        self.act("recheck")
        self.act("retry")
        self.assertEqual(
            (self.native.enqueues, self.native.imports, self.native.refreshes),
            (0, 0, 0),
        )

    def test_apply_requires_exact_preview_and_separate_consent(self):
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.authorize()
        self.payload["arrangement"]["values"]["radarr.Test::workflow_probe"][
            "authorize_download_import_scan"
        ] = False
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.assertEqual(self.native.enqueues, 0)

    def test_real_decoder_pipeline_and_repeated_apply_does_not_enqueue_again(self):
        self.authorize()
        result = self.act("apply")
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(result["evidence"]["decoded_frames"], 1)
        self.assertTrue(result["evidence"]["cleanup_complete"])
        self.act("apply")
        self.act("recheck")
        self.assertEqual((self.native.enqueues, self.native.imports), (1, 1))

    def test_native_auto_import_can_finish_after_provider_history_is_removed(self):
        self.authorize()
        self.native.downloaded = lambda record: None
        result = self.act("apply")
        self.assertEqual(result["verification"], "passed", result)
        self.assertEqual(self.native.imports, 0)
        self.assertEqual(self.native.enqueues, 1)
        self.assertTrue(result["evidence"]["cleanup_complete"])
        self.assertIn("Native Arr history", str(result["evidence"]["checks"]))

    def test_changed_scope_cannot_reuse_preview_or_completed_result(self):
        self.authorize()
        self.payload["arrangement"]["values"]["radarr.Test::workflow_probe"][
            "item_id"
        ] = 8
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.assertEqual(self.native.enqueues, 0)

    def test_ambiguous_enqueue_resumes_from_journal_without_second_post(self):
        self.authorize()
        self.native.ambiguous = True
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.assertEqual(self.act("apply")["verification"], "passed")
        self.assertEqual(self.native.enqueues, 1)

    def test_failed_cleanup_blocks_new_preview_and_cleanup_only_never_enqueues(self):
        self.authorize()
        self.native.cleanup_failure = True
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.assertFalse(self.act("preview")["evidence"]["cleanup_complete"])
        self.native.cleanup_failure = False
        self.payload["arrangement"]["values"]["radarr.Test::workflow_cleanup"] = True
        result = self.act("apply")
        self.assertEqual(result["evidence"]["stage"], "cleaned")
        self.assertTrue(result["evidence"]["cleanup_complete"])
        self.assertNotEqual(result["verification"], "passed")
        self.assertEqual(self.native.enqueues, 1)

    def test_audiobookshelf_library_scan_requires_consent_before_enqueue(self):
        self.payload["arrangement"]["values"]["radarr.Test::workflow_probe"][
            "media_server"
        ] = "audiobookshelf"
        preview = self.authorize()
        self.assertIn(
            "containing Audiobookshelf library",
            " ".join(preview["evidence"]["side_effects"]),
        )
        self.assertEqual(self.act("apply")["verification"], "failed")
        self.assertEqual(self.native.enqueues, 0)

    def test_cancelled_workflow_attempts_owned_cleanup(self):
        self.authorize()
        result = self.act("apply", cancelled=lambda: self.native.enqueues > 0)
        self.assertEqual(result["verification"], "failed")
        self.assertTrue(result["evidence"]["cleanup_complete"])
        self.assertEqual(self.native.cleanups, 1)

    def test_already_cancelled_test_does_not_enqueue(self):
        self.authorize()
        self.assertEqual(
            self.act("apply", cancelled=lambda: True)["verification"], "failed"
        )
        self.assertEqual(self.native.enqueues, 0)


class NativeOwnershipTests(unittest.TestCase):
    def native(self):
        native = NativeWorkflow(
            ACTION,
            {},
            {},
            {**OPTIONS, "max_download_bytes": 4096, "cleanup_after": True},
            None,
        )
        native.connect = lambda write=False: None
        native.binding = lambda: {"same": True}
        native.item = lambda: {"id": 7}
        return native

    def test_concurrent_existing_file_is_not_imported(self):
        native = self.native()
        native.files = lambda item: [{"id": 9}]
        with self.assertRaisesRegex(ProbeError, "preserved"):
            native.begin_import(
                {"baseline": {"path": "/media/test"}, "job_id": "controlled"},
                lambda: None,
            )

    def test_import_ownership_requires_native_download_history(self):
        native = self.native()
        native.files = lambda item: [
            {
                "id": 9,
                "path": "/media/test/movie.mkv",
                "size": 100,
                "dateAdded": "example",
            }
        ]
        record = {"job_id": "unique-job", "baseline": {"path": "/media/test"}}
        with patch(
            "utils.arrangement_workflow._get",
            return_value={
                "records": [
                    {
                        "downloadId": "other-job",
                        "data": {"importedPath": "/media/test/movie.mkv"},
                    }
                ]
            },
        ):
            self.assertIsNone(native.imported(record))
        with patch(
            "utils.arrangement_workflow._get",
            return_value={
                "records": [
                    {
                        "downloadId": "unique-job",
                        "data": {"importedPath": "/media/test/movie.mkv"},
                    }
                ]
            },
        ):
            self.assertEqual(native.imported(record)["id"], 9)

    def test_folder_only_import_requires_exact_uuid_source_file_id_and_size(self):
        native = self.native()
        native.files = lambda item: [
            {
                "id": 9,
                "path": "/media/test/movie.mkv",
                "size": 100,
                "dateAdded": "example",
            }
        ]
        record = {
            "job_id": "unique-job",
            "marker": "DUMB.Setup.Probe.example",
            "baseline": {"path": "/media/test"},
            "import_intent": {"path": "/downloads/DUMB.Setup.Probe.example/movie.mkv"},
        }
        event = {
            "downloadId": None,
            "data": {
                "droppedPath": record["import_intent"]["path"],
                "importedPath": "/media/test/movie.mkv",
                "fileId": "9",
                "size": "100",
            },
        }
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            self.assertEqual(native.imported(record)["id"], 9)
        for name, value in (
            ("droppedPath", "/downloads/other/movie.mkv"),
            ("fileId", "8"),
            ("size", "99"),
        ):
            changed = copy.deepcopy(event)
            changed["data"][name] = value
            with (
                self.subTest(name=name),
                patch(
                    "utils.arrangement_workflow._get",
                    return_value={"records": [changed]},
                ),
            ):
                self.assertIsNone(native.imported(record))
        event["downloadId"] = "another-job"
        with patch(
            "utils.arrangement_workflow._get", return_value={"records": [event]}
        ):
            self.assertIsNone(native.imported(record))

    def test_cleanup_refuses_file_changed_outside_probe(self):
        native = self.native()
        row = {
            "id": 9,
            "path": "/media/test/movie.mkv",
            "size": 101,
            "dateAdded": "example",
        }
        requests = []

        class Client:
            def request(self, method, path):
                requests.append((method, path))
                return ApiResponse(200, json.dumps(row).encode())

        native.arr = Client()
        original = {**row, "size": 100}
        record = {
            "baseline": {"binding": {"same": True}},
            "imported": {"id": 9, "fingerprint": _digest(original)},
        }
        with self.assertRaisesRegex(ProbeError, "changed outside"):
            native.cleanup(record)
        self.assertEqual([method for method, _ in requests], ["GET"])

    def test_ambiguous_native_import_intent_never_resubmits_command(self):
        native = self.native()
        native.begin_import(
            {"import_intent": {"download_id": "unique-job"}},
            lambda: self.fail("No second write"),
        )


class BoundedContentTests(unittest.TestCase):
    def test_decoder_rejects_malformed_media_and_cancellation(self):
        with self.assertRaises(ProbeError):
            _decoder(b"not a video")
        with self.assertRaises(ProbeError):
            _decoder(FRAME, cancelled=lambda: True)

    def test_manifest_rejects_multiple_files_and_oversize_before_enqueue(self):
        from utils.arrangement_workflow import _options

        options = _options(
            ACTION,
            {"arrangement": {"values": {"radarr.Test::workflow_probe": OPTIONS}}},
        )
        for manifest in (
            b'<nzb><file/><file/><segment bytes="5">id</segment></nzb>',
            b'<nzb><file><segment bytes="9000000">id</segment></file></nzb>',
            b'<!DOCTYPE nzb [<!ENTITY e SYSTEM "file:///etc/passwd">]><nzb>&e;</nzb>',
        ):

            class Client:
                def request(self, *args, **kwargs):
                    return ApiResponse(200, manifest)

            with (
                self.subTest(manifest=manifest),
                self.assertRaises((ValueError, DefusedXmlException)),
            ):
                _nzb(options, lambda *args, **kwargs: Client())
