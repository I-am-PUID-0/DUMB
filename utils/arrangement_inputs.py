"""Resolve protected input references without persisting duplicate credentials."""

from __future__ import annotations

import copy

PROVIDERS = frozenset({"infinidysk", "decypharr", "altmount", "mediastorm"})


def resolve_provider_inputs(payload, *, strict=False):
    """Return an ephemeral payload with one connection allocation per consumer.

    The stored payload keeps references. A consumer shares authentication, never
    the source application's allocation; account ceilings cover both clients.
    Missing credentials may remain pending during planning, but invalid or cyclic
    references are rejected before any account can be written to an application.
    """
    result = copy.deepcopy(payload)
    values = result.setdefault("arrangement", {}).setdefault("values", {})
    cores = payload.get("core_services") or []
    cores = [cores] if isinstance(cores, dict) else cores
    selected = {
        str(entry.get("name") or "").strip().lower()
        for entry in cores
        if isinstance(entry, dict)
    }
    selected.update(
        str(key).strip().lower() for key in payload.get("optional_services") or []
    )
    if "nzbdav" in selected:
        selected.add("infinidysk")
    selected &= PROVIDERS
    resolved = {}

    def accounts(resource, visiting):
        if resource in visiting:
            raise ValueError("Shared NNTP account references must not form a cycle.")
        if resource in resolved:
            return resolved[resource]
        source = values.get(f"{resource}::nntp_source", "own") or "own"
        if source == "own":
            rows = copy.deepcopy(values.get(f"{resource}::nntp") or [])
        else:
            if source not in selected or source == resource:
                raise ValueError(
                    "Choose a different selected provider as the NNTP account source."
                )
            rows = copy.deepcopy(accounts(source, visiting | {resource}))
            allocation = values.get(f"{resource}::nntp_connections", 5)
            if type(allocation) is not int or not 1 <= allocation <= 200:
                raise ValueError(
                    "Choose 1–200 NNTP connections for each consuming application."
                )
            for row in rows:
                row["connections"] = allocation
            # Private endpoint authorization is an explicit account-level choice.
            if values.get(f"{source}::allow_private_nntp"):
                values[f"{resource}::allow_private_nntp"] = True
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Provide a bounded list of NNTP account objects.")
        if strict and source != "own" and not rows:
            raise ValueError(
                "Enter the source NNTP account before applying its shared connection."
            )
        resolved[resource] = rows
        return rows

    for resource in sorted(selected):
        values[f"{resource}::nntp"] = accounts(resource, set())
    return result
