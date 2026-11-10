"""Stable per-target Arr action scope; no application or filesystem operations."""

from __future__ import annotations

import copy

from utils.arrangement_external import target_family

HELPER_FAMILIES = frozenset({"prowlarr", "neutarr", "profilarr"})
SUPPORTED_FAMILIES = HELPER_FAMILIES | {"seerr", "pulsarr", "bazarr", "maintainerr"}
ARR_FAMILIES = frozenset({"radarr", "sonarr", "lidarr", "whisparr"})


def _resource(config, identifier):
    family, _, instance = identifier.partition(".")
    block = config.get(family) or {}
    return ((block.get("instances") or {}).get(instance) or {}) if instance else block


def selected_targets(source, config, payload):
    values = (payload.get("arrangement") or {}).get("values") or {}
    selected = values.get(
        source + "::arr_instances", _resource(config, source).get("arr_instances")
    )
    return (
        list(dict.fromkeys(selected))
        if isinstance(selected, list)
        and all(isinstance(item, str) for item in selected)
        else None
    )


def refresh_link_actions(record, payload, config):
    """Refresh membership within an explicitly accepted revision, retaining IDs.

    Assignment actions do not count as connection units. Each exact source and
    target has its own retained execution and evidence. Unselected candidates
    stay visible, and removed owned helper links receive scoped retirement work.
    """
    actions = record.setdefault("actions", [])
    by_id = {action["id"]: action for action in actions}
    resources = {resource["id"]: resource for resource in record.get("services", [])}
    fields = (record.get("arrangement_schema") or {}).get("fields") or []
    groups = [
        action
        for action in list(actions)
        if action.get("action") == "arr_connections"
        and action.get("service_key") in SUPPORTED_FAMILIES
    ]
    for group in groups:
        source, family = group["source"], group["service_key"]
        group["coverage_counted"] = True
        group["connection_scope"] = "assignment"
        group["label"] = "Choose Arr connection assignments"
        group["automation"] = "after_input"
        group["declared_automation"] = "after_input"
        group.pop("capability_reason", None)
        selected = selected_targets(source, config, payload)
        allowed = (
            ARR_FAMILIES if family in {"prowlarr", "neutarr"} else {"radarr", "sonarr"}
        )
        candidates = {
            resource_id
            for resource_id, resource in resources.items()
            if resource.get("service_key") in allowed
        }
        for field in fields:
            if field.get("key") == source + "::arr_instances":
                candidates.update(
                    option["value"]
                    for option in field.get("options") or []
                    if isinstance(option.get("value"), str)
                )
        candidates.update(selected or [])
        candidates.update(
            action.get("target")
            for action in actions
            if action.get("action") == "arr_connection"
            and action.get("source") == source
        )
        for target in sorted(value for value in candidates if isinstance(value, str)):
            if target_family(payload, target) not in allowed:
                continue
            identifier = source + "::arr_connection::" + target
            existing = by_id.get(identifier)
            active = selected is not None and target in selected
            was_active = bool(
                existing and existing.get("selected", existing.get("required"))
            )
            if existing is None:
                target_resource = resources.get(target) or {}
                existing = {
                    "id": identifier,
                    "service_key": family,
                    "instance_key": group.get("instance_key"),
                    "source": source,
                    "target": target,
                    "target_instance_key": target.partition(".")[2],
                    "action": "arr_connection",
                    "label": "Connect "
                    + str(resources.get(source, {}).get("name") or source)
                    + " to "
                    + str(target_resource.get("name") or target),
                    "automation": "unknown",
                    "declared_automation": group.get(
                        "declared_automation", "after_input"
                    ),
                    "execution": "planned",
                    "verification": "not_checked",
                    "reason": "Choose and verify this exact application connection.",
                    "instructions": "Only this selected connection is applied or tested. Other connection assignments and policy are preserved.",
                    "docs_url": group.get("docs_url"),
                    "ui_url": group.get("ui_url"),
                    "missing_inputs": [],
                    "input_keys": [],
                    "checked_at": None,
                }
                actions.append(existing)
                by_id[identifier] = existing
            prerequisites = [group["id"]]
            prerequisites.extend(
                runtime_id
                for runtime_id in (
                    source + "::runtime",
                    target + "::runtime",
                    target + "::external_ready",
                )
                if runtime_id in by_id
            )
            existing.update(
                prerequisites=list(dict.fromkeys(prerequisites)),
                input_fields=copy.deepcopy(group.get("input_fields") or []),
                required=active,
                selected=active,
                coverage_counted=True,
                previewable=True,
                connection_scope="edge",
            )
            if active:
                if not was_active or existing.get("execution") == "declined":
                    existing.update(
                        execution="planned",
                        verification="stale",
                        evidence=None,
                        reason="This exact Arr connection is selected and awaits configuration or verification.",
                        attempts=0,
                        next_retry_at=None,
                        retry_exhausted=False,
                    )
                existing.update(retirement_pending=False, retirement_required=False)
                existing["retryable"] = existing.get("retryable", True)
            else:
                retiring = (
                    selected is not None
                    and family in HELPER_FAMILIES
                    and (was_active or existing.get("retirement_required", False))
                )
                if retiring:
                    existing["required"] = True
                    existing["retirement_required"] = True
                    evidence = existing.get("evidence") or {}
                    retired = (
                        isinstance(evidence, dict)
                        and evidence.get("retired") is True
                        and existing.get("verification") == "passed"
                    )
                    existing["retirement_pending"] = not retired
                    if was_active:
                        existing.update(
                            execution="planned",
                            verification="stale",
                            evidence=None,
                            retryable=True,
                            next_retry_at=None,
                            reason="This assignment was removed. Retire only this demonstrably owned helper link; preserve other connections.",
                        )
                    elif retired:
                        existing["retryable"] = False
                else:
                    existing.update(
                        retirement_pending=False,
                        retirement_required=False,
                        retryable=False,
                        execution=(
                            "declined" if selected is not None else "waiting_input"
                        ),
                        verification="not_checked",
                        evidence=None,
                        next_retry_at=None,
                        reason=(
                            "This connection is not selected. Existing native connections are preserved; remove them in the application if desired."
                            if selected is not None
                            else "Choose exact Arr assignments before this connection is applicable."
                        ),
                    )
    if len(actions) > 1200:
        raise ValueError("The arrangement has too many individual connection actions.")
    return record
