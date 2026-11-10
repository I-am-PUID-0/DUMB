"""Refresh selected optional action requirements without removing plan scope.

This is an in-memory operation only. Call it while accepting a revision, and at
initial planning before computing coverage. Original required actions remain
required even when an operator defers them.
"""

from __future__ import annotations


def refresh_requirements(record, payload):
    arrangement = payload.get("arrangement") or {}
    values = arrangement.get("values") or {}
    intent = arrangement.get("intent") or {}
    for action in record.get("actions", []):
        source = action.get("source", "")
        kind = action.get("action")
        family = action.get("service_key") or source.partition(".")[0]
        selected = None
        if kind == "external_registry":
            selected = bool(values.get("setup::external_arrs"))
        elif kind == "indexers":
            selected = bool(values.get(f"{source}::indexers"))
        elif kind == "consumer_paths":
            selected = bool(values.get(f"{source}::consumer_paths"))
        elif kind == "workflow_test":
            selected = bool(values.get(f"{source}::workflow_probe"))
        elif kind == "proxy_routes":
            selected = bool(
                values.get(f"{source}::route_domains")
                or values.get(f"{source}::route_services")
            )
        elif family == "riven_backend" and kind == "media_connection":
            selected = values.get(f"{source}::media_server") not in {None, "", "none"}
        elif kind == "subtitle_providers":
            selected = bool(values.get(f"{source}::providers"))
        elif kind == "library_hook":
            selected = values.get(f"{source}::enable_library_hook") is True
        elif kind == "public_route":
            selected = bool(values.get(f"{source}::public_url"))
        elif family == "maintainerr" and kind == "seerr_connection":
            choice = values.get(f"{source}::seerr_instance")
            selected = isinstance(choice, str) and choice not in {"", "none"}
        elif family == "maintainerr" and kind == "tautulli_connection":
            selected = values.get(f"{source}::connect_tautulli") is True
        elif family == "decypharr" and kind == "nntp":
            choice = values.get(f"{source}::nntp_source", "own")
            selected = (
                intent.get("stack") in {"usenet", "both"}
                or bool(values.get(f"{source}::nntp"))
                or (isinstance(choice, str) and choice not in {"", "own"})
            )
        if selected is not None:
            base_required = action.setdefault(
                "base_required", bool(action.get("required", True))
            )
            action["required"] = base_required or selected
    by_id = {a["id"]: a for a in record.get("actions", [])}
    for action in record.get("actions", []):
        kind = str(action.get("action") or "")
        if kind.startswith("provider."):
            provider = kind.removeprefix("provider.")
            prerequisite = provider + "::nntp"
            prerequisites = [
                key for key in action.get("prerequisites", []) if key != prerequisite
            ]
            if (
                not action.get("retirement_required")
                and by_id.get(prerequisite, {}).get("required") is True
            ):
                prerequisites.append(prerequisite)
            runtime = provider + "::runtime"
            prerequisites = [key for key in prerequisites if key != runtime]
            if not action.get("retirement_required") and runtime in by_id:
                prerequisites.append(runtime)
            action["prerequisites"] = prerequisites
        elif kind == "workflow_test":
            options = values.get(action["source"] + "::workflow_probe") or {}
            provider = options.get("provider")
            dynamic = {
                resource + "::" + name
                for resource in {
                    "infinidysk",
                    "decypharr",
                    "altmount",
                    "plex",
                    "jellyfin",
                    "emby",
                    "audiobookshelf",
                }
                for name in ("runtime", "account", "libraries", "webdav")
            }
            prerequisites = [
                key
                for key in action.get("prerequisites", [])
                if "::provider." not in key and key not in dynamic
            ]
            link = action["source"] + "::provider." + str(provider)
            if link in by_id:
                prerequisites.append(link)
            for resource in {provider, options.get("media_server")}:
                for name in ("runtime", "account", "libraries", "webdav"):
                    dependency = str(resource) + "::" + name
                    if dependency in by_id and dependency not in prerequisites:
                        prerequisites.append(dependency)
            action["prerequisites"] = prerequisites
        elif action.get("service_key") == "altmount" and kind == "webdav":
            dependency = action["source"] + "::account"
            prerequisites = action.setdefault("prerequisites", [])
            if dependency in by_id and dependency not in prerequisites:
                prerequisites.append(dependency)
        elif (
            action.get("service_key") == "traefik_proxy_admin"
            and kind == "proxy_routes"
        ):
            dependency = action["source"] + "::account"
            prerequisites = action.setdefault("prerequisites", [])
            if dependency in by_id and dependency not in prerequisites:
                prerequisites.append(dependency)
        elif (
            action.get("service_key") == "riven_backend" and kind == "media_connection"
        ):
            dynamic = {
                resource + "::" + name
                for resource in {"plex", "jellyfin", "emby"}
                for name in ("runtime", "account")
            }
            prerequisites = [
                key for key in action.get("prerequisites", []) if key not in dynamic
            ]
            target = values.get(action["source"] + "::media_server")
            dependencies = [action["source"] + "::backend_connection"]
            if target in {"plex", "jellyfin", "emby"}:
                dependencies.extend(
                    target + "::" + name for name in ("runtime", "account")
                )
            for dependency in dependencies:
                if dependency in by_id and dependency not in prerequisites:
                    prerequisites.append(dependency)
            action["prerequisites"] = prerequisites
    return record
