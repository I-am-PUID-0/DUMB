"""Private, resumable setup runs with separate execution and verification state.

The coordinator accepts injected application callbacks. Importing this module
never loads runtime configuration, opens files or launches an application.
"""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
import stat
import threading
import uuid
from datetime import datetime, timezone, timedelta
from contextlib import contextmanager, nullcontext
import hashlib
import time
from pathlib import Path

from utils.arrangement_plan import (
    build_plan,
    coverage,
    public_value,
    public_schema,
    public_values,
    validate_values,
    refresh_external_resources,
)
from utils.arrangement_requirements import refresh_requirements
from utils.arrangement_link_actions import refresh_link_actions
from utils.arrangement_action_fields import bind_action_fields

IDENTIFIER = re.compile(r"^[a-f0-9]{32}$")
ACTIVE = frozenset({"pending", "running"})
MAX_RECORD_SIZE = 8 * 1024 * 1024
MAX_TREE_DEPTH = 32
MAX_TREE_NODES = 100000
MAX_PENDING_RECORDS = 500
SECRET_INPUT = re.compile(r"password|token|secret|api.?key|username|credential", re.I)


def _bounded_tree(value):
    pending, count = [(value, 0)], 0
    while pending:
        item, depth = pending.pop()
        count += 1
        if count > MAX_TREE_NODES or depth > MAX_TREE_DEPTH:
            raise ValueError("Setup data exceeds its structural limits.")
        if isinstance(item, dict):
            if len(item) > 10000 or any(not isinstance(key, str) for key in item):
                raise ValueError("Setup objects require bounded string keys.")
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 10000:
                raise ValueError("Setup lists exceed the supported size.")
            pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if len(item) > MAX_RECORD_SIZE:
                raise ValueError("Setup text exceeds the supported size.")
        elif item is not None and not isinstance(item, (int, float, bool)):
            raise ValueError("Setup data must be JSON-compatible.")


def _reject_markers(value):
    _bounded_tree(value)
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif item == "[configured]" or item == "[configured URL]":
            raise ValueError(
                "Redacted display values cannot be submitted as configuration."
            )


def _merge_inputs(saved, edits, fields):
    def structured(old, value, field):
        if field.get("type") == "list" and isinstance(value, list):
            identity = field.get("identity_fields") or ["id"]
            merged = []
            for row in value:
                candidates = (
                    [
                        previous
                        for previous in (old or [])
                        if isinstance(previous, dict)
                        and isinstance(row, dict)
                        and all(row.get(key) == previous.get(key) for key in identity)
                    ]
                    if isinstance(old, list)
                    else []
                )
                previous = candidates[0] if len(candidates) == 1 else {}
                merged.append(
                    structured(
                        previous,
                        row,
                        {"type": "object", "fields": field.get("item_fields") or []},
                    )
                )
            return merged
        if field.get("type") != "object" or not isinstance(value, dict):
            return copy.deepcopy(value)
        old = old if isinstance(old, dict) else {}
        same_endpoint = all(
            key not in value or value[key] in (None, "") or value[key] == old.get(key)
            for key in ("url", "host", "port", "tls", "username", "service_key")
        )
        result = copy.deepcopy(value)
        for child in field.get("fields") or []:
            name = child["key"]
            secret = (
                child.get("secret")
                or child.get("type") == "password"
                or SECRET_INPUT.search(name)
            )
            if (
                secret
                and same_endpoint
                and result.get(name) in (None, "")
                and old.get(name) not in (None, "")
            ):
                result[name] = copy.deepcopy(old[name])
            elif child.get("type") in {"list", "object"} and name in result:
                result[name] = structured(old.get(name), result[name], child)
        return result

    merged = copy.deepcopy(saved)
    schema = {field["key"]: field for field in fields}
    for field in fields:
        destination = field.get("credential_destination_key")
        if (
            isinstance(destination, str)
            and destination in edits
            and edits[destination] != saved.get(destination)
        ):
            merged.pop(field["key"], None)
    for key, rows in edits.items():
        if key.endswith("::indexers") and isinstance(rows, list):
            old_rows = {
                row.get("id"): row
                for row in saved.get(key) or []
                if isinstance(row, dict)
            }
            for row in rows:
                previous = (
                    old_rows.get(row.get("id")) if isinstance(row, dict) else None
                )
                if previous and previous.get("definition") != row.get("definition"):
                    prefix = (
                        key.removesuffix("::indexers")
                        + "::indexer::"
                        + str(row["id"])
                        + "::"
                    )
                    for saved_key in list(merged):
                        if saved_key.startswith(prefix):
                            merged.pop(saved_key)
    for key, value in edits.items():
        old = merged.get(key)
        if SECRET_INPUT.search(key) and value in (None, "") and old not in (None, ""):
            continue
        if schema.get(key, {}).get("type") in {"list", "object"}:
            value = structured(old, value, schema[key])
        elif schema.get(key, {}).get("type") == "nntp_providers" and isinstance(
            value, list
        ):
            result = []
            for provider in value:
                updated = copy.deepcopy(provider)

                def identity(row):
                    return (
                        str(row.get("host", "")).casefold(),
                        row.get("port", 563),
                        row.get("tls", True),
                    )

                candidates = [
                    row
                    for row in old or []
                    if isinstance(row, dict) and identity(row) == identity(provider)
                ]
                explicit_user = provider.get("username")
                if explicit_user:
                    candidates = [
                        row
                        for row in candidates
                        if row.get("username") == explicit_user
                    ]
                if len(candidates) > 1 and any(
                    updated.get(secret) in (None, "")
                    for secret in ("username", "password")
                ):
                    raise ValueError(
                        "Identify the existing NNTP account before retaining its credentials."
                    )
                if len(candidates) == 1:
                    for secret in ("username", "password"):
                        if updated.get(secret) in (None, "") and candidates[0].get(
                            secret
                        ):
                            updated[secret] = candidates[0][secret]
                result.append(updated)
            value = result
        elif isinstance(value, dict) and isinstance(old, dict):
            # Never retain a credential when the receiving external endpoint changes.
            same_endpoint = value.get("url", old.get("url")) == old.get("url")
            if same_endpoint:
                value = copy.deepcopy(value)
                for nested, previous in old.items():
                    if SECRET_INPUT.search(nested) and value.get(nested) in (None, ""):
                        value[nested] = previous
        merged[key] = copy.deepcopy(value)
    return merged


