"""The native global scan endpoint requires the reviewed library scope."""

import copy
import json
import unittest

from utils.arrangement_http import ApiResponse
from utils.arrangement_workflow import NativeWorkflow, ProbeError


class MediaInventory:
    def __init__(self, service, libraries):
        self.service = service
        self.libraries = libraries
        self.calls = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, copy.deepcopy(kwargs)))
        inventory = (
            "/Library/VirtualFolders"
            if self.service == "jellyfin"
            else "/Library/VirtualFolders/Query"
        )
        if method == "GET" and path == inventory:
            document = copy.deepcopy(self.libraries)
            if self.service == "emby":
                document = {"Items": document}
            return ApiResponse(200, json.dumps(document).encode())
        if method == "POST" and path in {
            "/Library/Refresh",
            "/Library/Media/Updated",
        }:
            return ApiResponse(204)
        raise AssertionError((method, path))

    def global_scans(self):
        return [call for call in self.calls if call[:2] == ("POST", "/Library/Refresh")]


class WorkflowScanScopeTests(unittest.TestCase):
    path = "/media/movies/Reserved/Reserved.mkv"

    def native(self, service, *, containing_consent=False, server_consent=False):
        native = NativeWorkflow(
            {"source": "radarr.Test", "service_key": "radarr"},
            {},
            {},
            {
                "media_server": service,
                "authorize_library_scan": containing_consent,
                "authorize_server_scan": server_consent,
            },
            None,
        )
        inventory = MediaInventory(
            service, [{"ItemId": "movies", "Locations": ["/media/movies"]}]
        )
        native.media = inventory
        record = {
            "imported": {"path": self.path},
            "baseline": {"scan_scope": native._scan_scope(self.path)},
        }
        return native, inventory, record

    def test_single_library_without_scan_consent_only_sends_exact_change(self):
        for service in ("jellyfin", "emby"):
            with self.subTest(service=service):
                native, inventory, record = self.native(service)
                native.refresh_media(record)
                self.assertEqual(inventory.global_scans(), [])
                self.assertEqual(
                    inventory.calls[-1],
                    (
                        "POST",
                        "/Library/Media/Updated",
                        {
                            "json": {
                                "Updates": [
                                    {"Path": self.path, "UpdateType": "Created"}
                                ]
                            }
                        },
                    ),
                )

    def test_single_library_scan_requires_and_honors_containing_consent(self):
        for service in ("jellyfin", "emby"):
            with self.subTest(service=service):
                native, inventory, record = self.native(
                    service, containing_consent=True
                )
                native.refresh_media(record)
                self.assertEqual(len(inventory.global_scans()), 1)

    def test_multiple_libraries_need_separate_server_scan_consent(self):
        for service in ("jellyfin", "emby"):
            with self.subTest(service=service):
                native, inventory, record = self.native(
                    service, containing_consent=True
                )
                inventory.libraries.append(
                    {"itemId": "shows", "locations": ["/media/shows"]}
                )
                record["baseline"]["scan_scope"] = native._scan_scope(self.path)
                with self.assertRaisesRegex(ProbeError, "all configured libraries"):
                    native.refresh_media(record)
                self.assertEqual(inventory.global_scans(), [])
                native.options["authorize_server_scan"] = True
                native.refresh_media(record)
                self.assertEqual(len(inventory.global_scans()), 1)

    def test_inventory_changes_after_preview_refuse_global_post_even_with_consent(self):
        for service in ("jellyfin", "emby"):
            for mutation in ("new_library", "different_paths", "different_identity"):
                with self.subTest(service=service, mutation=mutation):
                    native, inventory, record = self.native(
                        service, containing_consent=True, server_consent=True
                    )
                    if mutation == "new_library":
                        inventory.libraries.append(
                            {"ItemId": "shows", "Locations": ["/media/shows"]}
                        )
                    elif mutation == "different_paths":
                        inventory.libraries[0]["Locations"].append("/media/other")
                    else:
                        inventory.libraries[0]["ItemId"] = "replacement-library"
                    with self.assertRaisesRegex(
                        ProbeError, "scope changed after preview"
                    ):
                        native.refresh_media(record)
                    self.assertEqual(inventory.global_scans(), [])


if __name__ == "__main__":
    unittest.main()
