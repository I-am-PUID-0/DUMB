"""One explicit proposal for Decypharr's torrent and configured Usenet clients."""

from __future__ import annotations

import copy

from utils.arrangement_adoption import preview
from utils.arrangement_connections import _failure, _ledger_path, _values, InputNeeded
from utils.arrangement_external import resolve_target
from utils.arrangement_http import ApiError
from utils.integration_ownership import read_ownership

_PRIVATE = {"fields", "implementation", "_provider_adoption", "additional_clients"}


def _safe(result):
    return {key: value for key, value in result.items() if key not in _PRIVATE}


def _aggregate(results):
    unfinished = next(
        (
            result
            for result in results
            if result.get("execution") not in {"applied", "already_present"}
        ),
        None,
    )
    result = dict(
        unfinished
        or {
            "execution": (
                "applied"
                if any(row["execution"] == "applied" for row in results)
                else "already_present"
            ),
            "verification": (
                "passed"
                if all(row.get("verification") == "passed" for row in results)
                else "unavailable"
            ),
            "retryable": False,
            "reason": "The external Arr's separate Decypharr torrent and Usenet connections were authenticated and read back. Existing policies, roots and mappings were preserved.",
        }
    )
    handoff, clear = {}, []
    for row in results:
        handoff.update(row.get("_private_values") or {})
        clear.extend(row.get("_clear_inputs") or [])
    result["evidence"] = {
        "level": "configuration_readback",
        "retired": bool(results)
        and all((row.get("evidence") or {}).get("retired") is True for row in results),
        "connections": [
            {key: value for key, value in _safe(row).items() if not key.startswith("_")}
            for row in results
        ],
    }
    if any(
        (row.get("evidence") or {}).get("restart_required") == "decypharr"
        for row in results
    ):
        result["evidence"]["restart_required"] = "decypharr"
    if handoff:
        result["_private_values"] = handoff
    if clear:
        result["_clear_inputs"] = list(dict.fromkeys(clear))
    if any(row.get("_clear_on_handoff") is True for row in results):
        result["_clear_on_handoff"] = True
    return result


