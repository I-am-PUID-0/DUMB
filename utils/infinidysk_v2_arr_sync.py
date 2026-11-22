"""Synchronize native SAB credentials only for proven DUMB-owned Arr clients."""

import copy
from pathlib import Path
import threading

from utils.core_services import has_core_service
from utils.integration_ownership import read_ownership, write_ownership
from utils.integration_result import (
    ActionResult,
    BoundedIntegrationRetry,
    arr_target_allowed,
    emit_action,
)
from utils.infinidysk_v2_sab import NativeSabKeyError
from utils.infinidysk_v2_arr_clients import (
    ArrClientReconciliationError,
    _ALIASES,
    _FAMILIES,
    _call,
    _client_matches,
    _configuration,
    _connection,
    _effective_native_key,
    _fields,
    _fingerprint,
    _passes,
    _request,
    _resource_fingerprint,
    _scopes,
    reconcile_saved_client,
)

_OWNER = "infinidysk.native.usenet"
_SIDECAR_LOCK = threading.RLock()
_SCHEDULE_LOCK = threading.Lock()
_MANAGED_FIELDS = {
    "host",
    "port",
    "apikey",
    "usessl",
    "urlbase",
    "category",
    "moviecategory",
    "tvcategory",
    "musiccategory",
    "usecategory",
    "usetags",
}


def _ledger(scope):
    return Path(scope["entry"]["config_file"]).parent / ".dumb-integrations.json"


def record_managed_binding(scope, connection, record, key):
    """Promote accepted migration proof without retaining credentials."""
    binding = {
        name: record[name]
        for name in (
            "client_id",
            "config_fingerprint",
            "resource_fingerprint",
            "category",
            "service_key",
            "instance",
            "process_name",
            "api_version",
        )
    }
    binding.update(
        schema=1,
        connection_fingerprint=_fingerprint(connection),
        credential_fingerprint=_fingerprint(key),
    )
    try:
        with _SIDECAR_LOCK:
            records = read_ownership(_ledger(scope))
            records[_OWNER] = binding
            write_ownership(_ledger(scope), records)
    except (OSError, ValueError):
        raise ArrClientReconciliationError(
            "ownership_unverified", scope["process_name"]
        ) from None


def _all_scopes(config):
    consumers = []
    for family in _FAMILIES:
        section = config.get(family) or {}
        entries = section.get("instances")
        entries = entries.items() if isinstance(entries, dict) else [(None, section)]
        for instance, entry in entries:
            if (
                isinstance(entry, dict)
                and entry.get("enabled") is True
                and has_core_service(entry, "infinidysk")
            ):
                consumers.append(
                    {
                        "service_key": family,
                        "instance": instance,
                        "process_name": entry.get("process_name"),
                    }
                )
    return _scopes(config, consumers, [item["process_name"] for item in consumers])


def _generic_owned(records, client):
    """Honor the existing managed helper's exact ID and field projection."""
    owner = records.get("infinidysk.usenet") or {}
    values = _fields(client)
    snapshot = {
        "name": client.get("name"),
        "implementation": client.get("implementation"),
        "fields": {name: values.get(name) for name in _MANAGED_FIELDS},
    }
    return owner.get("id") == client.get("id") and owner.get(
        "fingerprint"
    ) == _fingerprint(snapshot)


def _record(scope, connection, client):
    return {
        name: scope[name]
        for name in (
            "service_key",
            "instance",
            "process_name",
            "config_fingerprint",
            "category",
            "api_version",
        )
    } | {
        "client_id": client["id"],
        "connection_fingerprint": _fingerprint(connection),
        "resource_fingerprint": _resource_fingerprint(client),
    }


def _create_client(config, scope, connection, service, key, check_cancel):
    from utils.nzbdav_settings import _arr_req, ensure_nzbdav_download_client

    def request(url, token, method="GET", payload=None):
        if method in {"POST", "PUT", "DELETE"}:
            check_cancel()
        return _arr_req(url, token, method, payload)

    return ensure_nzbdav_download_client(
        *connection,
        service["backend_port"],
        key,
        scope["service_key"],
        category=scope["category"],
        test_before_save=True,
        api_version=scope["api_version"],
        config_manager=config,
        request_fn=request,
    )


