import hashlib
import io
import tarfile
import threading
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils import arr, setup


def write_whisparr_runtime(binary, version):
    binary = Path(binary)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_bytes(b"synthetic native runtime")
    binary.with_name("Whisparr.Core.dll").write_bytes(
        f"Whisparr.Common, Version={version}\x00".encode("ascii")
    )
    return binary


class WhisparrSetupTests(unittest.TestCase):
    def test_managed_directory_stays_per_instance_across_install_modes(self):
        modes = (
            {},
            {"branch_enabled": True, "branch": "eros"},
            {"release_version_enabled": True, "release_version": "latest"},
        )
        for mode in modes:
            with self.subTest(mode=mode):
                config = {"whisparr_version": "v3", **mode}
                target, private = setup._resolve_arr_install_dir(
                    "whisparr", "Adult Media", config
                )
                self.assertEqual(target, "/opt/whisparr/instances/adult_media")
                self.assertTrue(private)

    def test_conflicting_database_is_rejected_before_directory_or_download(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp, "data")
            data.mkdir()
            database = data / "whisparr3.db"
            database.write_bytes(b"preserve database")
            target = Path(temp, "install")
            config = {"config_dir": str(data), "whisparr_version": "v2"}
            installer = arr.ArrInstaller(
                "whisparr", install_dir=str(target), whisparr_config=config
            )
            with patch.object(arr, "Downloader") as downloader:
                success, error = installer.install()
            self.assertFalse(success)
            self.assertIn("existing v3 database", error)
            self.assertFalse(target.exists())
            self.assertEqual(database.read_bytes(), b"preserve database")
            downloader.assert_not_called()

    def test_latest_release_uses_selected_official_repository_and_numeric_version(self):
        for flavor, repo, tag, version in (
            ("v2", "Whisparr", "v2.2.0-release.231", "2.2.0.231"),
            ("v3", "Whisparr-Eros", "v3.6.4-release.1865", "3.6.4.1865"),
        ):
            with self.subTest(flavor=flavor):
                config = {
                    "whisparr_version": flavor,
                    "release_version_enabled": True,
                    "release_version": "latest",
                }
                installer = arr.ArrInstaller("whisparr", whisparr_config=config)
                downloader = Mock()
                downloader.get_latest_release.return_value = (tag, None)
                with patch.object(arr, "Downloader", return_value=downloader):
                    actual, error = installer.get_latest_version()
                self.assertEqual(actual, version)
                self.assertIsNone(error)
                downloader.get_latest_release.assert_called_once_with(
                    "Whisparr", repo, prerelease=False
                )
                self.assertEqual(installer.app_name_cap, "Whisparr")

    def test_legacy_v3_alias_uses_canonical_repository_and_binary_name(self):
        installer = arr.ArrInstaller("whisparr-v3")
        downloader = Mock()
        downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)
        with patch.object(arr, "Downloader", return_value=downloader):
            actual, error = installer.get_latest_version()
        self.assertEqual(actual, "3.6.4.1865")
        self.assertIsNone(error)
        self.assertEqual(installer.app_name, "whisparr")
        self.assertEqual(installer.install_dir, "/opt/whisparr")
        downloader.get_latest_release.assert_called_once_with(
            "Whisparr", "Whisparr-Eros", prerelease=False
        )

    def test_wrong_family_latest_release_is_rejected(self):
        installer = arr.ArrInstaller(
            "whisparr", whisparr_config={"whisparr_version": "v3"}
        )
        downloader = Mock()
        downloader.get_latest_release.return_value = ("v2.2.0-release.231", None)
        with patch.object(arr, "Downloader", return_value=downloader):
            version, error = installer.get_latest_version()
        self.assertIsNone(version)
        self.assertIn("does not match selected v3", error)

    def test_numeric_pin_resolves_unique_published_release_tag(self):
        installer = arr.ArrInstaller(
            "whisparr",
            whisparr_config={"whisparr_version": "v3", "pinned_version": "3.6.4.1865"},
        )
        downloader = Mock()
        downloader.fetch_github_release_info.return_value = (
            None,
            "no exact numeric tag",
        )
        response = Mock(status_code=200)
        response.json.return_value = [
            {"tag_name": "v3.6.4-release.1865", "draft": False}
        ]
        downloader.fetch_with_retries.return_value = response
        with patch.object(arr, "Downloader", return_value=downloader):
            self.assertEqual(installer.get_latest_version(), ("3.6.4.1865", None))
        self.assertEqual(installer._whisparr_release_tag, "v3.6.4-release.1865")

    def test_ambiguous_numeric_pin_requires_exact_tag(self):
        installer = arr.ArrInstaller(
            "whisparr",
            whisparr_config={"whisparr_version": "v3", "pinned_version": "3.6.4.1865"},
        )
        downloader = Mock()
        downloader.fetch_github_release_info.return_value = (
            None,
            "no exact numeric tag",
        )
        response = Mock(status_code=200)
        response.json.return_value = [
            {"tag_name": "v3.6.4-release.1865"},
            {"tag_name": "v3.6.4-develop.1865"},
        ]
        downloader.fetch_with_retries.return_value = response
        with patch.object(arr, "Downloader", return_value=downloader):
            version, error = installer.get_latest_version()
        self.assertIsNone(version)
        self.assertIn("no unique published release", error)

    def _release_install(self, actual_version, failure=None, before_activate=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "install"
            old = write_whisparr_runtime(target / "Whisparr" / "Whisparr", "3.6.2.1727")
            config = {
                "whisparr_version": "v3",
                "command": [str(old)],
                "release_version_enabled": True,
                "release_version": "latest",
            }
            installer = arr.ArrInstaller(
                "whisparr", install_dir=str(target), whisparr_config=config
            )
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)

            def download(**kwargs):
                if failure:
                    return False, failure
                write_whisparr_runtime(
                    Path(kwargs["target_dir"]) / "Whisparr", actual_version
                )
                return True, None

            downloader.download_release_version.side_effect = download
            activate = before_activate or Mock()
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = installer.install(force=True, before_activate=activate)
            remaining = sorted(
                p.name
                for p in root.iterdir()
                if "candidate" in p.name or "previous" in p.name
            )
            final = old.with_name("Whisparr.Core.dll").read_bytes()
            return success, error, activate, remaining, final, downloader

    def test_validated_candidate_activates_and_preserves_canonical_layout(self):
        success, error, activate, remaining, final, downloader = self._release_install(
            "3.6.4.1865"
        )
        self.assertTrue(success)
        self.assertIsNone(error)
        activate.assert_called_once_with()
        self.assertEqual(remaining, [])
        self.assertIn(b"Version=3.6.4.1865", final)
        kwargs = downloader.download_release_version.call_args.kwargs
        self.assertEqual(
            (kwargs["key"], kwargs["repo_owner"], kwargs["repo_name"]),
            ("whisparr", "Whisparr", "Whisparr-Eros"),
        )

    def test_wrong_major_candidate_preserves_old_binary_and_does_not_stop(self):
        success, error, activate, remaining, final, _ = self._release_install(
            "2.2.0.231"
        )
        self.assertFalse(success)
        self.assertIn("does not match selected v3", error)
        activate.assert_not_called()
        self.assertEqual(remaining, [])
        self.assertIn(b"Version=3.6.2.1727", final)

    def test_wrong_full_version_candidate_preserves_old_binary(self):
        success, error, activate, remaining, final, _ = self._release_install(
            "3.6.4.1866"
        )
        self.assertFalse(success)
        self.assertIn("selected release is 3.6.4.1865", error)
        activate.assert_not_called()
        self.assertEqual(remaining, [])
        self.assertIn(b"Version=3.6.2.1727", final)

    def test_failed_download_preserves_runtime_and_removes_candidate(self):
        success, error, activate, remaining, final, _ = self._release_install(
            None, failure="checksum mismatch"
        )
        self.assertFalse(success)
        self.assertEqual(error, "checksum mismatch")
        activate.assert_not_called()
        self.assertEqual(remaining, [])
        self.assertIn(b"Version=3.6.2.1727", final)

    def test_configure_only_validates_before_rewriting_xml_or_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            binary = write_whisparr_runtime(
                root / "install" / "Whisparr" / "Whisparr", "2.2.0.231"
            )
            xml = root / "config.xml"
            xml.write_text("<Config><Branch>nightly</Branch></Config>")
            config = {
                "whisparr_version": "v3",
                "config_dir": str(root),
                "config_file": str(xml),
                "command": ["original"],
            }
            with patch.object(
                setup,
                "_resolve_arr_install_dir",
                return_value=(str(root / "install"), True),
            ):
                success, error = setup.setup_arr_instance(
                    "whisparr", "adult", config, "Whisparr Adult", configure_only=True
                )
            self.assertFalse(success)
            self.assertIn("does not match selected v3", error)
            self.assertEqual(
                xml.read_text(), "<Config><Branch>nightly</Branch></Config>"
            )
            self.assertEqual(config["command"], ["original"])
            self.assertTrue(binary.is_file())

    def test_configure_writes_matching_native_update_identity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_dir = root / "install"
            binary = write_whisparr_runtime(
                install_dir / "Whisparr" / "Whisparr", "3.6.4.1865"
            )
            data = root / "data"
            xml = data / "config.xml"
            config = {
                "whisparr_version": "v3",
                "branch": "nightly",
                "config_dir": str(data),
                "config_file": str(xml),
                "port": 6969,
            }
            saved = []
            with (
                patch.object(
                    setup,
                    "_resolve_arr_install_dir",
                    return_value=(str(install_dir), True),
                ),
                patch.object(
                    setup.CONFIG_MANAGER,
                    "save_config",
                    side_effect=lambda name: saved.append(
                        (config["install_dir"], list(config["command"]))
                    ),
                ),
                patch.object(setup, "chown_single"),
                patch.object(setup, "apply_arr_postgres_config"),
            ):
                success, error = setup.setup_arr_instance(
                    "whisparr", "adult", config, "Whisparr Adult", configure_only=True
                )
            self.assertTrue(success, error)
            self.assertIsNone(error)
            text = xml.read_text()
            self.assertIn("<Branch>eros</Branch>", text)
            self.assertIn(
                "<GithubOwnerRepo>Whisparr/Whisparr-Eros</GithubOwnerRepo>", text
            )
            self.assertEqual(
                config["command"], [str(binary), "--nobrowser", f"--data={data}"]
            )
            self.assertEqual(
                saved,
                [(str(install_dir), [str(binary), "--nobrowser", f"--data={data}"])],
            )

    def test_other_arr_install_path_rules_are_unchanged(self):
        self.assertEqual(
            setup._resolve_arr_install_dir("radarr", "Movies", {}),
            ("/opt/radarr", False),
        )
        self.assertEqual(
            setup._resolve_arr_install_dir(
                "radarr",
                "Movies",
                {
                    "repo_owner": "Radarr",
                    "repo_name": "Radarr",
                    "release_version_enabled": True,
                },
            ),
            ("/opt/radarr/instances/movies", True),
        )

    def test_shared_downloader_extracts_real_tar_into_canonical_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            archive = io.BytesIO()
            files = {
                "Whisparr/Whisparr": b"synthetic native runtime",
                "Whisparr/Whisparr.Core.dll": b"Whisparr.Common, Version=3.6.4.1865\x00",
            }
            with tarfile.open(fileobj=archive, mode="w:gz") as tar:
                for name, payload in files.items():
                    member = tarfile.TarInfo(name)
                    member.size = len(payload)
                    member.mode = 0o755 if name.endswith("/Whisparr") else 0o644
                    tar.addfile(member, io.BytesIO(payload))
            payload = archive.getvalue()
            release = {
                "tag_name": "v3.6.4-release.1865",
                "assets": [
                    {
                        "name": "Whisparr.3.6.4.1865.linux-x64.tar.gz",
                        "id": 1,
                        "browser_download_url": "https://example.invalid/whisparr.tar.gz",
                        "digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
                    }
                ],
            }
            response = SimpleNamespace(
                status_code=200,
                content=payload,
                headers={
                    "Content-Disposition": 'attachment; filename="Whisparr.3.6.4.1865.linux-x64.tar.gz"',
                    "Content-Type": "application/gzip",
                },
            )
            downloader = arr.Downloader()
            downloader.logger = Mock()
            cache = Mock()
            cache.lookup_download.return_value = (None, {})
            original_globals = downloader.download_and_extract.__func__.__globals__
            config = {
                "whisparr_version": "v3",
                "config_dir": str(root / "data"),
                "release_version_enabled": True,
                "release_version": "latest",
            }
            target = root / "install"
            installer = arr.ArrInstaller(
                "whisparr", install_dir=str(target), whisparr_config=config
            )
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch.object(
                    downloader,
                    "get_latest_release",
                    return_value=(release["tag_name"], None),
                ),
                patch.object(
                    downloader,
                    "fetch_github_release_info",
                    return_value=(release, None),
                ),
                patch.object(downloader, "fetch_with_retries", return_value=response),
                patch.object(downloader, "get_headers", return_value={}),
                patch.object(arr.platform, "machine", return_value="x86_64"),
                patch.object(arr.platform, "libc_ver", return_value=("glibc", "2.39")),
                patch.dict(original_globals, {"INSTALL_CACHE": cache}),
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = installer.install(force=True)
            self.assertTrue(success, error)
            self.assertEqual(
                (target / "Whisparr" / "Whisparr").read_bytes(),
                files["Whisparr/Whisparr"],
            )
            self.assertEqual(
                (target / "Whisparr" / "Whisparr.Core.dll").read_bytes(),
                files["Whisparr/Whisparr.Core.dll"],
            )
            self.assertFalse((target / "Whisparr" / "Whisparr" / "Whisparr").exists())
            self.assertFalse(
                any(
                    "candidate" in child.name or "extract" in child.name
                    for child in root.iterdir()
                )
            )

    def test_data_inside_binary_directory_is_refused_before_download(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp, "install")
            data = target / "data"
            data.mkdir(parents=True)
            database = data / "whisparr3.db"
            database.write_bytes(b"preserve this database")
            installer = arr.ArrInstaller(
                "whisparr",
                install_dir=str(target),
                whisparr_config={"whisparr_version": "v3", "config_dir": str(data)},
            )
            with patch.object(arr, "Downloader") as downloader:
                success, error = installer.install(force=True)
            self.assertFalse(success)
            self.assertIn("must be separate", error)
            downloader.assert_not_called()
            self.assertEqual(database.read_bytes(), b"preserve this database")

    def test_protected_leaf_cannot_hide_writable_install_parent(self):
        with tempfile.TemporaryDirectory() as temp:
            unsafe_parent = Path(temp, "unsafe")
            unsafe_parent.mkdir(mode=0o777)
            unsafe_parent.chmod(0o777)
            protected_leaf = unsafe_parent / "protected"
            protected_leaf.mkdir(mode=0o700)
            installer = arr.ArrInstaller(
                "whisparr",
                install_dir=str(protected_leaf / "install"),
                whisparr_config={"whisparr_version": "v3"},
            )
            downloader = Mock()
            downloader.get_latest_release.return_value = ("v3.6.4-release.1865", None)
            with patch.object(arr, "Downloader", return_value=downloader):
                success, error = installer.install(force=True)
            self.assertFalse(success)
            self.assertIn("controller-owned parent directories", error)
            downloader.download_release_version.assert_not_called()

    def test_outer_setup_rejects_database_conflict_before_clear_or_install(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp, "whisparr3.db")
            database.write_bytes(b"preserve")
            config = {
                "whisparr_version": "v2",
                "config_dir": temp,
                "clear_on_update": True,
            }
            handler = Mock()
            handler.setup_tracker_lock = threading.Lock()
            handler.setup_tracker = set()
            with (
                patch.object(
                    setup.CONFIG_MANAGER,
                    "find_key_for_process",
                    return_value=("whisparr", "Adult"),
                ),
                patch.object(setup.CONFIG_MANAGER, "get_instance", return_value=config),
                patch.object(setup, "clear_directory") as clear,
                patch.object(setup, "install_arr_instances") as install,
            ):
                success, error = setup._setup_project_inner(
                    handler, "Whisparr Adult", True, False
                )
            self.assertFalse(success)
            self.assertIn("existing v3 database", error)
            clear.assert_not_called()
            install.assert_not_called()
            self.assertEqual(database.read_bytes(), b"preserve")

    def test_legacy_v2_runtime_is_adopted_without_network_or_database_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = write_whisparr_runtime(
                root / "old" / "Whisparr" / "Whisparr", "2.0.0.2151"
            )
            source.with_name("legacy-runtime-resource").write_bytes(
                b"old bundled resource"
            )
            source.with_name("logs.db").write_bytes(b"do not copy runtime database")
            data = root / "data"
            data.mkdir()
            database = data / "whisparr.db"
            database.write_bytes(b"preserve existing series library")
            target = root / "managed"
            config = {
                "config_dir": str(data),
                "command": [str(source)],
                "branch": "nightly",
                "release_version_enabled": True,
                "release_version": "latest",
            }
            installer = arr.ArrInstaller(
                "whisparr", install_dir=str(target), whisparr_config=config
            )
            with (
                patch.object(arr, "Downloader") as downloader,
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = installer.install()
            self.assertTrue(success, error)
            downloader.assert_not_called()
            canonical = target / "Whisparr" / "Whisparr"
            self.assertEqual(canonical.read_bytes(), source.read_bytes())
            self.assertEqual(
                canonical.with_name("Whisparr.Core.dll").read_bytes(),
                source.with_name("Whisparr.Core.dll").read_bytes(),
            )
            self.assertTrue(canonical.with_name("legacy-runtime-resource").is_file())
            self.assertFalse(canonical.with_name("logs.db").exists())
            self.assertEqual(database.read_bytes(), b"preserve existing series library")
            self.assertFalse((data / "whisparr2.db").exists())
            self.assertTrue(source.is_file())
            self.assertFalse(
                any(
                    "candidate" in child.name or "previous" in child.name
                    for child in root.iterdir()
                )
            )

    def test_existing_managed_legacy_runtime_is_retained_for_moving_startup(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "managed"
            binary = write_whisparr_runtime(
                target / "Whisparr" / "Whisparr", "2.0.0.2151"
            )
            data = root / "data"
            data.mkdir()
            (data / "whisparr.db").write_bytes(b"legacy library")
            installer = arr.ArrInstaller(
                "whisparr",
                install_dir=str(target),
                whisparr_config={
                    "config_dir": str(data),
                    "command": [str(binary)],
                    "release_version_enabled": True,
                    "release_version": "latest",
                },
            )
            with patch.object(arr, "Downloader") as downloader:
                success, error = installer.install()
            self.assertTrue(success, error)
            downloader.assert_not_called()
            self.assertIn(
                b"Version=2.0.0.2151",
                binary.with_name("Whisparr.Core.dll").read_bytes(),
            )

    def test_modern_v2_candidate_cannot_activate_on_legacy_only_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "managed"
            source = write_whisparr_runtime(
                root / "old" / "Whisparr" / "Whisparr", "2.0.0.2151"
            )
            data = root / "data"
            data.mkdir()
            database = data / "whisparr.db"
            database.write_bytes(b"legacy library")
            config = {
                "whisparr_version": "v2",
                "config_dir": str(data),
                "command": [str(source)],
                "release_version_enabled": True,
                "release_version": "v2.2.0-release.231",
            }
            installer = arr.ArrInstaller(
                "whisparr", install_dir=str(target), whisparr_config=config
            )
            downloader = Mock()
            downloader.fetch_github_release_info.return_value = (
                {"tag_name": "v2.2.0-release.231"},
                None,
            )

            def download(**kwargs):
                write_whisparr_runtime(
                    Path(kwargs["target_dir"]) / "Whisparr", "2.2.0.231"
                )
                return True, None

            downloader.download_release_version.side_effect = download
            activate = Mock()
            with (
                patch.object(arr, "Downloader", return_value=downloader),
                patch(
                    "utils.transactional_install.install_cache_root",
                    return_value=root / "cache",
                ),
            ):
                success, error = installer.install(before_activate=activate)
            self.assertFalse(success)
            self.assertIn("whisparr.db", error)
            activate.assert_not_called()
            self.assertFalse(target.exists())
            self.assertEqual(database.read_bytes(), b"legacy library")
            self.assertFalse((data / "whisparr2.db").exists())
            self.assertIn(
                b"Version=2.0.0.2151",
                source.with_name("Whisparr.Core.dll").read_bytes(),
            )

    def test_colliding_flavor_names_protect_disabled_runtime_before_download(self):
        with tempfile.TemporaryDirectory() as temp:
            source = write_whisparr_runtime(
                Path(temp) / "existing" / "Whisparr", "3.6.4.1865"
            )
            for first_name, disabled_name in (("A B", "a_b"), ("Adult", "adult")):
                with self.subTest(first_name=first_name, disabled_name=disabled_name):
                    current = {"whisparr_version": "v2", "enabled": True}
                    disabled = {
                        "whisparr_version": "v3",
                        "enabled": False,
                        "command": [str(source)],
                    }
                    configured = {
                        "instances": {first_name: current, disabled_name: disabled}
                    }
                    with (
                        patch.object(
                            setup.CONFIG_MANAGER, "get", return_value=configured
                        ),
                        patch.object(arr, "Downloader") as downloader,
                    ):
                        result = setup._install_arr_binary(
                            "whisparr", first_name, current, "Whisparr Example"
                        )
                    self.assertFalse(result[0])
                    self.assertIn("same managed runtime directory", result[1])
                    self.assertIn("disabled instances", result[1])
                    downloader.assert_not_called()
                    self.assertIn(
                        b"Version=3.6.4.1865",
                        source.with_name("Whisparr.Core.dll").read_bytes(),
                    )
