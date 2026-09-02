import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils import setup as setup_module


class PlexSetupTests(unittest.TestCase):
    def test_repairs_package_created_application_support_tree(self):
        with tempfile.TemporaryDirectory() as config_dir:
            application_support_dir = os.path.join(config_dir, "Plex Media Server")
            os.makedirs(application_support_dir)
            owner = (os.getuid(), os.getgid())
            real_stat = os.stat

            def fake_stat(path, *args, **kwargs):
                result = real_stat(path, *args, **kwargs)
                if os.fspath(path) == application_support_dir:
                    return SimpleNamespace(st_uid=owner[0] + 1, st_gid=owner[1] + 1)
                return result

            with (
                patch.object(
                    setup_module.os,
                    "stat",
                    side_effect=fake_stat,
                ),
                patch.object(
                    setup_module,
                    "chown_recursive",
                    return_value=(True, None),
                ) as chown,
            ):
                result = setup_module._ensure_plex_config_ownership(config_dir, *owner)

            self.assertEqual(result, (True, None))
            chown.assert_called_once_with(application_support_dir, *owner)

    def test_propagates_ownership_repair_failure(self):
        with tempfile.TemporaryDirectory() as config_dir:
            application_support_dir = os.path.join(config_dir, "Plex Media Server")
            os.makedirs(application_support_dir)
            target_owner = (os.getuid() + 1, os.getgid() + 1)
            with patch.object(
                setup_module,
                "chown_recursive",
                return_value=(False, "ownership failed"),
            ):
                result = setup_module._ensure_plex_config_ownership(
                    config_dir, *target_owner
                )

            self.assertEqual(result, (False, "ownership failed"))


if __name__ == "__main__":
    unittest.main()
