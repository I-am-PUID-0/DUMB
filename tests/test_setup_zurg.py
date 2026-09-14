import os
import tempfile
import unittest
import yaml
from pathlib import Path
from unittest.mock import patch

from utils import setup


class SetupZurgTests(unittest.TestCase):
    def test_install_only_creates_target_for_dangling_data_symlink(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            data_target = root / "data" / "zurg_RD"
            config_link = root / "zurg" / "RD"
            config_link.parent.mkdir()
            config_link.symlink_to(data_target)
            instance = {
                "enabled": True,
                "process_name": "Zurg w/ RealDebrid",
                "repo_owner": "debridmediamanager",
                "repo_name": "zurg-testing",
                "release_version_enabled": True,
                "release_version": "v0.9.3-hotfix.11",
                "config_dir": str(config_link),
                "exclude_dirs": [],
                "api_key": "",
            }

            def config_get(key):
                if key == "zurg":
                    return {"instances": {"RealDebrid": instance}}
                return 1000

            def download(**kwargs):
                binary = Path(kwargs["target_dir"]) / "zurg"
                binary.write_bytes(b"binary")
                return True, None

            with (
                patch.object(setup.CONFIG_MANAGER, "get", side_effect=config_get),
                patch.object(setup, "chown_recursive"),
                patch.object(
                    setup.downloader,
                    "download_release_version",
                    side_effect=download,
                ),
                patch.object(setup.downloader, "set_permissions") as permissions,
            ):
                success, error = setup.zurg_setup(install_only=True)

            self.assertTrue(success, error)
            self.assertTrue(data_target.is_dir())
            permissions.assert_called_once_with(str(config_link / "zurg"), 0o755)

    def test_update_port_preserves_nested_keys(self):
        """Test that update_port only updates top-level port key, not nested ones."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            self.addCleanup(os.unlink, f.name)
            config_content = """port: 9999
providers:
  - type: nzb
    nntp:
      host: news.example.com
      port: 563
      tls: true
      username: someuser
      password: somepass
      connections: 25
      servers:
        - host: other.example.com
          port: 563
          username: u2
          password: p2
"""
            f.write(config_content)
            f.flush()

            setup.update_port(f.name, 8888)

            with open(f.name, 'r') as result:
                updated_content = result.read()
                parsed = yaml.safe_load(updated_content)

            # Verify top-level port was updated
            self.assertEqual(parsed['port'], 8888)
            # Verify nested ports were NOT updated
            self.assertEqual(parsed['providers'][0]['nntp']['port'], 563)
            self.assertEqual(parsed['providers'][0]['nntp']['servers'][0]['port'], 563)
            # Verify nested credentials are still present
            self.assertEqual(parsed['providers'][0]['nntp']['username'], 'someuser')
            self.assertEqual(parsed['providers'][0]['nntp']['password'], 'somepass')

    def test_update_token_preserves_structure(self):
        """Test that update_token only updates top-level token key."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            self.addCleanup(os.unlink, f.name)
            config_content = """token: old_token
api:
  key: some_nested_key
  token: nested_token
"""
            f.write(config_content)
            f.flush()

            setup.update_token(f.name, 'new_token')

            with open(f.name, 'r') as result:
                updated_content = result.read()
                parsed = yaml.safe_load(updated_content)

            # Verify top-level token was updated
            self.assertEqual(parsed['token'], 'new_token')
            # Verify nested token was NOT updated
            self.assertEqual(parsed['api']['token'], 'nested_token')

    def test_update_creds_preserves_nested_credentials(self):
        """Test that update_creds only updates top-level credential keys."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yml', delete=False) as f:
            self.addCleanup(os.unlink, f.name)
            config_content = """username: olduser
password: oldpass
providers:
  - type: nzb
    nntp:
      host: news.example.com
      port: 563
      username: nesteduser
      password: nestedpass
"""
            f.write(config_content)
            f.flush()

            setup.update_creds(f.name, 'newuser', 'newpass')

            with open(f.name, 'r') as result:
                updated_content = result.read()
                parsed = yaml.safe_load(updated_content)

            # Verify top-level credentials were updated
            self.assertEqual(parsed['username'], 'newuser')
            self.assertEqual(parsed['password'], 'newpass')
            # Verify nested credentials were NOT updated
            self.assertEqual(parsed['providers'][0]['nntp']['username'], 'nesteduser')
            self.assertEqual(parsed['providers'][0]['nntp']['password'], 'nestedpass')


if __name__ == "__main__":
    unittest.main()