def apply_dual_provider(
    action,
    config,
    payload,
    operation,
    *,
    single,
    provider_adapter,
    client_factory,
    ownership_directory,
):
    source, provider = action["source"], "decypharr"
    values = _values(payload)
    kwargs = {
        "client_factory": client_factory,
        "ownership_directory": ownership_directory,
    }
    try:
        if provider not in (values.get(source + "::providers") or []):
            results = [
                single(
                    action,
                    config,
                    payload,
                    operation,
                    provider_adapter=provider_adapter,
                    **kwargs,
                )
            ]
            if read_ownership(
                _ledger_path(ownership_directory, source + ".decypharr.usenet.client")
            ):
                results.append(
                    single(
                        action,
                        config,
                        payload,
                        operation,
                        provider_adapter=provider_adapter,
                        protocol_suffix="usenet",
                        **kwargs,
                    )
                )
            return _aggregate(results) if len(results) > 1 else results[0]
        target = resolve_target(payload, source, client_factory=client_factory)
        category = (
            values.get(source + "::decypharr::category")
            or "dumb-" + source.partition(".")[2] + "-decypharr"
        )
        credentials = provider_adapter(
            config,
            payload,
            provider,
            target,
            category,
            "inspect",
            client_factory,
            ownership_directory,
        )
        if credentials.get("execution"):
            return _safe(credentials)
        extra = credentials.get("additional_clients") or []
        if not extra:
            return single(
                action,
                config,
                payload,
                operation,
                provider_adapter=provider_adapter,
                **kwargs,
            )
        if (
            not isinstance(extra, list)
            or len(extra) != 1
            or not isinstance(extra[0], dict)
            or extra[0].get("implementation") != "Sabnzbd"
            or extra[0].get("name_suffix") != "usenet"
            or not isinstance(extra[0].get("fields"), dict)
        ):
            raise InputNeeded(
                "Decypharr returned an unsupported additional client contract."
            )
        protocols = [("", credentials), ("usenet", extra[0])]
        cached_apply = []

        def callback_for(suffix, selected):
            def callback(
                cfg,
                private,
                prov,
                dest,
                chosen_category,
                phase,
                factory,
                directory,
                **consent,
            ):
                native = credentials
                if phase == "apply":
                    if not cached_apply:
                        cached_apply.append(
                            provider_adapter(
                                cfg,
                                private,
                                prov,
                                dest,
                                chosen_category,
                                phase,
                                factory,
                                directory,
                                **consent,
                            )
                        )
                    native = cached_apply[0]
                    if native.get("execution") or native.get("_provider_pending"):
                        return (
                            _safe(native)
                            if native.get("execution")
                            else {**native, **selected}
                        )
                protocol = selected
                if suffix:
                    protocol = next(
                        (
                            row
                            for row in native.get("additional_clients") or []
                            if row.get("name_suffix") == suffix
                        ),
                        selected,
                    )
                else:
                    protocol = native
                result = {
                    **native,
                    "implementation": protocol["implementation"],
                    "fields": protocol["fields"],
                }
                if suffix:
                    result.pop("_provider_adoption", None)
                return result

            return callback

        inspected = []
        for suffix, selected in protocols:
            row = single(
                action,
                config,
                payload,
                "preview" if operation == "preview" else "inspect",
                provider_adapter=callback_for(suffix, selected),
                protocol_suffix=suffix,
                **kwargs,
            )
            if (
                not isinstance(row.get("evidence"), dict)
                or "changes" not in row["evidence"]
            ):
                return _safe(row)
            inspected.append(row)
        proposal = preview(
            _ledger_path(
                ownership_directory,
                source + ".decypharr.batch-preview",
                write=operation == "preview",
            ),
            source + "::decypharr-clients::" + target["ownership_scope"],
            {"members": [row["evidence"].get("preview_id") for row in inspected]},
            {"members": [row["evidence"].get("preview_id") for row in inspected]},
            managed=False,
            operation=operation,
            accepted_id=values.get(source + "::adoption_preview_id"),
            consent=values.get(source + "::adopt_existing", False),
        )
        proposal["changes"] = [
            {
                **change,
                "field": ("usenet" if suffix else "torrent") + "." + change["field"],
            }
            for (suffix, _), row in zip(protocols, inspected)
            for change in row["evidence"]["changes"]
        ]
        proposal["requires_consent"] = any(
            row["evidence"].get("requires_consent") for row in inspected
        )
        different = bool(proposal["changes"])
        if operation in {"preview", "inspect"} or (
            different
            and (
                operation in {"recheck", "verify"}
                or (proposal["requires_consent"] and not proposal["approved"])
            )
        ):
            return {
                "execution": "waiting_input" if different else "already_present",
                "verification": "not_checked",
                "retryable": False,
                "reason": "Review the separate Decypharr torrent and Usenet clients together before changing existing native settings.",
                "evidence": proposal,
            }
        results = []
        for (suffix, selected), prior in zip(protocols, inspected):
            private = copy.deepcopy(payload)
            if proposal["approved"]:
                edits = private.setdefault("arrangement", {}).setdefault("values", {})
                edits[source + "::adopt_existing"] = True
                edits[source + "::adoption_preview_id"] = prior["evidence"][
                    "preview_id"
                ]
            row = single(
                action,
                config,
                private,
                operation,
                provider_adapter=callback_for(suffix, selected),
                protocol_suffix=suffix,
                **kwargs,
            )
            results.append(row)
            if row.get("execution") not in {"applied", "already_present"}:
                break
        return _aggregate(results)
    except (ApiError, ValueError, OSError, KeyError, TypeError) as error:
        return _failure(error)
