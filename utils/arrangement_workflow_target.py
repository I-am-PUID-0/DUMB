"""Read-only native release parsing before a bounded workflow enqueues content.

Official parse responses identify movie, series/episodes or artist/albums.
A parsed title/quality alone is not proof that the intended managed item matched.
"""

from __future__ import annotations

import re


class WorkflowTargetError(ValueError):
    pass


def _identifier(value):
    return type(value) is int and value > 0


def _title(value):
    if not isinstance(value, str) or not value.strip():
        raise WorkflowTargetError(
            "The selected test item needs a native title or an explicit release name."
        )
    return re.sub(r"[\/\\\x00-\x1f\x7f]+", " ", value).strip()


def _year(item, parent):
    for value in (
        item.get("year"),
        str(item.get("releaseDate") or "")[:4],
        parent.get("year"),
    ):
        if str(value).isdigit() and 1800 <= int(value) <= 2200:
            return int(value)
    raise WorkflowTargetError(
        "The selected test item needs a native release year or an explicit release name."
    )


def default_release_name(item, parent, family, marker, *, release_name=None):
    if not isinstance(marker, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", marker):
        raise WorkflowTargetError("The workflow marker is invalid.")
    if not isinstance(item, dict) or not isinstance(parent, dict):
        raise WorkflowTargetError("The selected test item needs a native identity.")
    if release_name is not None and release_name != "":
        if not isinstance(release_name, str) or any(
            char in release_name for char in "\0\r\n/\\"
        ):
            raise WorkflowTargetError(
                "Use a release label without path separators or control characters."
            )
        release = _title(release_name)
    elif family in {"radarr", "whisparr"}:
        release = f"{_title(item.get('title'))} ({_year(item, parent)})"
    elif family == "sonarr":
        season, episode = item.get("seasonNumber"), item.get("episodeNumber")
        if (
            type(season) is not int
            or not 0 <= season <= 999
            or type(episode) is not int
            or not 1 <= episode <= 9999
        ):
            raise WorkflowTargetError(
                "Choose an individually numbered native episode or provide an exact release name."
            )
        release = f"{_title(parent.get('title'))} S{season:02d}E{episode:02d}"
    elif family == "lidarr":
        release = f"{_title(parent.get('artistName'))} - {_title(item.get('title'))} ({_year(item, parent)})"
    else:
        raise WorkflowTargetError(
            "This application has no supported native release parser."
        )
    name = f"{release} [{marker}]"
    if len(name.encode("utf-8")) > 240:
        raise WorkflowTargetError(
            "Use a shorter explicit release name that the native parser maps to this exact item."
        )
    return name


def _get(client, route, **options):
    response = client.request("GET", route, **options)
    if response.status_code in {404, 405}:
        raise WorkflowTargetError(
            "This application version does not expose the required native release identity parser."
        )
    response.raise_for_status()
    document = response.json()
    if not isinstance(document, dict):
        raise WorkflowTargetError(
            "The native release parser returned an unsupported identity."
        )
    return document


def validate_target(client, prefix, family, jobname, itemid):
    if family not in {"radarr", "sonarr", "lidarr", "whisparr"} or prefix not in {
        "/api/v1",
        "/api/v3",
    }:
        raise WorkflowTargetError(
            "This application has no supported native release parser."
        )
    if (
        not _identifier(itemid)
        or not isinstance(jobname, str)
        or not jobname.strip()
        or len(jobname.encode("utf-8")) > 240
        or any(char in jobname for char in "\0\r\n/\\")
    ):
        raise WorkflowTargetError(
            "Choose a bounded release name and exact native item ID."
        )
    parsed = _get(client, prefix + "/parse", params={"title": jobname})
    parentid = None
    if family in {"radarr", "whisparr"}:
        matched = (
            (parsed.get("movie") or {}).get("id")
            if isinstance(parsed.get("movie"), dict)
            else None
        )
        valid = _identifier(matched) and matched == itemid
    else:
        item = _get(
            client,
            prefix + ("/episode/" if family == "sonarr" else "/album/") + str(itemid),
        )
        rows = parsed.get("episodes" if family == "sonarr" else "albums")
        parent_key, item_parent = (
            ("series", "seriesId") if family == "sonarr" else ("artist", "artistId")
        )
        parent = parsed.get(parent_key)
        parentid = parent.get("id") if isinstance(parent, dict) else None
        valid = (
            item.get("id") == itemid
            and _identifier(item.get(item_parent))
            and _identifier(parentid)
            and parentid == item[item_parent]
            and isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and _identifier(rows[0].get("id"))
            and rows[0]["id"] == itemid
            and (item_parent not in rows[0] or rows[0][item_parent] == parentid)
        )
    if not valid:
        raise WorkflowTargetError(
            "The native release parser did not map this job name exclusively to the exact selected test item. Choose an explicit release name and preview again."
        )
    return {
        "level": "native_release_identity",
        "family": family,
        "item_id": itemid,
        "parent_id": parentid,
    }
