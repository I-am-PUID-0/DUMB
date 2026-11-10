import json
import unittest

from utils.arrangement_http import ApiResponse
from utils.arrangement_workflow_target import (
    WorkflowTargetError,
    default_release_name,
    validate_target,
)


class NativeReleaseTargetTests(unittest.TestCase):
    def client(self, document, item=None, status=200):
        test = self

        class Client:
            def request(self, method, route, **options):
                test.assertEqual(method, "GET")
                data = document if route.endswith("/parse") else item
                return ApiResponse(status, json.dumps(data).encode())

        return Client()

    def test_default_names_retain_native_identity_components_and_safe_marker(self):
        marker = "DUMB.Setup.Probe." + "a" * 32
        self.assertEqual(
            default_release_name(
                {"title": "Movie", "year": 2020}, {}, "radarr", marker
            ),
            "Movie (2020) [" + marker + "]",
        )
        self.assertIn(
            "Show S02E03",
            default_release_name(
                {"seasonNumber": 2, "episodeNumber": 3},
                {"title": "Show"},
                "sonarr",
                marker,
            ),
        )
        self.assertIn(
            "Artist - Album (2021)",
            default_release_name(
                {"title": "Album", "releaseDate": "2021-03-01"},
                {"artistName": "Artist"},
                "lidarr",
                marker,
            ),
        )
        self.assertEqual(
            default_release_name(
                {}, {}, "radarr", marker, release_name="Native alias (2020)"
            ),
            "Native alias (2020) [" + marker + "]",
        )
        with self.assertRaises(WorkflowTargetError):
            default_release_name({"title": "Movie"}, {}, "radarr", marker)
        with self.assertRaises(WorkflowTargetError):
            default_release_name(
                {"title": "Movie", "year": 2020}, {}, "radarr", "../unsafe"
            )

    def test_movie_and_whisparr_require_exact_native_movie_id(self):
        for family in ("radarr", "whisparr"):
            for document, accepted in (
                ({"movie": {"id": 7}}, True),
                ({"parsedMovieInfo": {"movieTitle": "Movie"}}, False),
                ({"movie": {"id": 8}}, False),
                ({"movie": {"id": True}}, False),
            ):
                with self.subTest(family=family, document=document):
                    if accepted:
                        self.assertEqual(
                            validate_target(
                                self.client(document),
                                "/api/v3",
                                family,
                                "Movie (2020)",
                                7,
                            )["item_id"],
                            7,
                        )
                    else:
                        with self.assertRaises(WorkflowTargetError):
                            validate_target(
                                self.client(document),
                                "/api/v3",
                                family,
                                "Movie (2020)",
                                7,
                            )

    def test_episode_and_album_require_one_exact_item_and_native_parent(self):
        for family, section, parent, parent_key in (
            ("sonarr", "episodes", "series", "seriesId"),
            ("lidarr", "albums", "artist", "artistId"),
        ):
            item = {"id": 7, parent_key: 3}
            document = {section: [{"id": 7, parent_key: 3}], parent: {"id": 3}}
            self.assertEqual(
                validate_target(
                    self.client(document, item),
                    "/api/v1" if family == "lidarr" else "/api/v3",
                    family,
                    "Release",
                    7,
                )["parent_id"],
                3,
            )
            for changed in (
                {**document, section: [{"id": 7}, {"id": 8}]},
                {**document, section: [{"id": 8}]},
                {**document, parent: {"id": 4}},
                {**document, section: [{"id": 7, parent_key: 4}]},
            ):
                with self.subTest(family=family, changed=changed):
                    with self.assertRaises(WorkflowTargetError):
                        validate_target(
                            self.client(changed, item), "/api/v1", family, "Release", 7
                        )

    def test_unsupported_native_parse_and_unsafe_user_release_are_refused(self):
        with self.assertRaises(WorkflowTargetError):
            validate_target(
                self.client({}, status=404), "/api/v3", "radarr", "Movie", 7
            )
        with self.assertRaises(WorkflowTargetError):
            validate_target(
                self.client({"movie": {"id": 7}}),
                "/api/v3",
                "radarr",
                "Movie\nOther",
                7,
            )
