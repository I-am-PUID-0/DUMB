import unittest

from tests.test_arrangement_media import FakeMediaServer
from utils import arrangement_media as media


class ExplicitInitialScanTests(unittest.TestCase):
    def test_jellyfin_and_emby_scan_only_when_explicitly_selected_on_creation(self):
        for service in ("jellyfin", "emby"):
            for requested in (False, True):
                with self.subTest(service=service, requested=requested):
                    server = FakeMediaServer(service)
                    spec = {
                        "name": "Movies",
                        "media_type": "movie",
                        "paths": ["/media/movies"],
                        "create_if_missing": True,
                    }
                    if requested:
                        spec["scan_on_create"] = True
                    payload = {
                        "arrangement": {
                            "values": {
                                service + "::api_key": "private-fixture",
                                service + "::libraries": [spec],
                            }
                        }
                    }
                    action = {
                        "service_key": service,
                        "source": service,
                        "action": "libraries",
                    }
                    config = {service: {"enabled": True, "port": 8096}}
                    result = media._libraries(
                        action, config, payload, apply=True, factory=server.factory
                    )
                    self.assertEqual(result["verification"], "passed", result)
                    posts = [row for row in server.calls if row[0] == "POST"]
                    self.assertEqual(len(posts), 1)
                    if service == "jellyfin":
                        self.assertEqual(
                            posts[0][2]["params"]["refreshLibrary"],
                            "true" if requested else "false",
                        )
                    else:
                        self.assertIs(posts[0][2]["json"]["RefreshLibrary"], requested)
                    self.assertEqual(
                        result["evidence"]["initial_scans_requested"], int(requested)
                    )
                    spec["scan_on_create"] = True
                    repeated = media._libraries(
                        action, config, payload, apply=True, factory=server.factory
                    )
                    self.assertEqual(repeated["execution"], "already_present")
                    self.assertEqual(repeated["evidence"]["initial_scans_requested"], 0)
                    self.assertEqual(
                        len([row for row in server.calls if row[0] == "POST"]), 1
                    )

    def test_unsupported_servers_refuse_initial_scan_instead_of_ignoring_user_choice(
        self,
    ):
        for service, kind in (("plex", "movie"), ("audiobookshelf", "book")):
            server = FakeMediaServer(service)
            payload = {
                "arrangement": {
                    "values": {
                        service
                        + "::libraries": [
                            {
                                "name": "Media",
                                "media_type": kind,
                                "paths": ["/media/example"],
                                "create_if_missing": True,
                                "scan_on_create": True,
                            }
                        ]
                    }
                }
            }
            with (
                self.subTest(service=service),
                self.assertRaises(media.MediaInputError),
            ):
                media._libraries(
                    {"service_key": service, "source": service},
                    {},
                    payload,
                    apply=True,
                    factory=server.factory,
                )
            self.assertEqual(server.calls, [])
