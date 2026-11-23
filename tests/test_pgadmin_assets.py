"""Missing pgAdmin web assets are rebuilt from authenticated matching source."""

import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.request import Request

from scripts import prepare_pgadmin_assets as pgadmin_assets


class PgAdminAssetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / "installed" / "pgadmin4"
        self.package.mkdir(parents=True)
        (self.package / "version.py").write_text("APP_VERSION_INT = 91900\n")
        (self.package / "config_local.py").write_text(
            "# Preserve local configuration\n"
        )
        (self.package / "users.sqlite").write_bytes(b"preserve database sentinel")
        generated = self.package / pgadmin_assets.ASSET_DIRECTORY
        generated.mkdir(parents=True)
        (generated / "style.css").write_bytes(b"preserve until successful build")
        (generated / "old.js").write_bytes(b"previous generated asset")
        self.archive = self.source_archive()
        self.signature = self.root / "source.tar.gz.asc"
        self.signature.write_text("fixture detached signature")
        self.fetch = Mock(return_value=(self.archive, self.signature))
        self.run = Mock(side_effect=self.build)
        verifier = patch.object(pgadmin_assets, "verify_publisher_signature")
        self.verify = verifier.start()
        self.addCleanup(verifier.stop)

    def source_archive(self, *, source_version=91900, extra=(), links=()):
        path = (
            self.root / f"source-{len(list(self.root.glob('source-*.tar.gz')))}.tar.gz"
        )
        entries = [
            (
                "pgadmin4-9.19/web/version.py",
                f"APP_VERSION_INT = {source_version}\n".encode(),
            ),
            ("pgadmin4-9.19/web/package.json", b"{}"),
            ("pgadmin4-9.19/web/yarn.lock", b"# fixture immutable lock"),
            ("pgadmin4-9.19/web/do_not_copy.py", b"# source only"),
            *extra,
        ]
        with tarfile.open(path, "w:gz") as archive:
            for name, contents in entries:
                member = tarfile.TarInfo(name)
                member.size = len(contents)
                archive.addfile(member, io.BytesIO(contents))
            for link in links:
                archive.addfile(link)
        return path

    def write_assets(self, root, *, missing=()):
        directory = Path(root) / pgadmin_assets.ASSET_DIRECTORY
        directory.mkdir(parents=True, exist_ok=True)
        for name in pgadmin_assets.REQUIRED_ASSETS:
            if name not in missing:
                (directory / name).write_bytes(f"compiled fixture {name}".encode())
        (directory / "chunk.lazy.js").write_bytes(b"additional compiled chunk")
        return directory

    def build(self, command, **kwargs):
        self.assertTrue(
            self.verify.called, "Build started before publisher verification"
        )
        if command[-1] == "bundle":
            self.write_assets(kwargs["cwd"])
        return SimpleNamespace(returncode=0)

    def prepare(self, **kwargs):
        return pgadmin_assets.prepare_assets(
            self.package, "9.19", fetch=self.fetch, run=self.run, **kwargs
        )

    def installed_files(self):
        return {
            path.relative_to(self.package): path.read_bytes()
            for path in self.package.rglob("*")
            if path.is_file()
        }

    def test_missing_assets_requires_exact_nonempty_compiled_files(self):
        self.write_assets(self.package)
        self.assertEqual([], pgadmin_assets.missing_assets(self.package))
        generated = self.package / pgadmin_assets.ASSET_DIRECTORY
        (generated / "style.css").unlink()
        (generated / "app.bundle.js").write_bytes(b"")
        self.assertEqual(
            ["style.css", "app.bundle.js"], pgadmin_assets.missing_assets(self.package)
        )

    def test_version_is_read_as_ast_without_executing_package_code(self):
        marker = self.root / "must-not-execute"
        path = self.root / "version.py"
        path.write_text(
            "from pathlib import Path\n"
            f"Path({str(marker)!r}).write_text('executed')\n"
            "raise RuntimeError('must not execute package code')\n"
            "APP_VERSION_INT = 91900\n"
        )
        self.assertEqual(91900, pgadmin_assets.version_number(path))
        self.assertFalse(marker.exists())

    def test_missing_boolean_or_nonliteral_version_is_refused_without_execution(self):
        marker = self.root / "must-not-execute"
        path = self.root / "version.py"
        for source in (
            "OTHER_VERSION = 91900\n",
            "APP_VERSION_INT = True\n",
            f"APP_VERSION_INT = __import__('pathlib').Path({str(marker)!r}).write_text('bad')\n",
        ):
            with self.subTest(source=source):
                path.write_text(source)
                with self.assertRaises(ValueError):
                    pgadmin_assets.version_number(path)
                self.assertFalse(marker.exists())

    def test_complete_install_skips_network_signature_and_build(self):
        self.write_assets(self.package)
        before = self.installed_files()
        self.assertFalse(self.prepare())
        self.assertEqual(before, self.installed_files())
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_installed_version_mismatch_refuses_before_fetch_or_mutation(self):
        (self.package / "version.py").write_text("APP_VERSION_INT = 91800\n")
        before = self.installed_files()
        with self.assertRaisesRegex(
            pgadmin_assets.PgAdminAssetError, "installed.*version"
        ):
            self.prepare()
        self.assertEqual(before, self.installed_files())
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()

    def test_unknown_or_unstable_version_never_starts_preparation(self):
        before = self.installed_files()
        for version in ("latest", "9.19rc1", "9.19.dev1", "v9.19", "9"):
            with self.subTest(version=version):
                with self.assertRaises(pgadmin_assets.PgAdminAssetError):
                    pgadmin_assets.prepare_assets(
                        self.package, version, fetch=self.fetch, run=self.run
                    )
        self.assertEqual(before, self.installed_files())
        self.fetch.assert_not_called()
        self.run.assert_not_called()

    def test_bad_signature_never_extracts_builds_or_mutates_installed_files(self):
        before = self.installed_files()
        self.verify.side_effect = ValueError("fixture signature failure")
        with patch.object(pgadmin_assets, "extract_source") as extract:
            with self.assertRaisesRegex(ValueError, "signature failure"):
                self.prepare()
        self.verify.assert_called_once_with(self.archive, self.signature, "pgadmin")
        extract.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual(before, self.installed_files())

    def test_signed_source_version_mismatch_never_builds_or_mutates_install(self):
        self.fetch.return_value = (
            self.source_archive(source_version=91800),
            self.signature,
        )
        before = self.installed_files()
        with self.assertRaisesRegex(pgadmin_assets.PgAdminAssetError, "source version"):
            self.prepare()
        self.verify.assert_called_once()
        self.run.assert_not_called()
        self.assertEqual(before, self.installed_files())

    def test_source_extraction_requires_exact_release_prefix(self):
        for name in (
            "other-release/web/outside.txt",
            "pgadmin4-9.19-other/web/outside.txt",
            "pgadmin4-9.19/../escaped.txt",
            "/absolute-path.txt",
        ):
            with self.subTest(name=name):
                archive = self.source_archive(extra=[(name, b"must not extract")])
                destination = self.root / "extract"
                destination.mkdir(exist_ok=True)
                with self.assertRaises(
                    (pgadmin_assets.PgAdminAssetError, tarfile.TarError)
                ):
                    pgadmin_assets.extract_source(archive, destination, "9.19")
                self.assertFalse((destination / "escaped.txt").exists())

    def test_source_extraction_data_filter_rejects_escaping_links(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            with self.subTest(kind=kind):
                link = tarfile.TarInfo("pgadmin4-9.19/web/unsafe")
                link.type = kind
                link.linkname = str(self.root / "outside")
                archive = self.source_archive(links=[link])
                destination = self.root / f"links-{kind.decode()}"
                destination.mkdir()
                with self.assertRaises(
                    (pgadmin_assets.PgAdminAssetError, tarfile.TarError)
                ):
                    pgadmin_assets.extract_source(archive, destination, "9.19")
                self.assertFalse((destination / "pgadmin4-9.19/web/unsafe").exists())

    def test_source_extraction_refuses_declared_oversized_archive_before_extracting(
        self,
    ):
        source = MagicMock()
        source.__enter__.return_value = source
        source.getmembers.return_value = [
            SimpleNamespace(name="pgadmin4-9.19/web/huge.bin", size=1024**3 + 1)
        ]
        with patch.object(pgadmin_assets.tarfile, "open", return_value=source):
            with self.assertRaises(pgadmin_assets.PgAdminAssetError):
                pgadmin_assets.extract_source(
                    self.archive, self.root / "bounded", "9.19"
                )
        source.extractall.assert_not_called()

    def test_failed_immutable_install_or_bundle_keeps_existing_files(self):
        before = self.installed_files()
        for failure_stage in ("install", "bundle"):
            with self.subTest(failure_stage=failure_stage):

                def fail(command, **kwargs):
                    if failure_stage in command:
                        raise subprocess.CalledProcessError(1, command)
                    return self.build(command, **kwargs)

                self.run.side_effect = fail
                with self.assertRaises(subprocess.CalledProcessError):
                    self.prepare()
                self.assertEqual(before, self.installed_files())

    def test_incomplete_compiled_output_does_not_replace_installed_assets(self):
        before = self.installed_files()

        def incomplete(command, **kwargs):
            if command[-1] == "bundle":
                self.write_assets(kwargs["cwd"], missing=("browser_nodes.js",))

        self.run.side_effect = incomplete
        with self.assertRaisesRegex(
            pgadmin_assets.PgAdminAssetError, "produce its assets"
        ):
            self.prepare()
        self.assertEqual(before, self.installed_files())

    def test_linked_generated_directory_is_rejected_without_mutating_install(self):
        before = self.installed_files()
        external = self.root / "external-build"
        assets = self.write_assets(external)

        def linked(command, **kwargs):
            if command[-1] == "bundle":
                generated = Path(kwargs["cwd"]) / pgadmin_assets.ASSET_DIRECTORY
                generated.parent.mkdir(parents=True)
                generated.symlink_to(assets, target_is_directory=True)

        self.run.side_effect = linked
        with self.assertRaisesRegex(pgadmin_assets.PgAdminAssetError, "linked"):
            self.prepare()
        self.assertEqual(before, self.installed_files())

    def test_existing_generated_symlink_refuses_before_network_or_build(self):
        generated = self.package / pgadmin_assets.ASSET_DIRECTORY
        for asset in generated.iterdir():
            asset.unlink()
        generated.rmdir()
        external = self.root / "external-assets"
        external.mkdir()
        generated.symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(pgadmin_assets.PgAdminAssetError, "symlink"):
            self.prepare()
        self.fetch.assert_not_called()
        self.verify.assert_not_called()
        self.run.assert_not_called()
        self.assertEqual([], list(external.iterdir()))

    def test_successful_matching_source_build_changes_only_generated_assets(self):
        before = self.installed_files()
        self.assertTrue(self.prepare())
        self.assertEqual([], pgadmin_assets.missing_assets(self.package))
        self.fetch.assert_called_once()
        self.assertEqual("9.19", self.fetch.call_args.args[0])
        self.verify.assert_called_once_with(self.archive, self.signature, "pgadmin")
        self.assertEqual(
            [
                ["corepack", "yarn", "install", "--immutable"],
                ["corepack", "yarn", "run", "bundle"],
            ],
            [call.args[0] for call in self.run.call_args_list],
        )
        for call in self.run.call_args_list:
            self.assertIs(True, call.kwargs["check"])
            self.assertEqual("production", call.kwargs["env"]["NODE_ENV"])
            self.assertFalse(
                Path(call.kwargs["cwd"]).exists(),
                "Owned build temporary directory remains",
            )
        for name in ("version.py", "config_local.py", "users.sqlite"):
            self.assertEqual(before[Path(name)], (self.package / name).read_bytes())
        generated = self.package / pgadmin_assets.ASSET_DIRECTORY
        self.assertFalse((generated / "old.js").exists())
        self.assertTrue((generated / "chunk.lazy.js").is_file())
        self.assertFalse((self.package / "do_not_copy.py").exists())

    def test_source_redirects_reject_unsafe_targets_before_following(self):
        request = Request("https://ftp.postgresql.org/pub/pgadmin/source.tar.gz")
        for target in (
            "http://ftp.postgresql.org/pub/pgadmin/source.tar.gz",
            "https://untrusted.example/source.tar.gz",
            "https://fixture@ftp.postgresql.org/pub/pgadmin/source.tar.gz",
            "https://ftp.postgresql.org:444/pub/pgadmin/source.tar.gz",
            "https://ftp.postgresql.org/pub/pgadmin/source.tar.gz?download=1",
            "https://ftp.postgresql.org/pub/pgadmin/source.tar.gz#fragment",
        ):
            with self.subTest(target=target):
                with patch.object(
                    pgadmin_assets.HTTPRedirectHandler, "redirect_request"
                ) as follow:
                    with self.assertRaises(pgadmin_assets.PgAdminAssetError):
                        pgadmin_assets._SourceRedirectHandler().redirect_request(
                            request, None, 302, "Found", {}, target
                        )
                    follow.assert_not_called()

    def test_canonical_https_source_redirect_produces_native_request(self):
        request = Request("https://ftp.postgresql.org/pub/pgadmin/source.tar.gz")
        target = "https://ftp.postgresql.org/pub/pgadmin/matching-source.tar.gz"
        redirected = pgadmin_assets._SourceRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, target
        )
        self.assertIsInstance(redirected, Request)
        self.assertEqual(target, redirected.full_url)
        self.assertEqual("GET", redirected.get_method())

    def test_downloader_wires_redirect_guard_and_rejects_foreign_final_url_before_read(
        self,
    ):
        response = MagicMock()
        response.__enter__.return_value = response
        response.geturl.return_value = "https://untrusted.example/source.tar.gz"
        opener = Mock()
        opener.open.return_value = response
        with patch.object(pgadmin_assets, "build_opener", return_value=opener) as build:
            with self.assertRaises(pgadmin_assets.PgAdminAssetError):
                pgadmin_assets.download_source("9.19", self.root)
        build.assert_called_once()
        self.assertIsInstance(
            build.call_args.args[0], pgadmin_assets._SourceRedirectHandler
        )
        opener.open.assert_called_once()
        response.read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
