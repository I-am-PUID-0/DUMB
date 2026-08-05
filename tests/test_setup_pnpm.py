import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import setup


class SetupPnpmTests(unittest.TestCase):
    def test_npmrc_update_preserves_upstream_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            npmrc = Path(temp_dir, ".npmrc")
            npmrc.write_text(
                "strict-peer-dependencies=false\nchild-concurrency=1\n",
                encoding="utf-8",
            )

            setup._update_npmrc_settings(
                str(npmrc),
                {"child-concurrency": 8, "network-concurrency": 16},
            )

            contents = npmrc.read_text(encoding="utf-8").splitlines()
            self.assertIn("strict-peer-dependencies=false", contents)
            self.assertIn("child-concurrency=8", contents)
            self.assertIn("network-concurrency=16", contents)
            self.assertNotIn("child-concurrency=1", contents)

    def test_npmrc_update_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir, "target")
            target.write_text("do-not-change\n", encoding="utf-8")
            npmrc = Path(temp_dir, ".npmrc")
            npmrc.symlink_to(target)

            setup._update_npmrc_settings(str(npmrc), {"child-concurrency": 8})

            self.assertFalse(npmrc.is_symlink())
            self.assertEqual(target.read_text(encoding="utf-8"), "do-not-change\n")
            self.assertEqual(npmrc.read_text(encoding="utf-8"), "child-concurrency=8\n")

    def test_shared_build_cache_is_owned_by_controller_not_service_user(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_dir = str(Path(temp_dir, "dependencies", "python"))
            with (
                patch.object(setup.os, "geteuid", return_value=123),
                patch.object(setup.os, "getegid", return_value=456),
                patch.object(setup, "_chown_recursive_if_needed") as chown,
                patch.object(setup.os, "chmod") as chmod,
            ):
                result = setup._prepare_shared_build_cache(cache_dir)

            self.assertEqual(cache_dir, result)
            self.assertTrue(Path(cache_dir).is_dir())
            chown.assert_called_once_with(cache_dir, 123, 456)
            chmod.assert_called_once_with(cache_dir, 0o755)

    def test_runtime_install_is_frozen_first_with_guarded_refresh_fallback(self):
        setup_source = Path("utils/setup.py").read_text(encoding="utf-8")

        self.assertIn('"--frozen-lockfile"', setup_source)
        self.assertIn('"--no-frozen-lockfile"', setup_source)
        self.assertIn('"verify-store-integrity": "true"', setup_source)
        self.assertNotIn('"install", "--force"', setup_source)

    def test_poetry_sync_uses_a_separate_controller_tool_environment(self):
        setup_source = Path("utils/setup.py").read_text(encoding="utf-8")
        process_source = Path("utils/processes.py").read_text(encoding="utf-8")

        self.assertIn('"poetry-tool"', setup_source)
        self.assertIn(
            '[poetry_tool_python, "-m", "pip", "install", "poetry"]',
            setup_source,
        )
        self.assertNotIn(
            '[python_executable, "-m", "pip", "install", "poetry"]',
            setup_source,
        )
        self.assertIn('"corepack_prepare"', process_source)
        self.assertIn("simonc56/python-plexapi.git", setup_source)
        self.assertIn("bef199e4e1ce27569091a3ebc35e49c2777e2a9c", setup_source)
        self.assertIn(
            'riven_data_dir = os.path.join(config["config_dir"], "data")',
            setup_source,
        )
        self.assertIn("source_managed_by_service_setup = key in {", setup_source)
        self.assertIn('"/opt/emby-server/bin/emby-server",', setup_source)
        self.assertIn('config["env"]["EMBY_DATA"] = emby_config_dir', setup_source)

    def test_arr_binary_resolution_prefers_native_apphost_before_dll(self):
        setup_source = Path("utils/setup.py").read_text(encoding="utf-8")
        helper = setup_source.split("def _find_arr_binary", 1)[1].split(
            "def _binary_interpreter_exists", 1
        )[0]

        self.assertLess(helper.index("native_names ="), helper.index("managed_names ="))
        self.assertIn("os.path.join(install_dir, app_name)", helper)

    def test_decypharr_build_surfaces_immediate_compiler_failure(self):
        process_handler = Mock()
        process_handler.start_process.return_value = (
            False,
            "go_build failed to stay running.",
        )
        process_handler.stderr = "fatal error: fuse.h: No such file or directory"
        process_handler.stdout = ""

        with patch.object(setup, "setup_pnpm_environment", return_value=(True, None)):
            success, error = setup.build_decypharr_dev(
                process_handler,
                {"config_dir": "/decypharr", "branch": "beta"},
            )

        self.assertFalse(success)
        self.assertIn("fatal error: fuse.h: No such file or directory", error)
        self.assertEqual(3, process_handler.start_process.call_count)
        process_handler.wait.assert_not_called()


if __name__ == "__main__":
    unittest.main()
