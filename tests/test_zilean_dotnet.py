import tempfile
import unittest
from pathlib import Path

from utils.zilean_dotnet import prepare_zilean_for_net10, retarget_zilean_to_net10


class ZileanDotnetTests(unittest.TestCase):
    @staticmethod
    def _write_packages(temp_dir, kubernetes_version="15.0.1"):
        packages = Path(temp_dir, "Directory.Packages.props")
        packages.write_text(
            "<Project>\n  <ItemGroup>\n"
            f'    <PackageVersion Include="KubernetesClient" Version="{kubernetes_version}" />\n'
            "  </ItemGroup>\n</Project>\n",
            encoding="utf-8",
        )
        return packages

    def test_retargets_net9_projects_to_net10(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir, "src", "Zilean.ApiService", "Zilean.csproj")
            project.parent.mkdir(parents=True)
            project.write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net9.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )

            changed = retarget_zilean_to_net10(temp_dir)

            self.assertEqual(changed, [project])
            self.assertIn(
                "<TargetFramework>net10.0</TargetFramework>", project.read_text()
            )

    def test_accepts_fork_already_targeting_net10(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir, "Directory.Build.props")
            project.write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net10.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )

            self.assertEqual(retarget_zilean_to_net10(temp_dir), [])

    def test_retargets_and_deduplicates_multitarget_fork(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir, "Zilean.csproj")
            project.write_text(
                "<Project><PropertyGroup>"
                "<TargetFrameworks>net9.0;net10.0</TargetFrameworks>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )

            retarget_zilean_to_net10(temp_dir)

            self.assertIn(
                "<TargetFrameworks>net10.0</TargetFrameworks>", project.read_text()
            )

    def test_rejects_fork_with_incompatible_framework(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Zilean.csproj").write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net8.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError, "does not target net9.0 or net10.0"
            ):
                retarget_zilean_to_net10(temp_dir)

    def test_prepare_applies_safe_dependency_minimums(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Directory.Build.props").write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net9.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            packages = self._write_packages(temp_dir)

            prepare_zilean_for_net10(temp_dir)

            content = packages.read_text()
            self.assertIn('Include="KubernetesClient" Version="17.0.14"', content)
            self.assertIn('Include="OpenTelemetry.Api" Version="1.15.3"', content)
            self.assertIn(
                'Include="OpenTelemetry.Exporter.OpenTelemetryProtocol" Version="1.15.3"',
                content,
            )
            self.assertIn("<CentralPackageTransitivePinningEnabled>true", content)

    def test_prepare_preserves_newer_fork_dependency(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "Directory.Build.props").write_text(
                "<Project><PropertyGroup>"
                "<TargetFramework>net10.0</TargetFramework>"
                "</PropertyGroup></Project>",
                encoding="utf-8",
            )
            packages = self._write_packages(temp_dir, kubernetes_version="18.2.0")

            prepare_zilean_for_net10(temp_dir)

            self.assertIn(
                'Include="KubernetesClient" Version="18.2.0"', packages.read_text()
            )


if __name__ == "__main__":
    unittest.main()
