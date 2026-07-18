import contextlib
import unittest
from unittest.mock import call, mock_open, patch

from utils.jellyfin import JellyfinInstaller


class JellyfinInstallerTests(unittest.TestCase):
    def test_install_does_not_require_add_apt_repository(self):
        installer = JellyfinInstaller()
        source_file = mock_open()

        with (
            patch("utils.jellyfin.run_locked") as run_locked,
            patch("utils.jellyfin.apt_lock", return_value=contextlib.nullcontext()),
            patch.object(installer, "download_and_install_jellyfin_gpg_key"),
            patch(
                "utils.jellyfin.subprocess.check_output",
                side_effect=["ubuntu\n", "resolute\n", "amd64\n"],
            ),
            patch("utils.jellyfin.os.makedirs"),
            patch("utils.jellyfin.os.path.exists", return_value=True),
            patch("builtins.open", source_file),
        ):
            success, error = installer.install_jellyfin_server()

        self.assertTrue(success, error)
        self.assertEqual(
            run_locked.call_args_list,
            [
                call(["apt", "update"], check=True),
                call(["apt", "install", "-y", "gnupg", "curl"], check=True),
                call(["apt", "update"], check=True),
                call(["apt", "install", "-y", "jellyfin"], check=True),
            ],
        )
        self.assertFalse(
            any(
                args[0] and args[0][0] == "add-apt-repository"
                for args, _kwargs in run_locked.call_args_list
            )
        )
        written_source = "".join(
            item.args[0] for item in source_file().write.call_args_list
        )
        self.assertIn("URIs: https://repo.jellyfin.org/ubuntu", written_source)
        self.assertIn("Suites: resolute", written_source)


if __name__ == "__main__":
    unittest.main()