def now():
    return datetime.now(timezone.utc).isoformat()


class SetupConflict(ValueError):
    """A setup operation conflicts with another operation or a changed plan."""


class SetupStore:
    """Controller-owned, descriptor-relative state; reject links and foreign files."""

    def __init__(self, directory):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = os.open(
            self.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        info = os.fstat(self.fd)
        if info.st_uid != os.geteuid():
            os.close(self.fd)
            raise PermissionError("Setup state must be owned by the controller.")
        os.fchmod(self.fd, 0o700)
        self.closed = False

    def _name(self, kind, identifier):
        if kind not in {"plan", "inputs", "run"} or not IDENTIFIER.fullmatch(
            str(identifier)
        ):
            raise ValueError("Invalid setup record identifier.")
        return f"{kind}-{identifier}.json"

    def read(self, kind, identifier):
        name = self._name(kind, identifier)
        try:
            descriptor = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.fd
            )
        except FileNotFoundError:
            raise FileNotFoundError(
                "Setup record unavailable. Completed history may have expired; unfinished tasks are retained."
            ) from None
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
                or info.st_size > MAX_RECORD_SIZE
            ):
                raise PermissionError("Unsafe setup state file.")
            with os.fdopen(descriptor, encoding="utf-8") as handle:
                descriptor = -1
                raw = handle.read(MAX_RECORD_SIZE + 1)
                if len(raw.encode("utf-8")) > MAX_RECORD_SIZE:
                    raise ValueError("Setup state exceeds its size limit.")
                try:
                    result = json.loads(
                        raw,
                        parse_constant=lambda _: (_ for _ in ()).throw(
                            ValueError("Invalid JSON number.")
                        ),
                    )
                except (ValueError, RecursionError):
                    raise ValueError(
                        "The setup record could not be decoded safely."
                    ) from None
                _bounded_tree(result)
            if not isinstance(result, dict):
                raise ValueError("Invalid setup record.")
            return result
        finally:
            if descriptor >= 0:
                os.close(descriptor)

    def write(self, kind, identifier, value):
        name = self._name(kind, identifier)
        _bounded_tree(value)
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False)
        try:
            existing = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing and (
            not stat.S_ISREG(existing.st_mode)
            or existing.st_uid != os.geteuid()
            or existing.st_mode & 0o077
            or existing.st_nlink != 1
        ):
            raise PermissionError("Unsafe setup state destination.")
        if len(raw.encode()) > MAX_RECORD_SIZE:
            raise ValueError("Setup state exceeds its size limit.")
        temporary = f".pending-{uuid.uuid4().hex}"
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self.fd,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
            os.fsync(self.fd)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def records(self, kind):
        result = []
        for name in os.listdir(self.fd):
            if name.startswith(f"{kind}-") and name.endswith(".json"):
                identifier = name[len(kind) + 1 : -5]
                if IDENTIFIER.fullmatch(identifier):
                    result.append(identifier)
        return result

    def execution_lock(self):
        descriptor = os.open(
            ".execution-lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
        ):
            os.close(descriptor)
            raise PermissionError("Unsafe setup execution lock.")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            raise SetupConflict("Another setup operation is running.") from None
        return descriptor

    @contextmanager
    def state_lock(self):
        descriptor = os.open(
            ".state-lock",
            os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=self.fd,
        )
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or info.st_nlink != 1
            ):
                raise PermissionError("Unsafe setup state lock.")
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise SetupConflict(
                            "Setup state is busy; retry the request."
                        ) from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(descriptor)

    def remove(self, kind, identifier):
        # Read validates ownership, mode, type, link count and size before removal.
        self.read(kind, identifier)
        os.unlink(self._name(kind, identifier), dir_fd=self.fd)
        os.fsync(self.fd)

    def close(self):
        if not self.closed:
            self.closed = True
            os.close(self.fd)