def _sync_arr_clients(
    configuration,
    *,
    request=None,
    native_key=None,
    create_client=None,
    adopt_key=None,
    check_cancel=None,
    result_collector=None,
    _only_scope=None,
):
    """Use configured credentials; unowned broken clients remain review-only."""
    config = _configuration(configuration)
    service = config.get("infinidysk") or {}
    if (
        service.get("enabled") is not True
        or str(service.get("runtime_generation")) != "2"
    ):
        return []
    request, check_cancel = request or _request, check_cancel or (lambda: None)
    scopes = _all_scopes(config)
    from utils.infinidysk_v2_sab import adopt_running_sab_key

    check_cancel()
    snapshot = copy.deepcopy(service)
    try:
        effective = (native_key or _effective_native_key)(
            snapshot, config.get("data_root") or "/data"
        )
    except Exception:
        raise ArrClientReconciliationError("native_key_unavailable") from None
    check_cancel()
    try:
        key = (
            adopt_key(configuration, service, effective)
            if adopt_key
            else adopt_running_sab_key(
                configuration, service, effective, expected_service=snapshot
            )
        )
    except NativeSabKeyError:
        # These messages are static, credential-free configuration guidance.
        # Readiness retries cannot fix an override conflict or apply a new key.
        raise
    except Exception:
        raise ArrClientReconciliationError("native_key_unavailable") from None
    if effective != key:
        raise ArrClientReconciliationError("native_key_unavailable")
    if not scopes:
        return []
    if (
        type(service.get("backend_port")) is not int
        or not 1 <= service["backend_port"] <= 65535
    ):
        raise ArrClientReconciliationError("scope_changed")
    expected_service = copy.deepcopy(_configuration(configuration).get("infinidysk"))
    expected_scopes = [
        (item["process_name"], item["config_fingerprint"]) for item in scopes
    ]

    def check_current():
        check_cancel()
        current = _configuration(configuration)
        if (
            current.get("infinidysk") != expected_service
            or [
                (item["process_name"], item["config_fingerprint"])
                for item in _all_scopes(current)
            ]
            != expected_scopes
        ):
            raise ArrClientReconciliationError("scope_changed")

    results = []
    for scope in scopes:
        if (
            _only_scope is not None
            and (scope["service_key"], scope["instance"], scope["process_name"])
            != _only_scope
        ):
            continue
        check_current()
        try:
            connection = _connection(scope)
        except ArrClientReconciliationError:
            if not Path(scope["entry"].get("config_file") or "").is_file():
                raise ArrClientReconciliationError(
                    "arr_unready", scope["process_name"]
                ) from None
            raise
        if not arr_target_allowed(connection[0], configuration):
            continue
        try:
            with _SIDECAR_LOCK:
                records = read_ownership(_ledger(scope))
        except (OSError, ValueError):
            raise ArrClientReconciliationError(
                "ownership_unverified", scope["process_name"]
            ) from None
        record = records.get(_OWNER)
        if record:
            if (
                not isinstance(record, dict)
                or record.get("schema") != 1
                or any(
                    record.get(name) != scope[name]
                    for name in (
                        "service_key",
                        "instance",
                        "process_name",
                        "config_fingerprint",
                        "category",
                        "api_version",
                    )
                )
                or record.get("connection_fingerprint") != _fingerprint(connection)
            ):
                raise ArrClientReconciliationError(
                    "scope_changed", scope["process_name"]
                )
        else:
            clients = _call(request, connection, scope, "downloadclient")
            if not isinstance(clients, list) or not all(
                isinstance(client, dict) for client in clients
            ):
                raise ArrClientReconciliationError(
                    "client_ambiguous", scope["process_name"]
                )
            matches = [
                client for client in clients if _client_matches(client, scope, service)
            ]
            conflicts = [
                client
                for client in clients
                if (
                    str(client.get("name") or "").lower() in _ALIASES
                    or (
                        str(client.get("implementation") or "").lower() == "sabnzbd"
                        and _fields(client).get("host")
                        in {"localhost", "127.0.0.1", "::1"}
                        and _fields(client).get("port") == service.get("backend_port")
                    )
                )
            ]
            if not matches and not conflicts:
                try:
                    check_current()
                    if create_client:
                        create_client(config, scope, connection, service, key)
                    else:
                        _create_client(
                            config, scope, connection, service, key, check_current
                        )
                except Exception:
                    # A successful create followed by a transient/masked readback
                    # must be retried through the same exact saved-client proof.
                    raise ArrClientReconciliationError(
                        "client_unverified", scope["process_name"]
                    ) from None
                clients = _call(request, connection, scope, "downloadclient")
                if not isinstance(clients, list) or not all(
                    isinstance(client, dict) for client in clients
                ):
                    raise ArrClientReconciliationError(
                        "client_ambiguous", scope["process_name"]
                    )
                matches = [
                    client
                    for client in clients
                    if _client_matches(client, scope, service)
                ]
            if len(matches) != 1 or any(
                client.get("id") != matches[0]["id"] for client in conflicts
            ):
                raise ArrClientReconciliationError(
                    "client_ambiguous", scope["process_name"]
                )
            client = matches[0]
            if not _generic_owned(records, client) and not _passes(
                request, connection, scope, client
            ):
                raise ArrClientReconciliationError(
                    "proof_missing", scope["process_name"]
                )
            current = _call(
                request, connection, scope, f"downloadclient/{client['id']}"
            )
            if _resource_fingerprint(current) != _resource_fingerprint(client):
                raise ArrClientReconciliationError(
                    "scope_changed", scope["process_name"]
                )
            record = _record(scope, connection, current)
        result = reconcile_saved_client(
            request, connection, scope, service, record, key, check_current
        )
        check_current()
        record_managed_binding(scope, connection, record, key)
        results.append(result)
        emit_action(
            ActionResult(
                action_id=f"infinidysk.arr.{scope['service_key']}.{scope['instance']}.download_client_credential",
                service_key="infinidysk",
                instance_key=f"{scope['service_key']}.{scope['instance']}",
                action="download_client_credential",
                execution="applied" if result["changed"] else "already_present",
                verification="passed",
                changed=result["changed"],
                detail="The saved Arr download client passed its connection test using the configured InfiniDysk key.",
            ),
            collector=result_collector,
        )
    return results


