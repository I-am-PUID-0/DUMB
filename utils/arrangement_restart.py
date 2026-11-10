"""Explicit single-service restart when native Decypharr caches SAB categories."""

from __future__ import annotations

from utils.arrangement_control import _stop_verified, _tracked


def _needs_restart(result):
    if not isinstance(result, dict):
        return False
    evidence = result.get("evidence") or {}
    if isinstance(evidence, dict):
        if evidence.get("restart_required") == "decypharr":
            return True
        return any(
            _needs_restart(row)
            for row in evidence.get("connections") or []
            if isinstance(row, dict)
        )
    return False


def reconcile_with_restart(
    action,
    config,
    payload,
    operation,
    callback,
    *,
    process_handler=None,
    updater=None,
    read_only=False,
):
    result = callback()
    if action.get("action") != "provider.decypharr" or not _needs_restart(result):
        return result
    source = action["source"]
    evidence = {**(result.get("evidence") or {}), "restart_required": "decypharr"}
    if read_only or operation not in {"apply", "retry", "start"}:
        return {**result, "evidence": evidence}
    values = (payload.get("arrangement") or {}).get("values") or {}
    field = source + "::decypharr::restart_after_config"
    if values.get(field) is not True:
        return {
            **result,
            "execution": "waiting_input",
            "verification": "not_checked",
            "retryable": False,
            "evidence": evidence,
            "missing_inputs": [field],
            "reason": "Decypharr saved the selected category, but its SAB API still exposes its startup cache. Authorize a Decypharr-only restart to load that category, then apply this connection again.",
        }
    name = (config.get("decypharr") or {}).get("process_name")
    if (
        not name
        or process_handler is None
        or updater is None
        or _tracked(process_handler, name) is None
    ):
        return {
            **result,
            "execution": "waiting_input",
            "verification": "not_checked",
            "retryable": False,
            "evidence": evidence,
            "reason": "The Decypharr process must be tracked by DUMB before a controlled restart can load its saved SAB categories. Start Decypharr through DUMB and retry.",
        }
    if not _stop_verified(process_handler, name):
        return {
            **result,
            "execution": "failed",
            "verification": "failed",
            "retryable": False,
            "evidence": evidence,
            "reason": "Decypharr did not fully stop. No replacement process was launched; its saved native configuration is preserved for recovery.",
        }
    try:
        process, error = updater.auto_update(name, enable_update=False)
    except Exception:
        process, error = None, True
    if not process or error:
        return {
            **result,
            "execution": "failed",
            "verification": "not_checked",
            "retryable": False,
            "evidence": {**evidence, "restart_attempted": True},
            "reason": "Decypharr could not be restarted after its verified stop. Its new native configuration is preserved; inspect the controller before retrying.",
        }
    try:
        after = callback()
    except Exception:
        return {
            "execution": "waiting_dependency",
            "verification": "not_checked",
            "retryable": True,
            "evidence": {"restart_attempted": True},
            "reason": "Decypharr was restarted; waiting for native API readiness and connection readback.",
        }
    if not isinstance(after, dict):
        after = {
            "execution": "waiting_dependency",
            "verification": "not_checked",
            "retryable": True,
        }
    after["evidence"] = {**(after.get("evidence") or {}), "restart_attempted": True}
    if _needs_restart(after):
        after.update(
            execution="waiting_input",
            verification="not_checked",
            retryable=False,
            reason="Decypharr restarted, but its SAB category readback remains stale. Its saved configuration was preserved; review the native service before another restart.",
        )
    return after
