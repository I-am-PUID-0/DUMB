import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class NzbDAVSetupTests(unittest.TestCase):
    def test_fresh_prerelease_install_handles_comparison_error_without_type_crash(self):
        config = {
            "process_name": "NzbDAV",
            "config_dir": "/nzbdav",
            "release_version_enabled": True,
            "release_version": "prerelease",
            "auto_update": False,
            "branch_enabled": False,
            "commit_sha": "",
        }
        config_manager = Mock()
        config_manager.find_key_for_process.return_value = ("nzbdav", None)
        config_manager.get_instance.return_value = config
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(setup, "CONFIG_MANAGER", config_manager),
            patch.object(
                setup.versions,
                "compare_versions",
                return_value=(False, "No prerelease versions found."),
            ),
            patch.object(
                setup.versions,
                "version_check",
                return_value=(None, "version marker missing"),
            ),
            patch.object(
                setup, "setup_release_version", return_value=(True, None)
            ) as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup._setup_project_inner(
                process_handler,
                "NzbDAV",
                install_phase=True,
                configure_phase=False,
            )

        self.assertTrue(success, error)
        install_release.assert_called_once_with(
            process_handler, config, "NzbDAV", "nzbdav"
        )

    def test_artifact_restore_staging_resolves_data_root_symlink(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            data_root = root / "data"
            target = data_root / "nzbdav"
            target.mkdir(parents=True)
            link = root / "nzbdav"
            link.symlink_to(target, target_is_directory=True)

            staging_parent = setup._same_filesystem_staging_parent(str(link))

        self.assertEqual(str(data_root), staging_parent)

    def test_source_discovery_prefers_known_layout_without_walking_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend_project = root / "backend" / "NzbWebDAV.csproj"
            backend_project.parent.mkdir()
            backend_project.write_text("<Project />", encoding="utf-8")
            frontend_dir = root / "frontend"
            frontend_dir.mkdir()
            (frontend_dir / "package.json").write_text("{}", encoding="utf-8")
            (root / "blobs" / "aa" / "bb").mkdir(parents=True)

            with patch.object(
                setup.os,
                "walk",
                side_effect=AssertionError("known layouts must not be walked"),
            ):
                found_backend, error = setup._find_nzbdav_backend_project(str(root), {})
                found_frontend = setup._find_nzbdav_frontend_dir(str(root), {})

        self.assertIsNone(error)
        self.assertEqual(str(backend_project), found_backend)
        self.assertEqual(str(frontend_dir), found_frontend)

    def test_source_discovery_fallback_prunes_runtime_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            backend_project = root / "source" / "server" / "Fork.csproj"
            backend_project.parent.mkdir(parents=True)
            backend_project.write_text("<Project />", encoding="utf-8")
            frontend_dir = root / "source" / "ui"
            frontend_dir.mkdir()
            (frontend_dir / "package.json").write_text("{}", encoding="utf-8")

            runtime_dir = root / "blobs" / "aa" / "bb"
            runtime_dir.mkdir(parents=True)
            (runtime_dir / "Runtime.csproj").write_text("<Project />", encoding="utf-8")
            (runtime_dir / "package.json").write_text("{}", encoding="utf-8")

            real_scandir = os.scandir

            def guarded_scandir(path):
                if Path(path) == root / "blobs":
                    raise AssertionError("runtime data must not be traversed")
                return real_scandir(path)

            with patch.object(setup.os, "scandir", side_effect=guarded_scandir):
                found_backend, error = setup._find_nzbdav_backend_project(str(root), {})
                found_frontend = setup._find_nzbdav_frontend_dir(str(root), {})

        self.assertIsNone(error)
        self.assertEqual(str(backend_project), found_backend)
        self.assertEqual(str(frontend_dir), found_frontend)

    def test_frontend_runtime_requires_server_and_client_outputs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            frontend = Path(tmpdir) / "frontend"
            (frontend / "dist-node").mkdir(parents=True)
            (frontend / "dist-node" / "server.js").write_text("", encoding="utf-8")

            ready, error = setup._validate_nzbdav_frontend_runtime(str(frontend))
            self.assertFalse(ready)
            self.assertIn("build/server/index.js", error)

            (frontend / "build" / "server").mkdir(parents=True)
            (frontend / "build" / "server" / "index.js").write_text(
                "", encoding="utf-8"
            )
            (frontend / "build" / "client").mkdir()
            ready, error = setup._validate_nzbdav_frontend_runtime(str(frontend))

        self.assertTrue(ready, error)

    def test_artifact_activation_replaces_backend_and_frontend_together(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            restored = root / "restored"
            live = root / "live"
            for component in ("app", "frontend-build", "frontend-dist-node"):
                (restored / component).mkdir(parents=True)
                (restored / component / "marker").write_text("new", encoding="utf-8")
                (live / component).mkdir(parents=True)
                (live / component / "marker").write_text("old", encoding="utf-8")

            activated, error = setup._activate_nzbdav_build_artifact(
                [
                    (restored / "app", live / "app"),
                    (restored / "frontend-build", live / "frontend-build"),
                    (
                        restored / "frontend-dist-node",
                        live / "frontend-dist-node",
                    ),
                ]
            )

            self.assertTrue(activated, error)
            for component in ("app", "frontend-build", "frontend-dist-node"):
                self.assertEqual(
                    "new",
                    (live / component / "marker").read_text(encoding="utf-8"),
                )

    def test_commit_pin_requires_full_sha_and_normalizes_case(self):
        commit_sha = "A" * 40

        normalized, error = setup._normalize_commit_sha(commit_sha)
        short_value, short_error = setup._normalize_commit_sha("abc1234")

        self.assertEqual("a" * 40, normalized)
        self.assertIsNone(error)
        self.assertIsNone(short_value)
        self.assertIn("40-character hexadecimal", short_error)

    def test_commit_state_skip_requires_matching_installed_version_marker(self):
        commit_sha = "a" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            version_path = Path(tmpdir) / "version.txt"
            version_path.write_text("v0.8.1", encoding="utf-8")

            self.assertFalse(setup._commit_version_marker_matches(tmpdir, commit_sha))

            version_path.write_text(
                f"commit-{commit_sha[:12]}",
                encoding="utf-8",
            )
            self.assertTrue(setup._commit_version_marker_matches(tmpdir, commit_sha))

    def test_commit_pin_takes_precedence_over_release_and_branch(self):
        commit_sha = "a" * 40
        config = {
            "enabled": True,
            "process_name": "NzbDAV",
            "config_dir": "/nzbdav",
            "release_version_enabled": True,
            "release_version": "latest",
            "commit_sha": commit_sha,
            "branch_enabled": True,
            "branch": "main",
            "env": {},
        }
        process_handler = Mock()
        process_handler.setup_tracker = set()
        process_handler.setup_tracker_lock = threading.Lock()

        with (
            patch.object(
                setup.CONFIG_MANAGER,
                "find_key_for_process",
                return_value=("nzbdav", None),
            ),
            patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
            patch.object(
                setup, "setup_branch_version", return_value=(True, None)
            ) as install_source,
            patch.object(setup, "setup_release_version") as install_release,
            patch.object(setup, "setup_nzbdav", return_value=(True, None)),
        ):
            success, error = setup.install_project(process_handler, "NzbDAV")

        self.assertTrue(success, error)
        install_source.assert_called_once_with(
            process_handler, config, "NzbDAV", "nzbdav"
        )
        install_release.assert_not_called()

    def test_commit_marker_is_not_written_before_source_setup_succeeds(self):
        commit_sha = "b" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "DUMB Frontend",
                "config_dir": tmpdir,
                "repo_owner": "example",
                "repo_name": "frontend",
                "commit_sha": commit_sha,
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_commit",
                    return_value=("https://example.invalid/archive.zip", "source"),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    return_value=(True, None),
                ),
                patch.object(
                    setup,
                    "additional_setup",
                    return_value=(False, "build failed"),
                ),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_branch_version(
                    Mock(),
                    config,
                    "DUMB Frontend",
                    "dumb_frontend",
                )

        self.assertFalse(success)
        self.assertEqual("build failed", error)
        version_write.assert_not_called()

    def test_release_tag_marker_records_resolved_commit_after_setup(self):
        release_sha = "d" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "NzbDAV",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "dev",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ),
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as download_release,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "NzbDAV", "nzbdav"
                )

        self.assertTrue(success, error)
        self.assertEqual(
            release_sha,
            download_release.call_args.kwargs["release_version"],
        )
        version_write.assert_called_once_with(
            "NzbDAV",
            "nzbdav",
            version_path=os.path.join(tmpdir, "version.txt"),
            version=f"dev-{release_sha[:8]}",
        )

    def test_release_tag_marker_is_not_written_when_setup_fails(self):
        release_sha = "e" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "NzbDAV",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "dev",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ),
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ),
                patch.object(
                    setup, "additional_setup", return_value=(False, "build failed")
                ),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "NzbDAV", "nzbdav"
                )

        self.assertFalse(success)
        self.assertEqual("build failed", error)
        version_write.assert_not_called()

    def test_prebuilt_release_is_verified_activated_and_manifested(self):
        release_sha = "a" * 40
        published_digest = "b" * 64
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            config = {
                "process_name": "NzbDAV",
                "config_dir": str(root),
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
            }

            def populate_candidate(_url, target_dir, **kwargs):
                candidate = Path(target_dir)
                backend = candidate / "backend"
                frontend = candidate / "frontend"
                backend.mkdir(parents=True)
                for name in (
                    "NzbWebDAV.dll",
                    "NzbWebDAV.deps.json",
                    "librapidyenc.so",
                    "libe_sqlite3.so",
                ):
                    (backend / name).write_text(name, encoding="utf-8")
                (backend / "NzbWebDAV.runtimeconfig.json").write_text(
                    '{"runtimeOptions":{"frameworks":['
                    '{"name":"Microsoft.AspNetCore.App","version":"10.0.0"}]}}',
                    encoding="utf-8",
                )
                (frontend / "dist-node").mkdir(parents=True)
                (frontend / "dist-node" / "server.js").write_text(
                    "server", encoding="utf-8"
                )
                (frontend / "build" / "server").mkdir(parents=True)
                (frontend / "build" / "server" / "index.js").write_text(
                    "build", encoding="utf-8"
                )
                (frontend / "build" / "client").mkdir()
                (frontend / "package.json").write_text("{}", encoding="utf-8")
                (frontend / "node_modules" / "express").mkdir(parents=True)
                (frontend / "node_modules" / "express" / "package.json").write_text(
                    "{}", encoding="utf-8"
                )
                (candidate / "version.txt").write_text("0.10.0-rc.2", encoding="utf-8")
                self.assertEqual(
                    f"sha256:{published_digest}", kwargs["expected_sha256"]
                )
                return True, None

            release_info = {
                "assets": [
                    {
                        "name": "nzbdav-v0.10.0-rc.2-linux-arm64.tar.gz",
                        "digest": f"sha256:{published_digest}",
                        "browser_download_url": "https://example.test/nzbdav.tar.gz",
                    }
                ]
            }
            with (
                patch.object(setup.platform, "machine", return_value="aarch64"),
                patch.object(
                    setup.downloader,
                    "fetch_github_release_info",
                    return_value=(release_info, None),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup.downloader,
                    "download_and_extract",
                    side_effect=populate_candidate,
                ),
                patch.object(setup, "chown_recursive", return_value=(True, None)),
                patch.object(setup, "chown_single", return_value=(True, None)),
            ):
                result, error = setup._install_nzbdav_prebuilt_release(
                    config, "NzbDAV", "v0.10.0-rc.2", str(root)
                )
                runtime_ready, runtime_error = setup._nzbdav_prebuilt_runtime_ready(
                    str(root)
                )

            self.assertIsNone(error)
            self.assertEqual(
                f"v0.10.0-rc.2-{release_sha[:8]}", result["version_marker"]
            )
            self.assertTrue((root / "app" / "NzbWebDAV.dll").is_file())
            self.assertTrue((root / "frontend" / "dist-node" / "server.js").is_file())
            self.assertTrue((root / setup._NZBDAV_PREBUILT_MARKER).is_file())
            self.assertTrue(runtime_ready, runtime_error)

    def test_prebuilt_unavailable_falls_back_to_source_release(self):
        release_sha = "c" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "NzbDAV",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "v0.9.5",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ) as prebuilt,
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as source_download,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write"),
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "NzbDAV", "nzbdav"
                )

        self.assertTrue(success, error)
        prebuilt.assert_called_once()
        source_download.assert_called_once()

    def test_prerelease_source_fallback_uses_resolved_immutable_commit(self):
        release_sha = "d" * 40
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {
                "process_name": "NzbDAV",
                "config_dir": tmpdir,
                "repo_owner": "nzbdav",
                "repo_name": "nzbdav",
                "release_version": "prerelease",
                "clear_on_update": False,
            }
            with (
                patch.object(
                    setup.downloader,
                    "get_latest_release",
                    return_value=("v0.10.0-rc.3", None),
                ),
                patch.object(
                    setup.downloader,
                    "get_ref_commit_sha",
                    return_value=(release_sha, None),
                ),
                patch.object(
                    setup,
                    "_install_nzbdav_prebuilt_release",
                    return_value=(None, "archive unavailable"),
                ) as prebuilt,
                patch.object(
                    setup, "_prepare_nzbdav_source_tree", return_value=(True, None)
                ),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    return_value=(True, None),
                ) as source_download,
                patch.object(setup, "additional_setup", return_value=(True, None)),
                patch.object(setup.versions, "version_write") as version_write,
            ):
                success, error = setup.setup_release_version(
                    Mock(), config, "NzbDAV", "nzbdav"
                )

        self.assertTrue(success, error)
        self.assertEqual("v0.10.0-rc.3", prebuilt.call_args.args[2])
        self.assertEqual(
            release_sha,
            source_download.call_args.kwargs["release_version"],
        )
        version_write.assert_called_once_with(
            "NzbDAV",
            "nzbdav",
            version_path=os.path.join(tmpdir, "version.txt"),
            version=f"v0.10.0-rc.3-{release_sha[:8]}",
        )

    def _write_start_script(self, root: Path) -> Path:
        frontend_dir = root / "frontend"
        frontend_dir.mkdir()
        with patch.object(setup, "chown_single"):
            script_path = setup._write_nzbdav_start_script(
                str(root),
                [str(root / "backend")],
                str(frontend_dir),
                8080,
            )
        return Path(script_path)

    def test_start_script_runs_frontend_before_blocking_migration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            script = self._write_start_script(Path(tmpdir)).read_text(encoding="utf-8")

        frontend_start = script.index("node dist-node/server.js &")
        migration_start = script.index(" --db-migration")
        backend_start = script.index(" &\nBACKEND_PID=$!", migration_start)

        self.assertLess(script.index("trap terminate TERM INT"), migration_start)
        self.assertLess(frontend_start, migration_start)
        self.assertLess(migration_start, backend_start)
        self.assertIn("MIGRATION_EXIT_CODE=$?", script)
        self.assertIn('exit "$MIGRATION_EXIT_CODE"', script)

    def test_migration_failure_stops_frontend_and_preserves_exit_code(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            state_dir = root / "state"
            state_dir.mkdir()
            bin_dir = root / "bin"
            bin_dir.mkdir()

            backend = root / "backend"
            backend.write_text(
                """#!/bin/sh
if [ "${1:-}" = "--db-migration" ]; then
  count=0
  while [ ! -f "$TEST_STATE/frontend.started" ] && [ "$count" -lt 100 ]; do
    sleep 0.01
    count=$((count + 1))
  done
  [ -f "$TEST_STATE/frontend.started" ] || exit 99
  touch "$TEST_STATE/migration.started"
  exit 23
fi
touch "$TEST_STATE/backend.started"
""",
                encoding="utf-8",
            )
            backend.chmod(0o755)

            node = bin_dir / "node"
            node.write_text(
                """#!/bin/sh
touch "$TEST_STATE/frontend.started"
trap 'touch "$TEST_STATE/frontend.stopped"; exit 0' TERM INT
while :; do sleep 0.05; done
""",
                encoding="utf-8",
            )
            node.chmod(0o755)
            script = self._write_start_script(root)

            env = os.environ.copy()
            env["PATH"] = f"{bin_dir}:{env['PATH']}"
            env["TEST_STATE"] = str(state_dir)
            result = subprocess.run(
                ["/bin/sh", str(script)],
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(23, result.returncode, result.stderr)
            self.assertTrue((state_dir / "migration.started").exists())
            self.assertTrue((state_dir / "frontend.stopped").exists())
            self.assertFalse((state_dir / "backend.started").exists())


if __name__ == "__main__":
    unittest.main()
