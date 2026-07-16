import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import setup


class SetupDotnetTests(unittest.TestCase):
    @staticmethod
    def _write_zilean_source(temp_dir):
        Path(temp_dir, "Directory.Build.props").write_text(
            "<Project><PropertyGroup>"
            "<TargetFramework>net9.0</TargetFramework>"
            "</PropertyGroup></Project>",
            encoding="utf-8",
        )
        Path(temp_dir, "Directory.Packages.props").write_text(
            "<Project><ItemGroup>"
            '<PackageVersion Include="KubernetesClient" Version="15.0.1" />'
            "</ItemGroup></Project>",
            encoding="utf-8",
        )
        for project_name in ("Zilean.ApiService", "Zilean.Scraper"):
            project_dir = Path(temp_dir, "src", project_name)
            project_dir.mkdir(parents=True)
            Path(project_dir, f"{project_name}.csproj").write_text(
                "<Project />", encoding="utf-8"
            )

    def test_zilean_requires_local_dotnet_10_sdk_before_restore(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            self._write_zilean_source(temp_dir)
            with (
                patch.object(setup, "_dotnet_sdk_major", return_value=None),
                patch.object(
                    setup,
                    "_ensure_local_dotnet_sdk",
                    return_value=(False, "expected test stop", None),
                ) as ensure_sdk,
            ):
                success, error = setup.setup_dotnet_environment(
                    object(), "zilean", temp_dir
                )

            self.assertFalse(success)
            self.assertEqual(error, "expected test stop")
            self.assertEqual(ensure_sdk.call_args.args[-1], 10)

    def test_clear_directory_preserves_local_dotnet_sdk(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sdk_marker = Path(temp_dir, ".dotnet-sdk", "sdk", "10.0.302")
            sdk_marker.parent.mkdir(parents=True)
            sdk_marker.write_text("cached", encoding="utf-8")
            removable = Path(temp_dir, "old-source.cs")
            removable.write_text("remove", encoding="utf-8")

            success, error = setup.clear_directory(temp_dir)

            self.assertTrue(success, error)
            self.assertTrue(sdk_marker.is_file())
            self.assertFalse(removable.exists())


if __name__ == "__main__":
    unittest.main()
