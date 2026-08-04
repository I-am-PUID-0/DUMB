import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

utils_pkg = sys.modules.get("utils")
for module_name in (
    "utils.profilarr_settings",
    "utils.versions",
    "utils.core_services",
    "utils.config_loader",
    "utils.decypharr_settings",
    "utils.user_management",
):
    sys.modules.pop(module_name, None)
    attr_name = module_name.rsplit(".", 1)[-1]
    if utils_pkg is not None and hasattr(utils_pkg, attr_name):
        delattr(utils_pkg, attr_name)
profilarr_settings = importlib.import_module("utils.profilarr_settings")

from utils.versions import Versions

versions = Versions()
validate_profilarr_legacy_layout = profilarr_settings.validate_profilarr_legacy_layout
validate_profilarr_layout = profilarr_settings.validate_profilarr_layout
profilarr_v2_runtime_environment = profilarr_settings.profilarr_v2_runtime_environment


class ProfilarrSetupTests(unittest.TestCase):
    def test_v2_runtime_uses_writable_per_instance_home_and_cache(self):
        environment = profilarr_v2_runtime_environment(
            "/profilarr/v2/config",
            6868,
            "/usr/lib/aarch64-linux-gnu/libsqlite3.so.0",
        )

        self.assertEqual("/profilarr/v2/config", environment["HOME"])
        self.assertEqual("/profilarr/v2/config/.cache", environment["XDG_CACHE_HOME"])
        self.assertEqual("/profilarr/v2/config/.deno", environment["DENO_DIR"])
        self.assertEqual("1", environment["DENO_NO_UPDATE_CHECK"])
        self.assertEqual("6868", environment["PORT"])
        self.assertEqual(
            "/usr/lib/aarch64-linux-gnu/libsqlite3.so.0",
            environment["DENO_SQLITE_PATH"],
        )

    def test_latest_official_release_resolves_to_current_release(self):
        with patch.object(
            versions.downloader,
            "get_latest_release",
            return_value=("v2.0.9", None),
        ):
            release, version_to_write = versions.resolve_profilarr_release_version(
                {
                    "repo_owner": "Dictionarry-Hub",
                    "repo_name": "profilarr",
                    "release_version": "latest",
                }
            )

        self.assertEqual("v2.0.9", release)
        self.assertEqual("v2.0.9", version_to_write)

    def test_explicit_release_version_is_preserved(self):
        release, version_to_write = versions.resolve_profilarr_release_version(
            {
                "repo_owner": "Dictionarry-Hub",
                "repo_name": "profilarr",
                "release_version": "v2.0.7",
            }
        )

        self.assertEqual("v2.0.7", release)
        self.assertEqual("v2.0.7", version_to_write)

    def test_legacy_layout_validation_requires_backend_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            backend_dir = Path(tmpdir) / "backend"
            success, error = validate_profilarr_legacy_layout(
                "Profiles", str(backend_dir)
            )

            self.assertFalse(success)
            self.assertIn("legacy backend/frontend layout", error)

            entrypoint = backend_dir / "app" / "main.py"
            entrypoint.parent.mkdir(parents=True)
            entrypoint.write_text("def create_app(): pass\n")

            success, error = validate_profilarr_legacy_layout(
                "Profiles", str(backend_dir)
            )
            self.assertTrue(success)
            self.assertIsNone(error)

    def test_layout_detection_supports_v1_and_v2(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            v1_entrypoint = root / "backend" / "app" / "main.py"
            v1_entrypoint.parent.mkdir(parents=True)
            v1_entrypoint.touch()

            layout, error = validate_profilarr_layout("Profiles", str(root))
            self.assertEqual("v1", layout)
            self.assertIsNone(error)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for filename in profilarr_settings.PROFILARR_V2_LAYOUT_FILES:
                (root / filename).touch()
            (root / "src").mkdir()

            layout, error = validate_profilarr_layout("Profiles", str(root))
            self.assertEqual("v2", layout)
            self.assertIsNone(error)

    def test_v2_arr_reconciliation_only_changes_dumb_managed_rows(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config_root = Path(tmpdir)
            data_dir = config_root / "data"
            data_dir.mkdir()
            db_path = data_dir / "profilarr.db"
            with sqlite3.connect(db_path) as conn:
                conn.execute("""
                    CREATE TABLE arr_instances (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        type TEXT NOT NULL,
                        url TEXT NOT NULL,
                        external_url TEXT,
                        api_key TEXT NOT NULL,
                        tags TEXT,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """)
                conn.executemany(
                    """
                    INSERT INTO arr_instances (name, type, url, api_key, tags)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            "Managed Radarr",
                            "radarr",
                            "http://old:7878",
                            "old-key",
                            json.dumps(["dumb:auto", "keep-me"]),
                        ),
                        (
                            "Stale Sonarr",
                            "sonarr",
                            "http://old:8989",
                            "old-key",
                            json.dumps(["dumb:auto"]),
                        ),
                        (
                            "User Sonarr",
                            "sonarr",
                            "https://user.example",
                            "user-key",
                            json.dumps(["personal"]),
                        ),
                    ],
                )

            entries = [
                {
                    "name": "Managed Radarr",
                    "type": "radarr",
                    "arr_server": "http://127.0.0.1:7878",
                    "api_key": "new-key",
                    "tags": ["dumb:auto", "Radarr"],
                },
                {
                    "name": "User Sonarr",
                    "type": "sonarr",
                    "arr_server": "http://127.0.0.1:8989",
                    "api_key": "replacement-key",
                    "tags": ["dumb:auto", "Sonarr"],
                },
                {
                    "name": "New Sonarr",
                    "type": "sonarr",
                    "arr_server": "http://127.0.0.1:8990",
                    "api_key": "new-sonarr-key",
                    "tags": ["dumb:auto", "Sonarr"],
                },
            ]
            with patch.object(
                profilarr_settings, "_build_arr_entries", return_value=entries
            ):
                success, error = profilarr_settings._sync_profilarr_v2_arr_configs(
                    str(config_root), ["decypharr"]
                )

            self.assertTrue(success)
            self.assertIsNone(error)
            with sqlite3.connect(db_path) as conn:
                rows = {
                    row[0]: row[1:]
                    for row in conn.execute(
                        "SELECT name, url, api_key, tags FROM arr_instances"
                    )
                }

            self.assertNotIn("Stale Sonarr", rows)
            self.assertEqual(rows["Managed Radarr"][0], "http://127.0.0.1:7878")
            self.assertEqual(rows["Managed Radarr"][1], "new-key")
            self.assertIn("keep-me", json.loads(rows["Managed Radarr"][2]))
            self.assertEqual(rows["User Sonarr"][0], "https://user.example")
            self.assertEqual(rows["User Sonarr"][1], "user-key")
            self.assertIn("New Sonarr", rows)


if __name__ == "__main__":
    unittest.main()
