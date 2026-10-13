import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yaml import safe_load

from utils import setup
from utils.zurg_config import ZurgConfigError


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

    def config_file(self, content):
        temporary = tempfile.TemporaryDirectory(prefix="dumb-zurg-test-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "config.yml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_root_updates_preserve_provider_settings_and_comments(self):
        path = self.config_file(
            "# keep this heading\n"
            "port: 9999 # listener guidance\n"
            "token: old # token guidance\n"
            "username: old_user\n"
            "password: old_password\n"
            "providers:\n"
            "  - type: nzb\n"
            "    nntp:\n"
            "      port: 563 # provider guidance\n"
            "      token: nested_token\n"
            "      username: nested_user\n"
            "      password: nested_password\n"
            "      servers:\n"
            "        - port: 119\n"
            "          username: other_user\n"
            "          password: other_password\n"
        )
        original_provider = safe_load(path.read_text())["providers"]

        setup.update_port(path, 8888)
        setup.update_token(path, "new_token")
        setup.update_creds(path, "new_user", "new_password")

        content = path.read_text()
        parsed = safe_load(content)
        self.assertEqual(parsed["providers"], original_provider)
        self.assertEqual(parsed["port"], 8888)
        self.assertEqual(parsed["token"], "new_token")
        self.assertEqual(parsed["username"], "new_user")
        self.assertEqual(parsed["password"], "new_password")
        for comment in (
            "keep this heading",
            "listener guidance",
            "token guidance",
            "provider guidance",
        ):
            self.assertIn(comment, content)

    def test_indented_and_quoted_root_keys_receive_configured_values(self):
        for indentation, quote in (("", ""), ("  ", ""), ("  ", '"'), ("  ", "'")):
            with self.subTest(indentation=indentation, quote=quote):
                content = "".join(
                    f"{indentation}{quote}{key}{quote}: {value}\n"
                    for key, value in (
                        ("port", "9999"),
                        ("token", "old"),
                        ("username", "old_user"),
                        ("password", "old_password"),
                    )
                )
                path = self.config_file(content)
                self.assertIsInstance(safe_load(content), dict)

                setup.update_port(path, "8888")
                setup.update_token(path, "new_token")
                setup.update_creds(path, "new_user", "new_password")

                self.assertEqual(
                    safe_load(path.read_text()),
                    {
                        "port": 8888,
                        "token": "new_token",
                        "username": "new_user",
                        "password": "new_password",
                    },
                )
                self.assertEqual(setup.get_port_from_config(path), "8888")

    def test_commented_defaults_upsert_active_root_keys(self):
        path = self.config_file(
            "# port: 9999\n# token: <token>\n"
            "# username: <username>\n# password: <password>\n"
            "providers:\n  - nntp:\n"
            "      # port: 119\n      port: 563\n"
        )

        setup.update_port(path, 8888)
        setup.update_token(path, "new_token")
        setup.update_creds(path, "new_user", "new_password")

        content = path.read_text()
        parsed = safe_load(content)
        self.assertEqual(parsed["port"], 8888)
        self.assertEqual(parsed["token"], "new_token")
        self.assertEqual(parsed["username"], "new_user")
        self.assertEqual(parsed["password"], "new_password")
        self.assertEqual(parsed["providers"][0]["nntp"]["port"], 563)
        self.assertIn("# username: <username>", content)
        self.assertIn("# port: 119", content)

    def test_disabling_auth_removes_only_root_credentials(self):
        for username, password in (("", ""), ("", "present"), ("present", "")):
            with self.subTest(username=username, password=password):
                path = self.config_file(
                    "username: old_user\npassword: old_password\n"
                    "providers:\n  - nntp:\n"
                    "      username: nested_user\n"
                    "      password: nested_password # provider guidance\n"
                )
                setup.update_creds(path, username, password)
                content = path.read_text()
                parsed = safe_load(content)
                self.assertNotIn("username", parsed)
                self.assertNotIn("password", parsed)
                self.assertEqual(
                    parsed["providers"][0]["nntp"],
                    {"username": "nested_user", "password": "nested_password"},
                )
                self.assertIn("provider guidance", content)
                self.assertNotIn("old_user", content)
                self.assertNotIn("old_password", content)

    def test_managed_strings_are_quoted_without_type_or_yaml_injection(self):
        values = (
            "true",
            "no",
            "00123",
            "value: # literal",
            "line one\nport: 1234",
            "quote\" and ' and \\",
        )
        for value in values:
            with self.subTest(value=value):
                path = self.config_file("port: 9999\n")
                setup.update_token(path, value)
                setup.update_creds(path, value, value)
                parsed = safe_load(path.read_text())
                self.assertEqual(
                    parsed,
                    {
                        "port": 9999,
                        "token": value,
                        "username": value,
                        "password": value,
                    },
                )

    def test_literal_blocks_and_nested_commented_keys_remain_content(self):
        path = self.config_file(
            "port: 9999\n"
            "note: |\n  port: 1234\n  token: text\n  # username: text\n"
            "providers:\n  - nntp:\n"
            "      # username: provider_default\n"
            "      username: provider_user\n"
        )
        original = safe_load(path.read_text())
        setup.update_port(path, 8888)
        setup.update_token(path, "new_token")
        setup.update_creds(path, "new_user", "new_password")
        parsed = safe_load(path.read_text())
        self.assertEqual(parsed["note"], original["note"])
        self.assertEqual(parsed["providers"], original["providers"])
        self.assertIn("# username: provider_default", path.read_text())

    def test_flow_mapping_receives_root_updates(self):
        path = self.config_file("{port: 9999, token: old, providers: [{port: 563}]}\n")
        setup.update_port(path, 8888)
        setup.update_token(path, "new_token")
        self.assertEqual(
            safe_load(path.read_text()),
            {"port": 8888, "token": "new_token", "providers": [{"port": 563}]},
        )

    def test_root_scalar_alias_update_keeps_nested_original_value(self):
        path = self.config_file("token: &shared old\napi:\n  token: *shared\n")
        setup.update_token(path, "new_token")
        self.assertEqual(
            safe_load(path.read_text()), {"token": "new_token", "api": {"token": "old"}}
        )

    def test_invalid_documents_are_unchanged_and_errors_hide_source_values(self):
        documents = (
            "token: fixture_sensitive_marker\nproviders: [\n",
            "port: 9999\nport: 8888\n",
            "- port: 9999\n",
            "fixture_sensitive_marker\n",
            "port: 9999\n---\nport: 8888\n",
            "# only a comment\n",
        )
        operations = (
            (setup.update_port, (8888,)),
            (setup.update_token, ("new",)),
            (setup.update_creds, ("user", "password")),
            (setup.update_creds, ("", "")),
        )
        for content in documents:
            for operation, arguments in operations:
                with self.subTest(content=content, operation=operation.__name__):
                    path = self.config_file(content)
                    path.chmod(0o640)
                    original_stat = path.stat()
                    with self.assertRaises(ZurgConfigError) as raised:
                        operation(path, *arguments)
                    self.assertNotIn("fixture_sensitive_marker", str(raised.exception))
                    self.assertEqual(path.read_text(), content)
                    self.assertEqual(path.stat().st_ino, original_stat.st_ino)
                    self.assertEqual(path.stat().st_mode & 0o777, 0o640)
                    self.assertEqual(list(path.parent.glob(".config.yml.*.tmp")), [])

    def test_invalid_requested_ports_leave_existing_file_unchanged(self):
        for port in (True, 0, -1, 65536, "not_a_port", 8888.5):
            with self.subTest(port=port):
                path = self.config_file("port: 9999\n")
                with self.assertRaises(ZurgConfigError):
                    setup.update_port(path, port)
                self.assertEqual(path.read_text(), "port: 9999\n")

    def test_atomic_replace_failure_retains_original_and_cleans_stage(self):
        path = self.config_file("port: 9999\ntoken: old\n")
        path.chmod(0o640)
        original = path.read_text()
        original_stat = path.stat()
        with patch(
            "utils.private_files.os.replace",
            side_effect=OSError("fixture rename failure"),
        ):
            with self.assertRaises(OSError):
                setup.update_token(path, "new_token")
        self.assertEqual(path.read_text(), original)
        self.assertEqual(path.stat().st_ino, original_stat.st_ino)
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(list(path.parent.glob(".config.yml.*.tmp")), [])

    def test_atomic_updates_retain_owner_and_enforce_private_permissions(self):
        path = self.config_file("port: 9999\n")
        path.chmod(0o644)
        original_owner = (path.stat().st_uid, path.stat().st_gid)
        setup.update_port(path, 8888)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual((path.stat().st_uid, path.stat().st_gid), original_owner)
        self.assertEqual(list(path.parent.glob(".config.yml.*.tmp")), [])

    def test_atomic_update_preserves_config_file_symlink_and_updates_target(self):
        target = self.config_file("port: 9999\n")
        path = target.parent / "linked.yml"
        path.symlink_to(target)
        setup.update_port(path, 8888)
        self.assertTrue(path.is_symlink())
        self.assertEqual(path.resolve(), target)
        self.assertEqual(safe_load(path.read_text())["port"], 8888)
        self.assertEqual(safe_load(target.read_text())["port"], 8888)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_port_reader_uses_root_key_when_nested_port_comes_first(self):
        path = self.config_file(
            "providers:\n  - nntp:\n      port: 563\n'port': 8888\n"
        )
        self.assertEqual(setup.get_port_from_config(path), "8888")

    def test_port_reader_returns_default_when_only_nested_port_exists(self):
        path = self.config_file("providers:\n  - nntp:\n      port: 563\n")
        self.assertEqual(setup.get_port_from_config(path), "9999")

    def test_port_reader_invalid_document_keeps_default_and_sanitizes_log(self):
        path = self.config_file("token: fixture_sensitive_marker\nproviders: [\n")
        with patch.object(setup.logger, "error") as error:
            self.assertEqual(setup.get_port_from_config(path), "9999")
        self.assertNotIn("fixture_sensitive_marker", str(error.call_args))

    def test_inherited_auth_cannot_silently_survive_credential_removal(self):
        content = "defaults: &auth\n  username: nested_user\n  password: nested_password\n<<: *auth\nport: 9999\n"
        path = self.config_file(content)
        with self.assertRaisesRegex(ZurgConfigError, "inherited"):
            setup.update_creds(path, "", "")
        self.assertEqual(path.read_text(), content)


if __name__ == "__main__":
    unittest.main()
