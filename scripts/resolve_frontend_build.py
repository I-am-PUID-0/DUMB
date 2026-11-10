#!/usr/bin/env python3
"""Resolve one frontend revision before either architecture starts building."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

REPOSITORY = "nicocapalbo/dmbdb"
API_ROOT = f"https://api.github.com/repos/{REPOSITORY}/"
SOURCE_CHOICES = {
    "Latest release": "release",
    "dmbdb dev": "dev",
    "Exact commit": "commit",
}
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
VERSION_PATTERN = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward the workflow token to another origin after a redirect.
        raise ValueError("GitHub frontend resolution unexpectedly redirected")


class GitHubAPI:
    def __init__(self, token=""):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect)

    def __call__(self, endpoint):
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "DUMB-frontend-build-resolver",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        # Fixed HTTPS API origin; validated/quoted repository paths are appended.
        request = urllib.request.Request(  # noqa: S310
            API_ROOT + endpoint, headers=headers
        )
        for attempt in range(3):
            try:
                with self.opener.open(request, timeout=30) as response:
                    body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ValueError("GitHub frontend response exceeds its size limit")
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError("GitHub frontend response must be an object")
                return data
            except urllib.error.HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise ValueError(
                        f"Frontend source resolution failed (GitHub HTTP {error.code})"
                    ) from None
            except urllib.error.URLError:
                if attempt == 2:
                    raise ValueError("Cannot reach GitHub to resolve frontend source")
            time.sleep(attempt + 1)
        raise AssertionError("Unreachable retry state")


def validate_selection(source, commit, backend_ref, event):
    if source not in SOURCE_CHOICES:
        raise ValueError("Select Latest release, dmbdb dev, or Exact commit")
    mode = SOURCE_CHOICES[source]
    commit = commit.strip().lower()
    if mode != "release" and (
        event != "workflow_dispatch" or backend_ref != "refs/heads/dev"
    ):
        raise ValueError("Unreleased frontends require a manual DUMB dev branch build")
    if mode == "commit":
        if not SHA_PATTERN.fullmatch(commit):
            raise ValueError(
                "Frontend commit SHA must contain exactly 40 hex characters"
            )
    elif commit:
        raise ValueError("Frontend commit SHA is only valid with Exact commit")
    return mode, commit


def checked_sha(value):
    if not isinstance(value, str) or not SHA_PATTERN.fullmatch(value):
        raise ValueError("GitHub returned an invalid full frontend commit SHA")
    return value


def peel_tag(api, tag):
    obj = api("git/ref/tags/" + quote(tag, safe="")).get("object", {})
    seen = set()
    for _ in range(8):
        sha = checked_sha(obj.get("sha"))
        if sha in seen:
            raise ValueError("Frontend release tag contains a reference cycle")
        seen.add(sha)
        if obj.get("type") == "commit":
            return sha
        if obj.get("type") != "tag":
            raise ValueError("Frontend release tag does not point to a commit")
        obj = api("git/tags/" + sha).get("object", {})
    raise ValueError("Frontend release tag nesting exceeds its supported limit")


def resolve_frontend_build(source, commit, backend_ref, event, api):
    mode, commit = validate_selection(source, commit, backend_ref, event)
    if mode == "release":
        ref = api("releases/latest").get("tag_name")
        if (
            not isinstance(ref, str)
            or not 1 <= len(ref) <= 128
            or not re.fullmatch(r"[0-9A-Za-z._/+\-]+", ref)
        ):
            raise ValueError("GitHub returned an invalid frontend release tag")
        sha = peel_tag(api, ref)
        kind, image_tag = "release", ""
    elif mode == "dev":
        ref = "dev"
        obj = api("git/ref/heads/dev").get("object", {})
        if obj.get("type") != "commit":
            raise ValueError("The frontend dev branch does not point to a commit")
        sha = checked_sha(obj.get("sha"))
        kind, image_tag = "branch", "dev-frontend-dev"
    else:
        ref = commit
        sha = checked_sha(api("commits/" + commit).get("sha"))
        if sha != commit:
            raise ValueError("GitHub resolved a different frontend commit")
        kind, image_tag = "commit", "dev-frontend-" + sha[:12]

    package_file = api("contents/package.json?ref=" + sha)
    if package_file.get("encoding") != "base64" or package_file.get("type") != "file":
        raise ValueError("Frontend source has no readable package.json")
    content = package_file.get("content")
    if not isinstance(content, str) or len(content) > 256 * 1024:
        raise ValueError("Frontend package.json exceeds its supported size")
    package = json.loads(base64.b64decode("".join(content.split()), validate=True))
    version = package.get("version") if isinstance(package, dict) else None
    if (
        not isinstance(version, str)
        or len(version) > 128
        or not VERSION_PATTERN.fullmatch(version)
    ):
        raise ValueError("Frontend source has an invalid package version")
    return {
        "DUMB_FRONTEND_SOURCE": mode,
        "DUMB_FRONTEND_SOURCE_KIND": kind,
        "DUMB_FRONTEND_REF": ref,
        "DUMB_FRONTEND_SHA": sha,
        "DUMB_FRONTEND_PACKAGE_VERSION": version,
        "DUMB_FRONTEND_IMAGE_TAG": image_tag,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()
    selection = {
        "source": os.environ.get("FRONTEND_SOURCE_INPUT") or "Latest release",
        "commit": os.environ.get("FRONTEND_COMMIT_INPUT") or "",
        "backend_ref": os.environ.get("GITHUB_REF") or "",
        "event": os.environ.get("GITHUB_EVENT_NAME") or "",
    }
    try:
        if args.validate_only:
            validate_selection(**selection)
            print("Frontend source selection is valid")
            return 0
        result = resolve_frontend_build(
            **selection, api=GitHubAPI(os.environ.get("GH_TOKEN") or "")
        )
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a", encoding="utf-8") as handle:
                for name, value in result.items():
                    handle.write(f"{name}={value}\n")
        print(json.dumps(result, sort_keys=True))
        return 0
    except (ValueError, KeyError, TypeError) as error:
        print(f"Frontend build input error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
