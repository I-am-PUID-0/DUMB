"""Immutable CLI source contracts audited against official upstream source.

Audited 2026-10-14: main.py starts cli_battery only on Windows in v0.6.07
bf227aae3fa3774ac7dfbc95d0e137bb0577c5d6 and main snapshot db84f5e99181f5dc46cc0e3b170a6dd238f1f14d.
Dev snapshot c971c60336e3f85aad8f0624591885c7c1da83ce uses in-process metadata
and has no cli_battery/main.py. A moving branch name is never a capability.
"""

AUDITED_COMMITS = {
    "bf227aae3fa3774ac7dfbc95d0e137bb0577c5d6": "standalone",
    "db84f5e99181f5dc46cc0e3b170a6dd238f1f14d": "standalone",
    "c971c60336e3f85aad8f0624591885c7c1da83ce": "embedded",
}
AUDITED_RELEASES = {"v0.6.07": "standalone"}


def cli_source_capability(config):
    sha = str(config.get("commit_sha") or "").strip().lower()
    if sha:
        mode = AUDITED_COMMITS.get(sha)
        evidence = (
            f"audited commit {sha}"
            if mode
            else "the selected commit has no audited Battery contract"
        )
    elif config.get("branch_enabled"):
        mode, evidence = None, "a moving branch does not establish the Battery contract"
    elif config.get("release_version_enabled"):
        tag = str(config.get("release_version") or "")
        mode = AUDITED_RELEASES.get(tag)
        evidence = (
            f"audited release {tag}"
            if mode
            else "the selected release has no audited Battery contract"
        )
    else:
        mode, evidence = (
            None,
            "the moving default target has no immutable Battery contract",
        )
    return {"battery_mode": mode, "evidence": evidence, "phalanx_required": False}


def effective_battery_mode(config):
    explicit = config.get("battery_mode", "auto")
    return (
        cli_source_capability(config)["battery_mode"]
        if explicit == "auto"
        else explicit
    )
