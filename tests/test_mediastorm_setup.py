import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class MediaStormSetupTests(unittest.TestCase):
    def _runtime_tree(self, root: Path) -> None:
        (root / "web").mkdir(parents=True)
        (root / "iroh").mkdir()
        (root / "python-venv" / "bin").mkdir(parents=True)
        (root / "scripts").mkdir()
        (root / "bin").mkdir()
        (root / "mediastorm").write_text("binary", encoding="utf-8")
        (root / "version.txt").write_text("v1.5.0-20260711\n", encoding="utf-8")
        (root / "web" / "index.html").write_text("web", encoding="utf-8")
        (root / "iroh" / "iroh-direct-spike").write_text("iroh", encoding="utf-8")
        (root / "python-venv" / "bin" / "python3").write_text(
            "python", encoding="utf-8"
        )
        for binary_name in ("ffmpeg", "ffprobe", "yt-dlp", "deno"):
            (root / "bin" / binary_name).write_text("binary", encoding="utf-8")
        for script_name in (
            "parse_title.py",
            "parse_title_batch.py",
            "search_subtitles.py",
            "download_subtitle.py",
            "detect_credits.py",
        ):
            (root / "scripts" / script_name).write_text("", encoding="utf-8")

    def test_configure_wires_installed_runtime_and_managed_postgres(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            runtime = config_dir / "runtime"
            links_dir = root / "links"
            log_dir = root / "log"
            links_dir.mkdir()
            self._runtime_tree(runtime)
            config = {
                "enabled": True,
                "process_name": "MediaStorm",
                "port": 7788,
                "config_dir": str(config_dir),
                "command": [],
                "env": {},
                "wait_for_tcp": [],
                "log_file": "",
            }

            with (
                patch.object(setup, "_MEDIASTORM_RUNTIME_LINK_DIR", str(links_dir)),
                patch.object(
                    setup, "_MEDIASTORM_PYTHON_LINK", str(root / "python-link")
                ),
                patch.object(setup, "_MEDIASTORM_LOG_DIR", str(log_dir)),
                patch.object(setup.CONFIG_MANAGER, "get") as config_get,
                patch.object(setup.CONFIG_MANAGER, "save_config") as save_config,
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup, "_ensure_postgres_enabled", return_value=True),
                patch.object(
                    setup, "_ensure_postgres_database_config", return_value=True
                ) as ensure_database,
                patch.object(
                    setup,
                    "_initialize_postgres_databases_if_running",
                    return_value=(True, None),
                ) as initialize_databases,
                patch.object(
                    setup,
                    "_postgres_database_url",
                    return_value="postgresql://user:pass@db:5433/mediastorm",
                ),
                patch.dict(os.environ, {"PATH": "/usr/bin"}),
            ):
                config_get.side_effect = lambda key: (
                    config if key == "mediastorm" else {"host": "db", "port": 5433}
                )
                success, error = setup.setup_mediastorm(object(), configure_only=True)

            self.assertTrue(success, error)
            self.assertEqual(
                [str(runtime / "mediastorm"), "--port", "7788"],
                config["command"],
            )
            self.assertEqual(
                "postgresql://user:pass@db:5433/mediastorm",
                config["env"]["DATABASE_URL"],
            )
            self.assertEqual(
                [{"name": "PostgreSQL", "host": "db", "port": 5433, "timeout": 2}],
                config["wait_for_tcp"],
            )
            self.assertEqual(str(log_dir / "mediastorm.log"), config["log_file"])
            self.assertEqual(
                f"{runtime}/python-venv/bin:{runtime}/bin:/usr/bin",
                config["env"]["PATH"],
            )
            self.assertEqual(
                str(runtime / "python-venv"), os.readlink(root / "python-link")
            )
            self.assertEqual(
                str(runtime / "scripts" / "parse_title.py"),
                os.readlink(links_dir / "parse_title.py"),
            )
            self.assertEqual(
                str(runtime / "app-version.txt"),
                os.readlink(config_dir / "version.txt"),
            )
            self.assertEqual(
                (config_dir / "version.txt").read_text(encoding="utf-8"),
                "1.5.0\n20260711\n",
            )
            ensure_database.assert_called_once_with("mediastorm")
            initialize_databases.assert_called_once_with()
            save_config.assert_called_once_with("MediaStorm")

    def test_missing_runtime_requests_install_phase(self):
        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(setup, "_MEDIASTORM_LOG_DIR", temp_dir),
            patch.object(
                setup.CONFIG_MANAGER,
                "get",
                return_value={"enabled": True, "config_dir": temp_dir},
            ),
        ):
            success, error = setup.setup_mediastorm(object(), configure_only=True)

        self.assertFalse(success)
        self.assertIn("run the install phase", error)

    def test_install_phase_downloads_missing_runtime(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            links_dir = root / "links"
            log_dir = root / "log"
            links_dir.mkdir()
            config = {
                "enabled": True,
                "process_name": "MediaStorm",
                "repo_owner": "godver3",
                "repo_name": "mediastorm",
                "config_dir": str(config_dir),
                "port": 7777,
                "env": {},
            }

            def install_runtime(_config, version):
                self.assertEqual(version, "v1.2.3")
                self._runtime_tree(config_dir / "runtime")
                return {
                    "version": version,
                    "image_digest": "sha256:" + "a" * 64,
                    "oci_reference": "latest",
                    "runtime_dir": str(config_dir / "runtime"),
                }

            with (
                patch.object(setup, "_MEDIASTORM_RUNTIME_LINK_DIR", str(links_dir)),
                patch.object(
                    setup, "_MEDIASTORM_PYTHON_LINK", str(root / "python-link")
                ),
                patch.object(setup, "_MEDIASTORM_LOG_DIR", str(log_dir)),
                patch.object(setup.CONFIG_MANAGER, "get") as config_get,
                patch.object(setup, "_chown_recursive_if_needed"),
                patch.object(setup, "_ensure_postgres_enabled", return_value=False),
                patch.object(
                    setup, "_ensure_postgres_database_config", return_value=False
                ),
                patch.object(
                    setup,
                    "_initialize_postgres_databases_if_running",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "_postgres_database_url",
                    return_value="postgresql://db/mediastorm",
                ),
                patch.object(
                    setup.downloader,
                    "get_latest_release",
                    return_value=("v1.2.3", None),
                ),
                patch.object(
                    setup, "install_mediastorm_runtime", side_effect=install_runtime
                ) as install,
                patch.object(setup, "chown_recursive", return_value=(True, None)),
            ):
                config_get.side_effect = lambda key: (
                    config if key == "mediastorm" else {"host": "db", "port": 5432}
                )
                success, error = setup.setup_mediastorm(object(), install_only=True)

            self.assertTrue(success, error)
            install.assert_called_once()

    def test_running_postgres_reconciles_newly_registered_databases(self):
        postgres_config = {
            "host": "127.0.0.1",
            "port": 5432,
            "user": "DUMB",
            "password": "secret",
            "databases": [{"name": "mediastorm", "enabled": True}],
        }
        with (
            patch.object(setup.CONFIG_MANAGER, "get", return_value=postgres_config),
            patch.object(
                setup.subprocess,
                "run",
                return_value=Mock(returncode=0),
            ) as readiness,
            patch.object(
                setup.postgres,
                "initialize_postgres_databases",
                return_value=(True, None),
            ) as initialize_databases,
        ):
            success, error = setup._initialize_postgres_databases_if_running()

        self.assertTrue(success, error)
        readiness.assert_called_once_with(
            [
                "pg_isready",
                "-U",
                "DUMB",
                "-d",
                "postgres",
                "-h",
                "127.0.0.1",
                "-p",
                "5432",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        initialize_databases.assert_called_once_with(
            "127.0.0.1",
            5432,
            "DUMB",
            "secret",
            postgres_config["databases"],
        )

    def test_stopped_postgres_defers_database_reconciliation(self):
        with (
            patch.object(
                setup.CONFIG_MANAGER,
                "get",
                return_value={"host": "127.0.0.1", "port": 5432},
            ),
            patch.object(
                setup.subprocess,
                "run",
                return_value=Mock(returncode=1),
            ),
            patch.object(
                setup.postgres, "initialize_postgres_databases"
            ) as initialize_databases,
        ):
            success, error = setup._initialize_postgres_databases_if_running()

        self.assertTrue(success, error)
        initialize_databases.assert_not_called()

    def test_release_update_installs_verified_oci_runtime(self):
        config = {
            "release_version": "latest",
            "repo_owner": "godver3",
            "repo_name": "mediastorm",
        }
        installed = {
            "version": "v1.5.0-20260711",
            "image_digest": "sha256:" + "a" * 64,
            "oci_reference": "latest",
            "runtime_dir": "/mediastorm/runtime",
        }
        with (
            patch.object(
                setup.downloader,
                "get_latest_release",
                return_value=("v1.5.0-20260711", None),
            ),
            patch.object(
                setup, "install_mediastorm_runtime", return_value=installed
            ) as install,
            patch.object(setup, "chown_recursive", return_value=(True, None)),
        ):
            success, error = setup.setup_release_version(
                object(), config, "MediaStorm", "mediastorm"
            )

        self.assertTrue(success, error)
        install.assert_called_once_with(config, "v1.5.0-20260711")

    def test_pinned_release_uses_selected_oci_reference_without_latest_lookup(self):
        config = {
            "release_version_enabled": True,
            "release_version": "1.5.0",
            "repo_owner": "godver3",
            "repo_name": "mediastorm",
        }
        installed = {
            "version": "v1.5.0-20260711",
            "image_digest": "sha256:" + "a" * 64,
            "oci_reference": "1.5.0",
            "runtime_dir": "/mediastorm/runtime",
        }
        with (
            patch.object(setup.downloader, "get_latest_release") as latest,
            patch.object(
                setup, "install_mediastorm_runtime", return_value=installed
            ) as install,
            patch.object(setup, "chown_recursive", return_value=(True, None)),
        ):
            success, error = setup.setup_release_version(
                object(), config, "MediaStorm", "mediastorm"
            )

        self.assertTrue(success, error)
        latest.assert_not_called()
        install.assert_called_once_with(config, "1.5.0")


if __name__ == "__main__":
    unittest.main()
