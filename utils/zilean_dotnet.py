"""Compatibility helpers for building the unmaintained Zilean source tree."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ZILEAN_TARGET_FRAMEWORK = "net10.0"
KUBERNETES_CLIENT_MIN_VERSION = "17.0.14"
OPENTELEMETRY_MIN_VERSION = "1.15.3"
_PROJECT_FILE_SUFFIXES = {".csproj", ".props", ".targets"}
_TARGET_FRAMEWORK_ELEMENT = re.compile(
    r"(<TargetFrameworks?>)(.*?)(</TargetFrameworks?>)", re.DOTALL
)


def _retarget_framework_list(value: str) -> tuple[str, bool]:
    changed = False
    frameworks = []
    for framework in value.split(";"):
        stripped = framework.strip()
        retargeted = re.sub(r"^net9\.0(?=$|-)", ZILEAN_TARGET_FRAMEWORK, stripped)
        changed = changed or retargeted != stripped
        if retargeted and retargeted not in frameworks:
            frameworks.append(retargeted)
    return ";".join(frameworks), changed


def retarget_zilean_to_net10(source_dir: str | Path) -> list[Path]:
    """Retarget Zilean's .NET 9 projects and require a .NET 10 target.

    Forks already targeting .NET 10 are left untouched. A fork with neither a
    .NET 9 nor .NET 10 target is rejected because DUMB's final image only ships
    the .NET 10 runtime.
    """

    source_path = Path(source_dir)
    if not source_path.is_dir():
        raise ValueError(f"Zilean source directory does not exist: {source_path}")

    project_files = sorted(
        path
        for path in source_path.rglob("*")
        if path.is_file() and path.suffix.lower() in _PROJECT_FILE_SUFFIXES
    )
    if not project_files:
        raise ValueError(f"No Zilean project files found under {source_path}")

    changed_files = []
    discovered_frameworks = set()
    for project_file in project_files:
        content = project_file.read_text(encoding="utf-8")
        file_changed = False

        def replace_frameworks(match: re.Match[str]) -> str:
            nonlocal file_changed
            replacement, changed = _retarget_framework_list(match.group(2))
            file_changed = file_changed or changed
            discovered_frameworks.update(replacement.split(";"))
            return f"{match.group(1)}{replacement}{match.group(3)}"

        updated = _TARGET_FRAMEWORK_ELEMENT.sub(replace_frameworks, content)
        if file_changed:
            project_file.write_text(updated, encoding="utf-8")
            changed_files.append(project_file)

    if ZILEAN_TARGET_FRAMEWORK not in discovered_frameworks:
        frameworks = ", ".join(sorted(filter(None, discovered_frameworks))) or "none"
        raise ValueError(
            "Zilean source does not target net9.0 or net10.0; "
            f"found target frameworks: {frameworks}"
        )
    if any(framework.startswith("net9.0") for framework in discovered_frameworks):
        raise ValueError(
            "Zilean source still contains a net9.0 target after retargeting"
        )

    return changed_files


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"\d+", version))


def _ensure_package_version(content: str, package: str, minimum: str) -> str:
    pattern = re.compile(
        rf'(<PackageVersion\s+Include="{re.escape(package)}"\s+Version=")([^"]+)("\s*/>)'
    )
    match = pattern.search(content)
    if match:
        if _version_key(match.group(2)) >= _version_key(minimum):
            return content
        return pattern.sub(rf"\g<1>{minimum}\g<3>", content, count=1)

    item_group = re.search(r"<ItemGroup>\s*", content)
    if not item_group:
        raise ValueError("Zilean Directory.Packages.props has no ItemGroup")
    package_entry = f'    <PackageVersion Include="{package}" Version="{minimum}" />\n'
    return content[: item_group.end()] + package_entry + content[item_group.end() :]


def prepare_zilean_for_net10(source_dir: str | Path) -> list[Path]:
    """Apply DUMB's framework and minimum-safe dependency compatibility fixes."""

    source_path = Path(source_dir)
    changed_files = retarget_zilean_to_net10(source_path)
    packages_path = source_path / "Directory.Packages.props"
    if not packages_path.is_file():
        raise ValueError(f"Zilean package properties file is missing: {packages_path}")

    content = packages_path.read_text(encoding="utf-8")
    updated = content
    pinning_pattern = re.compile(
        r"<CentralPackageTransitivePinningEnabled>.*?"
        r"</CentralPackageTransitivePinningEnabled>",
        re.DOTALL,
    )
    if pinning_pattern.search(updated):
        updated = pinning_pattern.sub(
            "<CentralPackageTransitivePinningEnabled>true"
            "</CentralPackageTransitivePinningEnabled>",
            updated,
            count=1,
        )
    else:
        project_tag = re.search(r"<Project>\s*", updated)
        if not project_tag:
            raise ValueError("Zilean Directory.Packages.props has no Project element")
        property_group = (
            "  <PropertyGroup>\n"
            "    <CentralPackageTransitivePinningEnabled>true"
            "</CentralPackageTransitivePinningEnabled>\n"
            "  </PropertyGroup>\n"
        )
        updated = (
            updated[: project_tag.end()] + property_group + updated[project_tag.end() :]
        )

    updated = _ensure_package_version(
        updated, "KubernetesClient", KUBERNETES_CLIENT_MIN_VERSION
    )
    for package in (
        "OpenTelemetry.Api",
        "OpenTelemetry.Exporter.OpenTelemetryProtocol",
    ):
        updated = _ensure_package_version(updated, package, OPENTELEMETRY_MIN_VERSION)

    if updated != content:
        packages_path.write_text(updated, encoding="utf-8")
        if packages_path not in changed_files:
            changed_files.append(packages_path)
    return changed_files


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_dir", help="Extracted Zilean source directory")
    args = parser.parse_args()
    changed_files = prepare_zilean_for_net10(args.source_dir)
    print(
        f"Zilean targets {ZILEAN_TARGET_FRAMEWORK}; "
        f"prepared {len(changed_files)} source file(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
