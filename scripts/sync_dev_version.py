#!/usr/bin/env python3
"""Apply a calculated dev version locally or publish only its two metadata files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request

SEMVER = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
SHA = re.compile(r"[0-9a-f]{40}")
TARGETS = ("pyproject.toml", ".github/.release-please-manifest.json")
MAX_BYTES = 1024 * 1024


class VersionSyncError(ValueError):
    """A version plan cannot safely update its original source."""


def version(value):
    if not isinstance(value, str) or not SEMVER.fullmatch(value):
        raise VersionSyncError("Version plans require plain stable X.Y.Z versions.")
    return tuple(map(int, value.split(".")))


def validate_plan(plan):
    if not isinstance(plan, dict):
        raise VersionSyncError("The dev version plan must be an object.")
    for key in ("sourceHead", "stableSha", "cutoffSha"):
        if not isinstance(plan.get(key), str) or not SHA.fullmatch(plan[key]):
            raise VersionSyncError(f"The dev version plan has an invalid {key}.")
    stable, candidate = version(plan.get("stableVersion")), version(
        plan.get("candidateVersion")
    )
    if candidate <= stable or plan.get("coreVersion") != plan["candidateVersion"]:
        raise VersionSyncError(
            "The dev source version must exceed its stable baseline."
        )
    if plan.get("stableTag") not in (
        plan["stableVersion"],
        "v" + plan["stableVersion"],
    ):
        raise VersionSyncError("The stable release tag does not match its version.")
    if type(plan.get("displayCount")) is not int or plan["displayCount"] < 0:
        raise VersionSyncError("The dev version count is invalid.")
    return plan


def replace_project_version(text, candidate):
    version(candidate)
    try:
        current = tomllib.loads(text)["project"]["version"]
    except (KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        raise VersionSyncError("The source project version is unreadable.") from error
    version(current)
    lines = text.splitlines(keepends=True)
    inside = False
    matches = []
    for index, line in enumerate(lines):
        section = line.strip()
        if section.startswith("["):
            inside = section == "[project]"
        if inside and re.match(r"^\s*version\s*=", line):
            matches.append(index)
    if len(matches) != 1:
        raise VersionSyncError("The source requires one explicit [project] version.")
    index = matches[0]
    line = lines[index]
    match = re.fullmatch(
        r"(\s*version\s*=\s*)([\"'])([^\"'\r\n]+)([\"'])([ \t]*(?:#[^\r\n]*)?)(\r?\n)?",
        line,
    )
    if not match or match[2] != match[4] or match[3] != current:
        raise VersionSyncError("The source version assignment is unsupported.")
    lines[index] = (
        match[1] + match[2] + candidate + match[4] + match[5] + (match[6] or "")
    )
    result = "".join(lines)
    if tomllib.loads(result)["project"]["version"] != candidate:
        raise VersionSyncError("The prepared source version did not verify.")
    return result


def prepared_files(root, plan):
    validate_plan(plan)
    root = Path(root)
    result = {}
    for name in TARGETS:
        path = root / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise VersionSyncError("Version metadata must be bounded regular files.")
        result[name] = path.read_bytes().decode("utf-8")
    result["pyproject.toml"] = replace_project_version(
        result["pyproject.toml"], plan["candidateVersion"]
    )
    try:
        manifest = json.loads(result[TARGETS[1]])
    except ValueError as error:
        raise VersionSyncError(
            "The released version manifest is unreadable."
        ) from error
    if not isinstance(manifest, dict) or set(manifest) != {"."}:
        raise VersionSyncError(
            "This workflow supports DUMB's single root package only."
        )
    version(manifest["."])
    manifest["."] = plan["stableVersion"]
    result[TARGETS[1]] = json.dumps(manifest, indent=2) + "\n"
    return result


def checkout_head(root):
    return subprocess.check_output(
        ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
    ).strip()


def require_checkout(root, plan, *, clean=False):
    if checkout_head(root) != plan["sourceHead"]:
        raise VersionSyncError("The source checkout differs from the calculated head.")
    if (
        clean
        and subprocess.check_output(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            text=True,
        ).strip()
    ):
        raise VersionSyncError("Refusing to publish metadata from a modified checkout.")


class GitHubVersionAPI:
    def __init__(self, repository, token):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise VersionSyncError("The GitHub repository is invalid.")
        if not token:
            raise VersionSyncError(
                "DEV_VERSION_TOKEN (workflow secret RELEASE_PLEASE_TOKEN) with repository contents write permission is required."
            )
        self.repository, self.token = repository, token

    def call(self, method, path, payload=None):
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        request = urllib.request.Request(
            "https://api.github.com/repos/" + self.repository + path,
            method=method,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "DUMB-dev-version",
            },
        )
        try:
            with urllib.request.build_opener(NoRedirect()).open(
                request, timeout=30
            ) as response:
                raw = response.read(MAX_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code in (409, 422):
                raise VersionSyncError(
                    "GitHub rejected the non-force dev update."
                ) from error
            raise VersionSyncError(
                f"GitHub metadata request failed (HTTP {error.code}); check token permissions and dev branch rules."
            ) from None
        except urllib.error.URLError:
            raise VersionSyncError("The GitHub metadata API is unavailable.") from None
        if len(raw) > MAX_BYTES:
            raise VersionSyncError("The GitHub metadata response exceeds its bound.")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise VersionSyncError("The GitHub metadata response is invalid.")
        return value


def publish_plan(root, plan, api):
    """Create one metadata commit with API-authenticated identity; never force."""
    validate_plan(plan)
    require_checkout(root, plan, clean=True)
    files = prepared_files(root, plan)
    result = {
        "source_sha": plan["sourceHead"],
        "committed_sha": plan["sourceHead"],
        "version": plan["candidateVersion"],
        "released_version": plan["stableVersion"],
        "changed": False,
        "superseded": False,
    }
    tip = api.call("GET", "/git/ref/heads/dev")["object"]["sha"]
    if tip != plan["sourceHead"]:
        return {**result, "superseded": True}
    changes = [
        {"path": name, "mode": "100644", "type": "blob", "content": content}
        for name, content in files.items()
        if (Path(root) / name).read_bytes().decode("utf-8") != content
    ]
    if not changes:
        return result
    tree = api.call("GET", "/git/commits/" + plan["sourceHead"])["tree"]["sha"]
    next_tree = api.call("POST", "/git/trees", {"base_tree": tree, "tree": changes})[
        "sha"
    ]
    next_commit = api.call(
        "POST",
        "/git/commits",
        {
            "message": (
                f"chore(dev): track next release {plan['candidateVersion']}\n\n"
                f"Released baseline: {plan['stableTag']}.\n"
                "Generated by the dev version workflow."
            ),
            "tree": next_tree,
            "parents": [plan["sourceHead"]],
        },
    )["sha"]
    if not isinstance(next_commit, str) or not SHA.fullmatch(next_commit):
        raise VersionSyncError("GitHub returned an invalid metadata commit.")
    if api.call("GET", "/git/ref/heads/dev")["object"]["sha"] != plan["sourceHead"]:
        return {**result, "superseded": True}
    try:
        api.call("PATCH", "/git/refs/heads/dev", {"sha": next_commit, "force": False})
    except VersionSyncError:
        if api.call("GET", "/git/ref/heads/dev")["object"]["sha"] != plan["sourceHead"]:
            return {**result, "superseded": True}
        raise
    return {**result, "changed": True, "committed_sha": next_commit}


def stamp_image_version(root, candidate):
    """Stamp only the image's API core; its source SHA remains a build input."""
    version(candidate)
    path = Path(root) / "pyproject.toml"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise VersionSyncError("Image project metadata must be a bounded regular file.")
    text = replace_project_version(path.read_bytes().decode("utf-8"), candidate)
    path.write_bytes(text.encode("utf-8"))
    return {"version": candidate, "stamped": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--publish", action="store_true")
    modes.add_argument("--stamp", action="store_true")
    modes.add_argument("--stamp-image", action="store_true")
    parser.add_argument("--version")
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args(argv)
    if args.stamp_image:
        if args.plan or not args.version:
            raise VersionSyncError(
                "Image stamping requires --version and no release plan."
            )
        result = stamp_image_version(args.root, args.version)
        print(json.dumps(result, sort_keys=True))
        return
    if args.plan is None or args.version is not None:
        raise VersionSyncError("Version synchronization requires --plan.")
    if args.plan.stat().st_size > MAX_BYTES:
        raise VersionSyncError("The dev version plan exceeds its bound.")
    plan = validate_plan(json.loads(args.plan.read_text(encoding="utf-8")))
    if args.publish:
        api = GitHubVersionAPI(
            os.environ.get("GITHUB_REPOSITORY", ""),
            os.environ.get("DEV_VERSION_TOKEN", ""),
        )
        result = publish_plan(args.root, plan, api)
    else:
        require_checkout(args.root, plan)
        files = prepared_files(args.root, plan)
        if args.stamp:
            for name, text in files.items():
                (args.root / name).write_bytes(text.encode("utf-8"))
        result = {
            "source_sha": plan["sourceHead"],
            "version": plan["candidateVersion"],
            "released_version": plan["stableVersion"],
            "dev_version": f"{plan['candidateVersion']}-dev.{plan['displayCount']}",
            "stable_tag": plan["stableTag"],
            "stamped": args.stamp,
        }
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            for key, value in result.items():
                output.write(
                    f"{key}={str(value).lower() if isinstance(value, bool) else value}\n"
                )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except VersionSyncError as error:
        print("Dev version synchronization failed: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, subprocess.SubprocessError):
        print(
            "Dev version synchronization failed. Check the stable release, "
            "source head, token permissions and dev branch rules.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