def _review_result(scope, error):
    message = (
        "This older InfiniDysk connection has no DUMB ownership record and its saved connection test failed. "
        "In this Arr's download-client settings, copy the InfiniDysk download-client key from DUMB Config "
        "(env: INFINIDYSK__SAB__API_KEY), save, then Test. DUMB can verify and remember that working connection "
        "on the next synchronization. Existing downloader settings were preserved."
        if error.reason_code == "proof_missing"
        else str(error)
    )
    return {
        "process_name": scope["process_name"],
        "changed": False,
        "connection_verified": False,
        "reason_code": error.reason_code,
        "message": message,
        "retryable": error.reason_code
        in {
            "native_key_unavailable",
            "client_unverified",
            "arr_unready",
        },
    }


def sync_arr_clients(configuration, *, continue_on_review=False, **options):
    """Keep independent Arr connections usable when another needs review."""
    if not continue_on_review:
        return _sync_arr_clients(configuration, **options)
    scopes = _all_scopes(_configuration(configuration))
    if not scopes:
        return _sync_arr_clients(configuration, **options)
    results = []
    for scope in scopes:
        try:
            results.extend(
                _sync_arr_clients(
                    configuration,
                    _only_scope=(
                        scope["service_key"],
                        scope["instance"],
                        scope["process_name"],
                    ),
                    **options,
                )
            )
        except ArrClientReconciliationError as error:
            result = _review_result(scope, error)
            results.append(result)
            emit_action(
                ActionResult(
                    action_id=f"infinidysk.arr.{scope['service_key']}.{scope['instance']}.download_client_credential",
                    service_key="infinidysk",
                    instance_key=f"{scope['service_key']}.{scope['instance']}",
                    action="download_client_credential",
                    execution=(
                        "waiting_dependency" if result["retryable"] else "waiting_input"
                    ),
                    detail=result["message"],
                    retryable=result["retryable"],
                ),
                collector=options.get("result_collector"),
            )
    return results


def _admission_recovery_pending():
    from utils.infinidysk_migration_admission import (
        infinidysk_major_recovery_pending,
        infinidysk_v2_recovery_pending,
        infinidysk_namespace_recovery_pending,
        infinidysk_postgres_recovery_pending,
    )

    return bool(
        infinidysk_major_recovery_pending()
        or infinidysk_v2_recovery_pending()
        or infinidysk_namespace_recovery_pending()
        or infinidysk_postgres_recovery_pending()
    )


