"""Keep DUMB's infinidysk adapter on infinidysk 1.x.

infinidysk 2.x is a different program with its own migration. This adapter
installs and updates 1.x only; 2.x targets are refused before anything stops.
It imports no other utils modules, so their test stubs do not affect it.
"""

import re

INFINIDYSK_REPOSITORY = ("infinidysk", "infinidysk")
INFINIDYSK_UNSUPPORTED_MAJOR = 2
INFINIDYSK_V1_BRANCH = "v1"
_RELEASE_MAJOR_RE = re.compile(r"v?(\d+)\.\d+(?:\.\d+)?(?:[-+].*)?", re.IGNORECASE)
_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_MOVING_RELEASE_SELECTORS = {"", "latest", "prerelease"}


def is_infinidysk_repository(repo_owner, repo_name):
    return (
        str(repo_owner or "").strip().lower(),
        str(repo_name or "").strip().lower(),
    ) == INFINIDYSK_REPOSITORY


def infinidysk_release_major(tag):
    """Return the major version of a version-shaped tag, else None."""
    match = _RELEASE_MAJOR_RE.fullmatch(str(tag or "").strip())
    return int(match.group(1)) if match else None


def infinidysk_release_supported(tag):
    major = infinidysk_release_major(tag)
    return major is not None and major < INFINIDYSK_UNSUPPORTED_MAJOR


def _unsupported_error(selection, reason=None):
    detail = f" ({reason})" if reason else ""
    return (
        "infinidysk source check failed: this DUMB integration supports "
        f"infinidysk 1.x only, and the selected {selection} is not 1.x{detail}. "
        "infinidysk 2.x needs a separate migration."
    )


def infinidysk_commit_on_v1(commit, downloader):
    """Prove the commit is contained in the official 1.x maintenance branch."""

    if not _COMMIT_SHA_RE.fullmatch(str(commit or "")):
        return False, "not a full 40-character commit SHA"
    owner, name = INFINIDYSK_REPOSITORY
    url = (
        f"https://api.github.com/repos/{owner}/{name}/compare/"
        f"{commit}...{INFINIDYSK_V1_BRANCH}"
    )
    response = downloader.fetch_with_retries(url, downloader.get_headers())
    if response is None or response.status_code != 200:
        status = response.status_code if response is not None else "no_response"
        return False, f"GitHub ancestry lookup failed with status {status}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    status = str((payload or {}).get("status") or "").strip().lower()
    if status in {"ahead", "identical"}:
        return True, None
    return False, f"not part of the {INFINIDYSK_V1_BRANCH} branch"


def validate_infinidysk_release(release, downloader):
    """Accept a 1.x version tag, or a named ref whose commit is on 1.x."""

    release = str(release or "").strip()
    selection = f"release {release or 'missing'}"
    if infinidysk_release_major(release) is not None:
        if infinidysk_release_supported(release):
            return True, None
        return False, _unsupported_error(selection)
    commit, error = downloader.get_ref_commit_sha(*INFINIDYSK_REPOSITORY, release)
    if not commit:
        return False, _unsupported_error(selection, error or "commit lookup failed")
    on_v1, reason = infinidysk_commit_on_v1(commit, downloader)
    if not on_v1:
        return False, _unsupported_error(selection, reason)
    return True, None


def validate_infinidysk_source(service, downloader):
    """Refuse fixed infinidysk selections that are not 1.x.

    Moving channels (latest, prerelease) are already limited to 1.x when
    they are resolved. Forks are the user's choice and are not checked.
    """

    service = service if isinstance(service, dict) else {}
    if not is_infinidysk_repository(
        service.get("repo_owner"), service.get("repo_name")
    ):
        return True, None
    commit = str(service.get("commit_sha") or "").strip().lower()
    if commit:
        if not _COMMIT_SHA_RE.fullmatch(commit):
            return False, (
                "infinidysk source check failed: commit_sha must be a full "
                "40-character hexadecimal value."
            )
        selection = f"commit {commit[:12]}"
    elif service.get("branch_enabled") is True:
        branch = str(service.get("branch") or "main").strip() or "main"
        selection = f"branch {branch}"
        commit, error = downloader.get_ref_commit_sha(*INFINIDYSK_REPOSITORY, branch)
        if not commit:
            return False, _unsupported_error(selection, error or "commit lookup failed")
    elif service.get("release_version_enabled") is True:
        release = str(service.get("release_version") or "").strip()
        if release.lower() in _MOVING_RELEASE_SELECTORS:
            return True, None
        return validate_infinidysk_release(release, downloader)
    else:
        return True, None
    on_v1, reason = infinidysk_commit_on_v1(commit, downloader)
    if not on_v1:
        return False, _unsupported_error(selection, reason)
    return True, None
