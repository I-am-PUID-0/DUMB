"""Credential-free integration outcomes and bounded background reconciliation."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class ActionResult:
    action_id: str
    service_key: str
    action: str
    instance_key: str | None = None
    execution: str = "planned"
    verification: str = "not_checked"
    detail: str = ""
    changed: bool = False
    retryable: bool = False

    def __bool__(self):
        # Preserve the old changed/not-changed helper convention while keeping
        # failure distinct for callers that inspect the typed result.
        return self.changed


_RESULT_COLLECTOR = ContextVar("integration_result_collector", default=None)
_ARR_TARGETS = ContextVar("integration_arr_targets", default=None)


@contextmanager
def integration_targets(identifiers=None):
    """Limit outbound Arr mutations without shrinking provider registrations."""
    allowed = (
        None if identifiers is None else frozenset(str(value) for value in identifiers)
    )
    token = _ARR_TARGETS.set(allowed)
    try:
        yield
    finally:
        _ARR_TARGETS.reset(token)


def arr_target_allowed(host, manager):
    allowed = _ARR_TARGETS.get()
    return allowed is None or _arr_target_key(host, manager) in allowed


@contextmanager
def integration_results(collector=None):
    # Nested helpers inherit the current collection unless explicitly supplied.
    token = _RESULT_COLLECTOR.set(
        collector if collector is not None else _RESULT_COLLECTOR.get()
    )
    try:
        yield
    finally:
        _RESULT_COLLECTOR.reset(token)


@contextmanager
def capture_integration_results(collector=None):
    """Collect whole-attempt outcomes while forwarding the caller's collector."""
    parent = collector if collector is not None else _RESULT_COLLECTOR.get()
    outcomes = []

    def capture(result):
        outcomes.append(result)
        if parent is not None:
            if callable(parent):
                parent(result)
            else:
                parent.append(result)

    with integration_results(capture):
        yield outcomes


def reset_retry_after_success(retry, legacy_result, outcomes):
    """Renew readiness retries only after a complete successful reconciliation."""
    if (
        legacy_result[1] is None
        and outcomes
        and all(
            result.execution in {"applied", "already_present"} for result in outcomes
        )
    ):
        retry.reset()


def emit_action(result: ActionResult, collector=None) -> ActionResult:
    target = collector if collector is not None else _RESULT_COLLECTOR.get()
    if target is not None:
        if callable(target):
            target(result)
        else:
            target.append(result)
    return result


class BoundedIntegrationRetry:
    """One pending retry per integration, with a bounded retry chain.

    Persistent setup state and user-triggered retries belong to the setup
    coordinator. This only bridges transient process/API readiness during boot.
    The pending flag is released *before* invoking the callback so the callback
    can schedule another attempt. Collector context is retained across attempts.
    """

    def __init__(self, *, max_attempts=5, max_delay_s=120):
        self.max_attempts = max(1, int(max_attempts))
        self.max_delay_s = max(1, int(max_delay_s))
        self._lock = threading.Lock()
        self._pending = False
        self._attempts = 0
        self.next_attempt_at = None

    def reset(self):
        with self._lock:
            # Do not create concurrent timers by clearing a pending attempt.
            if not self._pending:
                self._attempts = 0
                self.next_attempt_at = None

    def schedule(self, callback, shutdown_requested, *, delay_s=15):
        with self._lock:
            if self._pending or self._attempts >= self.max_attempts:
                return False
            if shutdown_requested():
                return False
            self._attempts += 1
            delay = min(
                self.max_delay_s,
                max(1, delay_s) * (2 ** (self._attempts - 1)),
            )
            self._pending = True
            self.next_attempt_at = time.time() + delay
        context = copy_context()

        def runner():
            time.sleep(delay)
            with self._lock:
                self._pending = False
                self.next_attempt_at = None
            if not shutdown_requested():
                context.run(callback)

        try:
            threading.Thread(target=runner, daemon=True).start()
        except Exception:
            with self._lock:
                self._pending = False
                self.next_attempt_at = None
            raise
        return True


def _arr_target_key(host, manager):
    from urllib.parse import urlsplit

    target_port = urlsplit(host or "").port
    instance_key = None
    for service in ("radarr", "sonarr", "lidarr", "whisparr"):
        for key, instance in (
            (manager.get(service) or {}).get("instances") or {}
        ).items():
            try:
                matches = (
                    int(instance.get("port") or instance.get("host_port") or 0)
                    == target_port
                )
            except (TypeError, ValueError):
                matches = False
            if matches:
                instance_key = f"{service}.{key}"
                break
        if instance_key:
            break
    return instance_key or "unknown"


def arr_action_result(provider, action, host, manager, **kwargs):
    """Resolve a configured Arr identity without including URLs or credentials."""
    instance_key = _arr_target_key(host, manager)
    return ActionResult(
        action_id=f"{provider}.arr.{instance_key}.{action}",
        service_key=provider,
        instance_key=instance_key,
        action=action,
        **kwargs,
    )


def request_failure(exc):
    """Expose fixed reasons only; upstream exception bodies can contain secrets."""
    import urllib.error

    if getattr(exc, "retryable", None) is False:
        return {
            "execution": "waiting_input",
            "detail": "The integration requires an explicit configuration or ownership decision.",
            "retryable": False,
        }
    if isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403):
        return {
            "execution": "waiting_input",
            "detail": "Application rejected the configured credentials.",
            "retryable": False,
        }
    return {
        "execution": "failed",
        "detail": "Application configuration request failed; retry when the application is ready.",
        "retryable": True,
    }