class SetupCoordinator:
    def __init__(
        self,
        directory,
        *,
        configuration,
        defaults,
        start_services,
        inspect_action,
        apply_action=None,
        preflight=None,
        discover_fields=None,
        autostart=True,
        mutation_guard=None,
        retry_limit=5,
        retry_base_seconds=15,
        retry_max_seconds=120,
        retention_runs=100,
        retention_days=30,
        clock=time.time,
    ):
        self.store = SetupStore(directory)
        self.configuration, self.defaults = configuration, defaults
        self.start_services, self.inspect_action = start_services, inspect_action
        self.apply_action = apply_action or inspect_action
        self.preflight, self.discover_fields = preflight, discover_fields
        self.mutation_guard = mutation_guard or nullcontext
        self.autostart, self.clock = autostart, clock
        self.retry_limit = max(1, min(int(retry_limit), 20))
        self.retry_base = max(1.0, float(retry_base_seconds))
        self.retry_max = max(self.retry_base, float(retry_max_seconds))
        self.retention_runs = max(1, int(retention_runs))
        self.retention_days = max(1, int(retention_days))
        self.lock, self._local = threading.RLock(), threading.local()
        self.stop, self.workers = threading.Event(), {}
        self.storage_issues = []
        self.retry_worker = None
        try:
            descriptor = self.store.execution_lock()
        except SetupConflict:
            descriptor = None
        if descriptor is not None:
            try:
                with self._transaction():
                    for record in self._valid_records("run"):
                        if record.get("status") in ACTIVE:
                            record.update(status="interrupted", updated_at=self._now())
                            for item in record.get("actions", []):
                                if item.get("execution") == "running":
                                    item.update(
                                        execution="waiting_dependency",
                                        verification="stale",
                                        reason="The controller stopped during this action. Recheck existing state before retrying.",
                                    )
                                    item["next_retry_at"] = None
                            self.store.write("run", record["run_id"], record)
                    self._prune()
            finally:
                os.close(descriptor)
        if autostart:
            self.retry_worker = threading.Thread(
                target=self._retry_loop, name="setup-retry", daemon=True
            )
            self.retry_worker.start()

    def _now(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    @contextmanager
    def _transaction(self):
        with self.lock:
            depth = getattr(self._local, "depth", 0)
            if depth:
                self._local.depth += 1
                try:
                    yield
                finally:
                    self._local.depth -= 1
            else:
                with self.store.state_lock():
                    self._local.depth = 1
                    try:
                        yield
                    finally:
                        self._local.depth = 0

    def _valid_records(self, kind):
        for identifier in self.store.records(kind):
            try:
                record = self.store.read(kind, identifier)
                if kind == "run" and (
                    record.get("run_id") != identifier
                    or not IDENTIFIER.fullmatch(str(record.get("plan_id", "")))
                    or not isinstance(record.get("actions"), list)
                    or any(
                        not isinstance(action, dict)
                        or not isinstance(action.get("id"), str)
                        for action in record["actions"]
                    )
                    or not isinstance(record.get("status"), str)
                    or any(
                        not isinstance(record.get(field, ""), str)
                        for field in ("created_at", "updated_at")
                    )
                ):
                    raise ValueError("Setup record identity or structure mismatch.")
                if kind == "plan" and record.get("plan_id") != identifier:
                    raise ValueError("Setup record identity mismatch.")
                yield record
            except (OSError, ValueError):
                if len(self.storage_issues) < 50:
                    issue = {
                        "code": "unreadable_record",
                        "kind": kind,
                        "id": identifier,
                    }
                    if issue not in self.storage_issues:
                        self.storage_issues.append(issue)

    def _prune(self, protect=()):
        runs = sorted(
            self._valid_records("run"),
            key=lambda item: item.get("updated_at", ""),
            reverse=True,
        )
        retained_plans = set()
        terminal = 0
        cutoff = datetime.fromtimestamp(self.clock(), timezone.utc) - timedelta(
            days=self.retention_days
        )
        for record in runs:
            eligible = record.get("status") in {"complete", "abandoned"}
            terminal += int(eligible)
            try:
                timestamp = datetime.fromisoformat(record.get("updated_at", ""))
            except ValueError:
                timestamp = cutoff
            old = timestamp < cutoff if timestamp.tzinfo else False
            if (
                eligible
                and record["run_id"] not in protect
                and (terminal > self.retention_runs or old)
            ):
                self.store.remove("run", record["run_id"])
            else:
                retained_plans.add(record.get("plan_id"))
        plans = sorted(
            self._valid_records("plan"),
            key=lambda item: item.get("created_at", ""),
            reverse=True,
        )
        for index, plan in enumerate(plans):
            if plan["plan_id"] in retained_plans or index < self.retention_runs * 2:
                continue
            try:
                self.store.remove("inputs", plan["plan_id"])
                self.store.remove("plan", plan["plan_id"])
            except FileNotFoundError:
                pass

    def plan(self, payload):
        _reject_markers(payload)
        identifier = uuid.uuid4().hex
        record = build_plan(
            payload,
            self.configuration(),
            self.defaults(),
            plan_id=identifier,
            discover_fields=self.discover_fields,
        )
        record.update(created_at=self._now(), revision=1)
        if self.preflight:
            record["checks"] += self.preflight(record, payload)
        with self._transaction():
            self._prune()
            if len(self.store.records("plan")) >= MAX_PENDING_RECORDS:
                raise SetupConflict(
                    "Finish or explicitly abandon older setup runs before creating another plan."
                )
            self.store.write("inputs", identifier, {"payload": copy.deepcopy(payload)})
            self.store.write("plan", identifier, record)
        return copy.deepcopy(record)

    def get_plan(self, identifier):
        record = self.store.read("plan", identifier)
        record["values"] = public_values(
            record.get("values") or {},
            record.get("arrangement_schema", {}).get("fields", []),
        )
        return record

    def get_run(self, identifier):
        with self.lock:
            record = self.store.read("run", identifier)
            record["values"] = public_values(
                record.get("values") or {},
                record.get("arrangement_schema", {}).get("fields", []),
            )
            record["coverage"] = coverage(record.get("actions", []))
            return record

    def latest(self):
        with self.lock:
            records = list(self._valid_records("run"))
            record = (
                max(records, key=lambda item: item.get("created_at", ""))
                if records
                else None
            )
            if record:
                record["coverage"] = coverage(record.get("actions", []))
                record["values"] = public_values(
                    record.get("values") or {},
                    record.get("arrangement_schema", {}).get("fields", []),
                )
            return record

    def _duplicate(self, plan_id, idempotency_key):
        for record in self._valid_records("run"):
            if record.get("idempotency_key") == idempotency_key:
                if record.get("plan_id") != plan_id:
                    raise SetupConflict(
                        "This request identifier belongs to a different plan."
                    )
                record["coverage"] = coverage(record.get("actions", []))
                record["values"] = public_values(
                    record.get("values") or {},
                    record.get("arrangement_schema", {}).get("fields", []),
                )
                return record
            if record.get("plan_id") == plan_id:
                raise SetupConflict(
                    "This accepted plan already has a setup run. Create a new plan before starting another run."
                )
        return None

    def start(self, plan_id, idempotency_key):
        if not isinstance(idempotency_key, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,128}", idempotency_key
        ):
            raise ValueError("Provide a stable setup request identifier.")
        with self.lock:
            try:
                descriptor = self.store.execution_lock()
            except SetupConflict:
                with self._transaction():
                    duplicate = self._duplicate(plan_id, idempotency_key)
                    if duplicate:
                        return duplicate
                raise
            try:
                with self._transaction():
                    duplicate = self._duplicate(plan_id, idempotency_key)
                    if duplicate:
                        return duplicate
                    plan = self.get_plan(plan_id)
                    if any(
                        check.get("critical") and check.get("ok") is False
                        for check in plan.get("checks", [])
                    ):
                        raise SetupConflict(
                            "Resolve the plan's blocking checks before starting."
                        )
                    self._prune()
                    if len(self.store.records("run")) >= MAX_PENDING_RECORDS:
                        raise SetupConflict(
                            "Finish or explicitly abandon older setup runs before starting another."
                        )
                    identifier = uuid.uuid4().hex
                    record = {
                        **copy.deepcopy(plan),
                        "run_id": identifier,
                        "idempotency_key": idempotency_key,
                        "status": "pending",
                        "results": [],
                        "errors": [],
                        "created_at": self._now(),
                        "updated_at": self._now(),
                        "cancel_requested": False,
                        "revision": 1,
                    }
                    self.store.write("run", identifier, record)
                self._launch(identifier, descriptor, "start")
                descriptor = None
                return self.get_run(identifier)
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _launch(self, identifier, descriptor, operation, action_id=None):
        if self.autostart:
            worker = threading.Thread(
                target=self._execute,
                args=(identifier, descriptor, operation, action_id),
                name=f"setup-{identifier[:8]}",
                daemon=True,
            )
            self.workers[identifier] = worker
            worker.start()
        else:
            self.workers[identifier] = (descriptor, operation, action_id)

    def run_pending(self, identifier):
        descriptor, operation, action_id = self.workers.pop(identifier)
        self._execute(identifier, descriptor, operation, action_id)

    def _refresh_schema(self, identifier, payload):
        if not self.discover_fields:
            return
        record, discovered = self.get_run(identifier), []
        refresh_external_resources(record, payload)
        config = self.configuration()
        for resource in [
            {"id": "setup", "service_key": "setup"},
            *record.get("services", []),
        ]:
            if self.stop.is_set() or self.get_run(identifier).get("cancel_requested"):
                return
            try:
                fields = self.discover_fields(
                    copy.deepcopy(resource),
                    copy.deepcopy(config),
                    copy.deepcopy(payload),
                )
                for field in fields or []:
                    if not isinstance(field, dict) or not str(
                        field.get("key", "")
                    ).startswith(resource["id"] + "::"):
                        continue
                    field = copy.deepcopy(field)
                    field["resource_id"] = resource["id"]
                    if field.get("type") == "password":
                        field.pop("default", None)
                    discovered.extend(public_schema([field]))
            except Exception:
                continue
        with self._transaction():
            current = self.get_run(identifier)
            refresh_external_resources(current, payload)
            previous = current["arrangement_schema"]["fields"]
            by_key = {field["key"]: field for field in previous}
            by_key.update({field["key"]: field for field in discovered})
            if len(by_key) > 10000:
                raise ValueError("The application advertised too many setup fields.")
            current["arrangement_schema"]["fields"] = list(by_key.values())
            for action in current["actions"]:
                action["input_fields"] = [
                    field["key"]
                    for field in by_key.values()
                    if field.get("resource_id") == action.get("source")
                ]
            refresh_link_actions(current, payload, config)
            bind_action_fields(current)
            self.store.write("run", identifier, current)

    def operate(self, identifier, operation, *, action_id=None, values=None):
        if operation not in {
            "retry",
            "recheck",
            "apply",
            "defer",
            "confirm",
            "cancel",
            "abandon",
            "preview",
        }:
            raise ValueError("Unsupported setup operation.")
        if values is not None:
            _reject_markers(values)
        if operation == "preview" and not action_id:
            raise ValueError("Choose the action whose changes should be previewed.")
        with self.lock:
            if operation == "cancel":
                with self._transaction():
                    record = self.get_run(identifier)
                    record.update(cancel_requested=True, updated_at=self._now())
                    self.store.write("run", identifier, record)
                    return record
            descriptor = self.store.execution_lock()
            try:
                with self._transaction():
                    record = self.get_run(identifier)
                    target = (
                        next(
                            (
                                item
                                for item in record["actions"]
                                if item["id"] == action_id
                            ),
                            None,
                        )
                        if action_id
                        else None
                    )
                    if action_id and target is None:
                        raise ValueError("Unknown setup action.")
                    if record["status"] in ACTIVE:
                        raise SetupConflict("This setup run is already active.")
                    if operation == "abandon":
                        record.update(
                            status="abandoned",
                            cancel_requested=True,
                            updated_at=self._now(),
                        )
                        self.store.write("run", identifier, record)
                        return record
                    if operation in {"defer", "confirm"}:
                        if target is None:
                            raise ValueError("Choose the task to update.")
                        if operation == "confirm":
                            if target.get("automation") not in {
                                "manual",
                                "unknown",
                                "unsupported",
                            }:
                                raise ValueError(
                                    "Only manual or unsupported tasks can be confirmed by the operator."
                                )
                            target.update(
                                execution="applied",
                                verification="unavailable",
                                reason="You confirmed this manual task is complete. DUMB has not independently verified it.",
                                evidence={"level": "user_attested"},
                                next_retry_at=None,
                            )
                        else:
                            target.update(
                                execution=(
                                    "waiting_input"
                                    if target.get("required")
                                    else "declined"
                                ),
                                verification="not_checked",
                                reason="Deferred by you. Required work remains incomplete.",
                                next_retry_at=None,
                                retryable=False,
                            )
                        self._finish(record)
                        self.store.write("run", identifier, record)
                        return record
                    private = self.store.read("inputs", record["plan_id"])
                if values:
                    saved_values = (private["payload"].get("arrangement") or {}).get(
                        "values"
                    ) or {}
                    if any(
                        key.rsplit("::", 1)[-1] in {"battery_mode", "use_phalanx_db"}
                        and value != saved_values.get(key)
                        for key, value in values.items()
                    ):
                        raise ValueError(
                            "Create a new plan to change service topology or companion dependencies."
                        )
                    # Refresh after target edits so newly selected targets expose
                    # their profile/root fields in this same accepted revision.
                    candidate = copy.deepcopy(private["payload"])
                    candidate.setdefault("arrangement", {}).setdefault(
                        "values", {}
                    ).update(values)
                    self._refresh_schema(identifier, candidate)
                with self._transaction():
                    record = self.get_run(identifier)
                    target = (
                        next(
                            (
                                item
                                for item in record["actions"]
                                if item["id"] == action_id
                            ),
                            None,
                        )
                        if action_id
                        else None
                    )
                    if values:
                        schema = record["arrangement_schema"]["fields"]
                        edits = validate_values(values, schema)
                        allowed = {
                            field["key"]
                            for field in schema
                            if not target
                            or field.get("resource_id") == target.get("source")
                        }
                        if set(edits) - allowed:
                            raise ValueError(
                                "These inputs belong to another setup action."
                            )
                        saved = (
                            private["payload"]
                            .setdefault("arrangement", {})
                            .setdefault("values", {})
                        )
                        merged = _merge_inputs(saved, edits, schema)
                        if merged != saved:
                            changed_input_keys = {
                                key
                                for key in edits
                                if saved.get(key) != merged.get(key)
                            }
                            saved.clear()
                            saved.update(merged)
                            refresh_external_resources(record, private["payload"])
                            refresh_link_actions(
                                record, private["payload"], self.configuration()
                            )
                            self.store.write("inputs", record["plan_id"], private)
                            record["values"] = public_values(saved, schema)
                            record["revision"] = record.get("revision", 1) + 1
                            refresh_requirements(record, private["payload"])
                            record["fingerprint"] = hashlib.sha256(
                                json.dumps(
                                    {
                                        "revision": record["revision"],
                                        "values": record["values"],
                                        "actions": [
                                            item["id"] for item in record["actions"]
                                        ],
                                    },
                                    sort_keys=True,
                                ).encode()
                            ).hexdigest()
                            probe_edits = {}
                            for key in changed_input_keys:
                                source, _, field = key.partition("::")
                                kind = (
                                    "workflow_test"
                                    if field
                                    in {
                                        "workflow_probe",
                                        "workflow_preview_id",
                                        "workflow_cleanup",
                                    }
                                    else (
                                        "consumer_paths"
                                        if field == "consumer_paths"
                                        else None
                                    )
                                )
                                if kind:
                                    probe_edits.setdefault(source, set()).add(kind)
                            probes_only = sum(
                                1
                                for key in changed_input_keys
                                if key.partition("::")[2]
                                in {
                                    "workflow_probe",
                                    "workflow_preview_id",
                                    "workflow_cleanup",
                                    "consumer_paths",
                                }
                            ) == len(changed_input_keys)
                            affected = (
                                set()
                                if probes_only
                                else (
                                    {target.get("source")}
                                    if target
                                    else {
                                        item.get("source") for item in record["actions"]
                                    }
                                )
                            )
                            if "setup::external_arrs" in changed_input_keys:
                                affected.update(
                                    item.get("source") for item in record["actions"]
                                )
                                affected.update(
                                    item.get("target") for item in record["actions"]
                                )
                            # Reach a bounded fixed point so reference chains do
                            # not depend on JSON key/resource iteration order.
                            for _ in range(len(saved) + 1):
                                previous = len(affected)
                                for key, choice in saved.items():
                                    if (
                                        isinstance(choice, str) and choice in affected
                                    ) or (
                                        isinstance(choice, list)
                                        and any(
                                            isinstance(value, str) and value in affected
                                            for value in choice
                                        )
                                    ):
                                        affected.add(key.split("::", 1)[0])
                                if len(affected) == previous:
                                    break
                            for item in record["actions"]:
                                if probes_only and item.get(
                                    "action"
                                ) not in probe_edits.get(item.get("source"), set()):
                                    continue
                                if (
                                    item.get("action") == "arr_connections"
                                    and f"{item.get('source')}::arr_instances"
                                    not in changed_input_keys
                                ):
                                    continue
                                if item.get("action") == "arr_connection":
                                    prefix = f"{item.get('source')}::"
                                    source_edits = [
                                        key
                                        for key in changed_input_keys
                                        if key.startswith(prefix)
                                    ]
                                    if (
                                        source_edits
                                        and all(
                                            "::" in key[len(prefix) :]
                                            for key in source_edits
                                        )
                                        and not any(
                                            key.startswith(
                                                prefix + str(item.get("target")) + "::"
                                            )
                                            for key in source_edits
                                        )
                                    ):
                                        continue
                                if item.get("action") != "runtime" and (
                                    item.get("source") in affected
                                    or item.get("target") in affected
                                    or probes_only
                                ):
                                    item.update(
                                        verification="stale",
                                        evidence=None,
                                        next_retry_at=None,
                                    )
                                    if item.get("execution") in {
                                        "applied",
                                        "already_present",
                                    }:
                                        item["execution"] = (
                                            "waiting_input"
                                            if item.get("automation") == "manual"
                                            else "planned"
                                        )
                    refresh_link_actions(
                        record, private["payload"], self.configuration()
                    )
                    bind_action_fields(record)
                    if operation in {"retry", "apply"}:
                        for item in record["actions"]:
                            if action_id is None or item["id"] == action_id:
                                item["retry_exhausted"] = False
                                item["next_retry_at"] = None
                                if item.get("execution") not in {
                                    "applied",
                                    "already_present",
                                }:
                                    item["attempts"] = 0
                    record.update(
                        cancel_requested=False, status="pending", updated_at=self._now()
                    )
                    if operation == "preview" and target is not None:
                        target["evidence"] = None
                    self.store.write("run", identifier, record)
                self._launch(identifier, descriptor, operation, action_id)
                descriptor = None
                return self.get_run(identifier)
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _save_action(self, identifier, action_id, changes, *, attempted=False):
        _bounded_tree(changes)
        with self._transaction():
            record = self.get_run(identifier)
            item = next(
                action for action in record["actions"] if action["id"] == action_id
            )
            if changes.get("automation") in {
                "automatic",
                "after_input",
                "manual",
                "unknown",
                "unsupported",
            }:
                item["automation"] = changes["automation"]
            if attempted:
                item["attempts"] = int(item.get("attempts", 0)) + 1
            for key in (
                "execution",
                "verification",
                "reason",
                "instructions",
                "retryable",
                "missing_inputs",
                "evidence",
                "options",
            ):
                if key in changes:
                    item[key] = public_value(changes[key], key)
            if changes.get("execution") != "running" and "retryable" not in changes:
                item["retryable"] = False
            item["checked_at"] = self._now()
            item["next_retry_at"] = None
            if (
                item.get("retryable")
                and item.get("execution") in {"failed", "waiting_dependency"}
                and item.get("action") != "runtime"
            ):
                attempts = item.get("attempts", 0)
                if attempts < self.retry_limit:
                    delay = min(
                        self.retry_max, self.retry_base * 2 ** max(0, attempts - 1)
                    )
                    item["next_retry_at"] = datetime.fromtimestamp(
                        self.clock() + delay, timezone.utc
                    ).isoformat()
                else:
                    item["retry_exhausted"] = True
                    item["reason"] = (
                        (item.get("reason") or "")
                        + " Automatic retries exhausted. Review this task before retrying."
                    )
            record["updated_at"] = self._now()
            self.store.write("run", identifier, record)

    def _consume_private(self, identifier, result, payload):
        minted, clear = (
            result.get("_private_values") or {},
            result.get("_clear_inputs") or [],
        )
        if not minted and not clear:
            return payload
        with self._transaction():
            record = self.get_run(identifier)
            fields = record["arrangement_schema"]["fields"]
            advertised = {field["key"] for field in fields}
            if (
                not isinstance(minted, dict)
                or not isinstance(clear, list)
                or set(minted) - advertised
                or set(clear) - advertised
            ):
                raise ValueError(
                    "An application returned unadvertised private setup fields."
                )
            _reject_markers(minted)
            validate_values(minted, fields)
            private = self.store.read("inputs", record["plan_id"])
            values = (
                private["payload"]
                .setdefault("arrangement", {})
                .setdefault("values", {})
            )
            values.update(copy.deepcopy(minted))
            credential_handoff = result.get("_clear_on_handoff") is True and any(
                field.get("type") == "password"
                and isinstance(minted.get(field["key"]), str)
                and bool(minted[field["key"]].strip())
                for field in fields
            )
            if (
                result.get("execution") in {"applied", "already_present"}
                or credential_handoff
            ):
                for key in clear:
                    values.pop(key, None)
            self.store.write("inputs", record["plan_id"], private)
            record["values"] = public_values(values, fields)
            self.store.write("run", identifier, record)
            return copy.deepcopy(private["payload"])

    def _cancelled(self, identifier):
        return self.stop.is_set() or self.get_run(identifier).get("cancel_requested")

    def _execute(self, identifier, descriptor, operation, action_id):
        try:
            if not self._cancelled(identifier):
                with self.mutation_guard():
                    self._execute_phases(identifier, operation, action_id)
            with self._transaction():
                record = self.get_run(identifier)
                self._finish(record)
                self.store.write("run", identifier, record)
                self._prune(protect={identifier})
        except Exception:
            try:
                with self._transaction():
                    record = self.get_run(identifier)
                    record.update(
                        status="interrupted",
                        updated_at=self._now(),
                        errors=[
                            {
                                "code": "operation_interrupted",
                                "message": "Setup stopped at an operation boundary. Recheck existing state before retrying.",
                            }
                        ],
                    )
                    self.store.write("run", identifier, record)
            except (OSError, ValueError):
                pass
        finally:
            os.close(descriptor)
            with self.lock:
                self.workers.pop(identifier, None)
                if (
                    self.stop.is_set()
                    and not self.workers
                    and (not self.retry_worker or not self.retry_worker.is_alive())
                ):
                    self.store.close()

    def _execute_phases(self, identifier, operation, action_id):
        with self._transaction():
            record = self.get_run(identifier)
            record["status"] = "running"
            self.store.write("run", identifier, record)
            payload = self.store.read("inputs", record["plan_id"])["payload"]
        runtime = [item for item in record["actions"] if item["action"] == "runtime"]
        needs_start = operation == "start" or (
            operation == "retry"
            and not action_id
            and any(
                item["execution"] not in {"applied", "already_present"}
                for item in runtime
            )
        )
        if needs_start and not self._cancelled(identifier):
            for item in runtime:
                if item["execution"] not in {"applied", "already_present"}:
                    self._save_action(identifier, item["id"], {"execution": "running"})
            try:
                startup_payload = copy.deepcopy(payload)
                if operation == "retry":
                    startup_payload["_retry_resources"] = [
                        item["source"]
                        for item in runtime
                        if item.get("execution") not in {"applied", "already_present"}
                    ]
                self.start_services(startup_payload)
            except Exception:
                with self._transaction():
                    failed = self.get_run(identifier)
                    failed["errors"] = [
                        {
                            "code": "startup_incomplete",
                            "message": "Some services did not start. Inspect per-service tasks and sanitized application logs.",
                        }
                    ]
                    self.store.write("run", identifier, failed)
        if not self._cancelled(identifier):
            self._refresh_schema(identifier, payload)
        for saved in self.get_run(identifier)["actions"]:
            if self._cancelled(identifier):
                break
            current = self.get_run(identifier)
            item = next(
                action for action in current["actions"] if action["id"] == saved["id"]
            )
            if (
                action_id
                and item["id"] != action_id
                and not (
                    item.get("action") == "arr_connection"
                    and action_id in item.get("prerequisites", [])
                    and next(
                        (
                            parent
                            for parent in current["actions"]
                            if parent["id"] == action_id
                        ),
                        {},
                    ).get("action")
                    == "arr_connections"
                )
            ):
                continue
            if not item.get("required", True) and item.get("execution") == "declined":
                if not (
                    action_id == item["id"]
                    and operation in {"apply", "preview", "recheck"}
                ):
                    continue
            if item.get("explicit_only") and not (
                action_id == item["id"] and operation in {"apply", "recheck", "preview"}
            ):
                continue
            if (
                operation != "recheck"
                and item.get("automation") in {"manual", "unknown", "unsupported"}
                and item.get("execution") == "applied"
                and isinstance(item.get("evidence"), dict)
                and item["evidence"].get("level") == "user_attested"
            ):
                continue
            if (
                operation in {"retry", "automatic_retry"}
                and item.get("execution") in {"applied", "already_present"}
                and item.get("verification") == "passed"
            ):
                continue
            prerequisites = [
                action
                for action in current["actions"]
                if action["id"] in item.get("prerequisites", [])
            ]
            if any(
                action.get("execution") not in {"applied", "already_present"}
                for action in prerequisites
            ):
                self._save_action(
                    identifier,
                    item["id"],
                    {
                        "execution": "waiting_dependency",
                        "verification": "not_checked",
                        "retryable": not item.get("explicit_only", False),
                        "reason": "Waiting for the required setup prerequisites.",
                    },
                    attempted=True,
                )
                continue
            inspect = operation in {"recheck", "preview"} or item["action"] == "runtime"
            callback = self.inspect_action if inspect else self.apply_action
            callback_operation = (
                "retry" if operation == "automatic_retry" else operation
            )
            self._save_action(
                identifier,
                item["id"],
                {
                    "execution": "running",
                    **(
                        {"evidence": None}
                        if operation in {"recheck", "preview"}
                        else {}
                    ),
                },
                attempted=not inspect,
            )
            try:
                callback_action = copy.deepcopy(item)
                callback_action["_setup_run_id"] = identifier
                callback_action["_setup_revision"] = current.get("revision", 1)
                result = callback(
                    callback_action, copy.deepcopy(payload), callback_operation
                )
                if not isinstance(result, dict):
                    result = {
                        "execution": "waiting_input",
                        "verification": "unavailable",
                        "reason": "Complete this task manually; no supported adapter result is available.",
                    }
                if operation == "preview" and isinstance(result.get("evidence"), dict):
                    result["evidence"]["preview_status"] = (
                        "ready"
                        if result.get("execution")
                        in {"planned", "already_present", "waiting_input"}
                        and (
                            result["evidence"].get("preview_id")
                            or result["evidence"].get("level") == "probe_preview"
                        )
                        else "unavailable"
                    )
                payload = self._consume_private(identifier, result, payload)
            except Exception:
                result = {
                    "execution": "failed",
                    "verification": "failed",
                    "retryable": True,
                    "reason": "The setup action failed. Review sanitized service logs before retrying.",
                }
            self._save_action(identifier, item["id"], result)
        if not self._cancelled(identifier):
            self._refresh_schema(identifier, payload)

    def _finish(self, record):
        required = [item for item in record["actions"] if item.get("required", True)]
        if self.stop.is_set() or record.get("cancel_requested"):
            record["status"] = "interrupted"
        elif all(
            item.get("execution") in {"applied", "already_present"}
            and item.get("verification") != "failed"
            for item in required
        ):
            record["status"] = "complete"
        elif any(item.get("execution") == "failed" for item in required):
            record["status"] = (
                "partial"
                if any(
                    item.get("execution") in {"applied", "already_present"}
                    for item in required
                )
                else "failed"
            )
        else:
            record["status"] = (
                "waiting_input"
                if any(item.get("execution") == "waiting_input" for item in required)
                else "partial"
            )
        record["coverage"] = coverage(record["actions"])
        record["verification_complete"] = all(
            item.get("verification") == "passed" for item in required
        )
        record["next_retry_at"] = min(
            (
                item["next_retry_at"]
                for item in record["actions"]
                if item.get("next_retry_at")
            ),
            default=None,
        )
        record["updated_at"] = self._now()

    def process_due_retries(self):
        if self.stop.is_set():
            return None
        with self.lock:
            try:
                descriptor = self.store.execution_lock()
            except SetupConflict:
                return None
            try:
                with self._transaction():
                    for record in self._valid_records("run"):
                        if (
                            record.get("status") in ACTIVE
                            or record.get("cancel_requested")
                            or record.get("status") == "abandoned"
                        ):
                            continue
                        for item in record.get("actions", []):
                            when = item.get("next_retry_at")
                            if (
                                not when
                                or item.get("action") == "runtime"
                                or not item.get("retryable")
                                or item.get("execution")
                                not in {"failed", "waiting_dependency"}
                            ):
                                continue
                            try:
                                due = (
                                    datetime.fromisoformat(when).timestamp()
                                    <= self.clock()
                                )
                            except (ValueError, TypeError):
                                continue
                            if not due or item.get("attempts", 0) >= self.retry_limit:
                                continue
                            record.update(status="pending", updated_at=self._now())
                            item["next_retry_at"] = None
                            self.store.write("run", record["run_id"], record)
                            self._launch(
                                record["run_id"],
                                descriptor,
                                "automatic_retry",
                                item["id"],
                            )
                            descriptor = None
                            return record["run_id"]
                return None
            finally:
                if descriptor is not None:
                    os.close(descriptor)

    def _retry_loop(self):
        try:
            while not self.stop.wait(0.5):
                try:
                    self.process_due_retries()
                except (OSError, ValueError):
                    continue
        finally:
            with self.lock:
                if self.stop.is_set() and not self.workers:
                    self.store.close()

    def shutdown(self):
        self.stop.set()
        if self.retry_worker and self.retry_worker is not threading.current_thread():
            self.retry_worker.join(timeout=1)
        for identifier, worker in list(self.workers.items()):
            if isinstance(worker, threading.Thread):
                worker.join(timeout=1)
            else:
                os.close(worker[0])
                self.workers.pop(identifier, None)
        # In-flight installers finish their own safe boundary. The store remains
        # open for that worker; its finally block closes it after persisting state.
        if not self.workers and (
            not self.retry_worker or not self.retry_worker.is_alive()
        ):
            self.store.close()