class ArrSync:
    """One bounded retry chain per controller; never block process startup."""

    def __init__(self, handler, configuration):
        self.handler, self.configuration = handler, configuration
        self.retry = BoundedIntegrationRetry()
        self.lock = threading.Lock()
        self.last_results = []

    def shutdown(self):
        return bool(getattr(self.handler, "shutting_down", False))

    def run(self):
        from utils.infinidysk_migration_admission import (
            InfiniDyskMigrationAdmissionError,
            INFINIDYSK_NATIVE_SETTINGS_LOCK,
            reserve_infinidysk_external_mutation,
            release_infinidysk_external_mutation,
            assert_infinidysk_external_mutation_reserved,
        )

        if self.shutdown() or not self.lock.acquire(blocking=False):
            return False, None
        token = None
        native_locked = False
        try:
            native_locked = INFINIDYSK_NATIVE_SETTINGS_LOCK.acquire(blocking=False)
            if not native_locked:
                message = "Arr credential synchronization is waiting for the native runtime update or settings operation to finish."
                emit_action(
                    ActionResult(
                        action_id="infinidysk.arr.credentials",
                        service_key="infinidysk",
                        action="download_client_credential",
                        execution="waiting_dependency",
                        detail=message,
                        retryable=True,
                    )
                )
                self.retry.schedule(self.run, self.shutdown)
                return False, message
            token = reserve_infinidysk_external_mutation(
                "InfiniDysk managed Arr credential synchronization"
            )
            from utils.infinidysk_v2_update import native_update_state

            config_path = getattr(self.configuration, "file_path", None)
            if (
                not isinstance(config_path, (str, Path))
                or not Path(config_path).is_absolute()
            ):
                raise ArrClientReconciliationError("native_recovery_pending")
            state = native_update_state(
                _configuration(self.configuration).get("infinidysk") or {},
                Path(config_path).parent / "updates" / "infinidysk-v2",
            )
            if state and state.get("recovery_required"):
                raise ArrClientReconciliationError("native_recovery_pending")

            def check_owner():
                if self.shutdown():
                    raise ArrClientReconciliationError("scope_changed")
                assert_infinidysk_external_mutation_reserved(token)

            results = sync_arr_clients(
                self.configuration, check_cancel=check_owner, continue_on_review=True
            )
            self.last_results = copy.deepcopy(results)
            failures = [
                result for result in results if not result["connection_verified"]
            ]
            if failures:
                for result in failures:
                    self.handler.logger.warning(
                        "InfiniDysk connection for %s: %s",
                        result["process_name"],
                        result["message"],
                    )
                if any(result["retryable"] for result in failures):
                    self.retry.schedule(self.run, self.shutdown)
                return any(result["changed"] for result in results), " ".join(
                    f"{result['process_name']}: {result['message']}"
                    for result in failures
                )
            self.retry.reset()
            return any(result["changed"] for result in results), None
        except InfiniDyskMigrationAdmissionError:
            recovery = _admission_recovery_pending()
            message = (
                "Arr credential synchronization is paused while migration recovery needs review."
                if recovery
                else "Arr credential synchronization is deferred while a guarded operation is active; DUMB will retry when it finishes."
            )
            emit_action(
                ActionResult(
                    action_id="infinidysk.arr.credentials",
                    service_key="infinidysk",
                    action="download_client_credential",
                    execution="waiting_input" if recovery else "waiting_dependency",
                    detail=message,
                    retryable=not recovery,
                )
            )
            if not recovery:
                self.retry.schedule(self.run, self.shutdown)
            return False, message
        except NativeSabKeyError as error:
            message = str(error)
            emit_action(
                ActionResult(
                    action_id="infinidysk.arr.credentials",
                    service_key="infinidysk",
                    action="download_client_credential",
                    execution="waiting_input",
                    detail=message,
                    retryable=False,
                )
            )
            self.handler.logger.warning(
                "InfiniDysk download-client configuration: %s", message
            )
            return False, message
        except ArrClientReconciliationError as error:
            retryable = error.reason_code in {
                "native_key_unavailable",
                "client_unverified",
                "arr_unready",
            }
            message = str(error)
            emit_action(
                ActionResult(
                    action_id="infinidysk.arr.credentials",
                    service_key="infinidysk",
                    action="download_client_credential",
                    execution="waiting_dependency" if retryable else "waiting_input",
                    detail=message,
                    retryable=retryable,
                )
            )
            self.handler.logger.warning(
                "InfiniDysk Arr credential synchronization: %s", message
            )
            if retryable:
                self.retry.schedule(self.run, self.shutdown)
            return False, message
        finally:
            release_infinidysk_external_mutation(token)
            if native_locked:
                INFINIDYSK_NATIVE_SETTINGS_LOCK.release()
            self.lock.release()


def schedule_arr_sync(handler, service_key, service):
    if service_key != "infinidysk" and not (
        service_key in _FAMILIES
        and isinstance(service, dict)
        and service.get("enabled") is True
        and has_core_service(service, "infinidysk")
    ):
        return False
    from utils.config_loader import CONFIG_MANAGER

    native = CONFIG_MANAGER.get("infinidysk") or {}
    if (
        native.get("enabled") is not True
        or str(native.get("runtime_generation")) != "2"
    ):
        return False
    coordinator = _coordinator(handler, CONFIG_MANAGER)
    # A genuine new child start gets a fresh bounded chain. reset() preserves
    # any pending timer, so simultaneous native/Arr starts cannot multiply it.
    coordinator.retry.reset()
    return coordinator.retry.schedule(coordinator.run, coordinator.shutdown, delay_s=1)


def _coordinator(handler, configuration):
    with _SCHEDULE_LOCK:
        coordinator = getattr(handler, "_native_arr_sync", None)
        if coordinator is None:
            coordinator = ArrSync(handler, configuration)
            handler._native_arr_sync = coordinator
        return coordinator


def synchronize_arr_config(handler, configuration):
    """Explicit setup/configuration hooks share the startup single-flight guard."""
    return _coordinator(handler, configuration).run()
