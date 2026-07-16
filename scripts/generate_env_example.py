#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "utils" / "dumb_config.json"
ENV_EXAMPLE_PATH = ROOT / ".env.example"
HEADER_WIDTH = 37


def _title_part(part: str) -> str:
    special = {
        "api": "API",
        "ui": "UI",
        "url": "URL",
        "id": "ID",
        "pgadmin": "pgAdmin",
        "postgres": "PostgreSQL",
        "nzbdav": "NzbDAV",
        "cli": "CLI",
        "debrid": "Debrid",
        "rclone": "Rclone",
        "zurg": "Zurg",
        "zilean": "Zilean",
        "riven": "Riven",
        "dumb": "DUMB",
        "plex": "Plex",
        "jellyfin": "Jellyfin",
        "emby": "Emby",
        "sonarr": "Sonarr",
        "radarr": "Radarr",
        "lidarr": "Lidarr",
        "whisparr": "Whisparr",
        "prowlarr": "Prowlarr",
        "neutarr": "NeutArr",
        "profilarr": "Profilarr",
        "bazarr": "Bazarr",
        "pulsarr": "Pulsarr",
        "tautulli": "Tautulli",
        "seerr": "Seerr",
        "traefik": "Traefik",
        "cloudflared": "Cloudflared",
    }
    return special.get(part.lower(), part.replace("_", " ").title())


def _group_for(path: tuple[str, ...], value: Any) -> tuple[str, ...]:
    if len(path) == 1:
        return ("global",)
    if (
        path[0] == "dumb"
        and len(path) >= 3
        and path[1]
        in {
            "api_service",
            "frontend",
            "metrics",
            "ui",
            "auto_restart",
            "ffprobe_monitor",
        }
    ):
        return path[:2]
    if len(path) >= 3 and path[1] == "instances":
        return (path[0],)
    return (path[0],)


def _group_title(group: tuple[str, ...]) -> str:
    if group == ("global",):
        return "Global Variables"
    return " ".join(_title_part(part) for part in group) + " Variables"


def _env_name(path: Iterable[str]) -> str:
    return "_".join(str(part).upper() for part in path)


def _format_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(",", ":"))
    value = str(value)
    if value == "":
        return ""
    if any(char.isspace() for char in value) or any(
        char in value for char in ["#", '"', "'"]
    ):
        return json.dumps(value)
    return value


def _iter_leaves(node: Any, path: tuple[str, ...] = ()):  # type: ignore[no-untyped-def]
    if isinstance(node, dict) and node:
        for key, value in node.items():
            yield from _iter_leaves(value, path + (str(key),))
    else:
        yield path, node


def generate_env_example(config: dict[str, Any]) -> str:
    groups: OrderedDict[tuple[str, ...], list[tuple[tuple[str, ...], Any]]] = (
        OrderedDict()
    )
    for path, value in _iter_leaves(config):
        groups.setdefault(_group_for(path, value), []).append((path, value))

    lines = [
        "# This file is generated from utils/dumb_config.json.",
        "# Regenerate with: poetry run python scripts/generate_env_example.py",
        "# Values use the same nested path convention loaded from /config/.env.",
        "# Do not copy this file unchanged into production; review and set only the values you need.",
        "",
    ]
    for group, entries in groups.items():
        lines.extend(
            [
                "#" + "-" * HEADER_WIDTH,
                f"# {_group_title(group)}",
                "#" + "-" * HEADER_WIDTH,
                "",
            ]
        )
        for path, value in entries:
            lines.append(f"{_env_name(path)}={_format_value(value)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate .env.example from DUMB config defaults."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if .env.example is not in sync with utils/dumb_config.json",
    )
    args = parser.parse_args()

    with CONFIG_PATH.open("r", encoding="utf-8") as handle:
        config = json.load(handle, object_pairs_hook=OrderedDict)
    generated = generate_env_example(config)

    if args.check:
        current = (
            ENV_EXAMPLE_PATH.read_text(encoding="utf-8")
            if ENV_EXAMPLE_PATH.exists()
            else ""
        )
        if current != generated:
            raise SystemExit(
                ".env.example is out of date; run `poetry run python scripts/generate_env_example.py`"
            )
        print(".env.example ok")
        return

    ENV_EXAMPLE_PATH.write_text(generated, encoding="utf-8")
    print(f"wrote {ENV_EXAMPLE_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
