"""Publish a verified native runtime without rolling an upgraded database back.

Release policy and lifecycle admission belong to the updater. The caller owns
the staged artifact context and callbacks must raise when their operation fails.
Once launch is authorized, even an unsuccessful start can have migrated SQLite
or PostgreSQL. That boundary is durable and never triggers binary-only rollback.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import time

from utils.infinidysk_v2_runtime import MARKER, MAX_BINARY, runtime_dir
from utils.infinidysk_v2_releases import release_tag
from utils.infinidysk_v2_space_control import identity, rename_exclusive
from utils.private_files import atomic_write_private_text, read_private_text

_FORMAT = "native-runtime-update-v1"
_ARTIFACT = "official-v2-release-v1"
_UPDATE_OWNER = ContextVar("infinidysk_native_update_owner", default=None)
_TERMINAL = {"completed", "restored"}
_STATES = _TERMINAL | {"active", "recovery_required"}
_METADATA = {
    "format",
    "release",
    "commit",
    "architecture",
    "archive_sha256",
    "binary_sha256",
}
_RECOVERY = (
    "The native runtime update requires recovery. Keep the current data and both "
    "runtime directories. Do not start the previous binary against a database "
    "the replacement may have upgraded. Review the retained update journal."
)


class NativeUpdateError(ValueError):
    """A prelaunch update failed; the previous runtime was verified restored."""

    def __init__(self, message, *, receipt=None, stage="preflight"):
        super().__init__(message)
        self.receipt = copy.deepcopy(receipt or {})
        self.stage = stage
        self.launched = self.receipt.get("launched") is True
        self.recovery_required = False


class NativeUpdateRecoveryRequired(NativeUpdateError):
    """Runtime/database compatibility is uncertain; preserve all update evidence."""

    def __init__(self, message=_RECOVERY, **kwargs):
        super().__init__(message, **kwargs)
        self.recovery_required = True


def _direct(value):
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or path == Path("/"):
        raise NativeUpdateError("Native update paths must be dedicated absolute paths.")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise NativeUpdateError("Native update paths cannot follow symbolic links.")
    return path


def _directory(path):
    value = identity(_direct(path))
    if value is None or value["mode"] != stat.S_IFDIR:
        raise NativeUpdateError("A native update directory is unavailable.")
    return value


def _same(path, expected):
    if identity(_direct(path)) != expected:
        raise NativeUpdateError("A native update directory changed; it was retained.")


def _metadata(value):
    if (
        not isinstance(value, dict)
        or set(value) != _METADATA
        or value.get("format") != _ARTIFACT
        or value.get("architecture") not in {"x64", "arm64"}
        or not isinstance(value.get("release"), str)
        or release_tag(value["release"]) != value["release"]
        or any(
            not isinstance(value.get(key), str) or not re.fullmatch(pattern, value[key])
            for key, pattern in (
                ("commit", r"[0-9a-f]{40}"),
                ("archive_sha256", r"[0-9a-f]{64}"),
                ("binary_sha256", r"[0-9a-f]{64}"),
            )
        )
    ):
        raise NativeUpdateError("The verified native release metadata is invalid.")
    return copy.deepcopy(value)


def _service_fingerprint(service):
    scope = {
        key: value for key, value in service.items() if key != "v2_release_version"
    }
    return hashlib.sha256(
        json.dumps(
            scope, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def _selector(value):
    return "latest" if not value or value == "latest" else release_tag(value)


def _read(path, maximum):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum
        ):
            raise NativeUpdateError("A staged native runtime file is unsafe.")
        data = handle.read(maximum + 1)
        after = os.fstat(handle.fileno())
        named = path.lstat()
        stamp = lambda value: (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_nlink,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )
        if (
            len(data) > maximum
            or stamp(before) != stamp(after)
            or stamp(before) != stamp(named)
        ):
            raise NativeUpdateError(
                "A staged native runtime file changed during verification."
            )
        return data


def _artifact(path, metadata, expected):
    _same(path, expected)
    if {item.name for item in path.iterdir()} != {
        "infinidysk",
        "LICENSE",
        "version.txt",
        "commit.txt",
        MARKER,
    }:
        raise NativeUpdateError("The staged native runtime contains unexpected files.")
    _read(path / "LICENSE", 2 * 1024 * 1024)
    if (
        json.loads(_read(path / MARKER, 8192)) != metadata
        or _read(path / "version.txt", 256).decode().strip().removeprefix("v")
        != metadata["release"].removeprefix("v")
        or _read(path / "commit.txt", 256).decode().strip() != metadata["commit"]
        or hashlib.sha256(_read(path / "infinidysk", MAX_BINARY)).hexdigest()
        != metadata["binary_sha256"]
        or not (path.joinpath("infinidysk").stat().st_mode & 0o111)
    ):
        raise NativeUpdateError(
            "The staged native runtime no longer matches its verified release."
        )
    _same(path, expected)


def _bindings(path):
    return {
        name: hashlib.sha256(_read(path / name, maximum)).hexdigest()
        for name, maximum in (
            ("infinidysk", MAX_BINARY),
            (MARKER, 8192),
            ("version.txt", 256),
            ("commit.txt", 256),
            ("LICENSE", 2 * 1024 * 1024),
        )
    }


def _journal_path(service, journal_root):
    root = _direct(journal_root)
    runtime = _direct(runtime_dir(service))
    if root == runtime or runtime in root.parents:
        raise NativeUpdateError(
            "Native update journals must be outside the replaced runtime."
        )
    key = hashlib.sha256(os.fsencode(runtime)).hexdigest()[:24]
    return root / f"native-runtime-{key}.json"


def _load(path, runtime):
    record = json.loads(read_private_text(path, max_bytes=32768))
    if (
        not isinstance(record, dict)
        or record.get("format") != _FORMAT
        or record.get("runtime") != str(runtime)
        or record.get("state") not in _STATES
        or type(record.get("launched")) is not bool
        or type(record.get("was_running")) is not bool
        or (record.get("state") == "restored" and record["launched"])
        or (record.get("state") == "completed" and not record["launched"])
        or not isinstance(record.get("id"), str)
        or not re.fullmatch(r"[0-9a-f]{32}", record["id"])
        or record.get("previous_runtime")
        != str(runtime.parent / f".{runtime.name}.dumb-previous-{record['id']}")
        or not isinstance(record.get("stage"), str)
        or not re.fullmatch(r"[a-z_]{1,64}", record["stage"])
        or not isinstance(record.get("service_fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", record["service_fingerprint"])
    ):
        raise NativeUpdateRecoveryRequired()
    for key in ("previous_identity", "candidate_identity"):
        value = record.get(key)
        if (
            not isinstance(value, dict)
            or set(value) != {"device", "inode", "mode"}
            or any(type(number) is not int or number < 0 for number in value.values())
            or value["mode"] != stat.S_IFDIR
        ):
            raise NativeUpdateRecoveryRequired()
    bindings = record.get("previous_bindings")
    if (
        not isinstance(bindings, dict)
        or set(bindings)
        != {"infinidysk", MARKER, "version.txt", "commit.txt", "LICENSE"}
        or any(
            not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in bindings.values()
        )
    ):
        raise NativeUpdateRecoveryRequired()
    _metadata(record.get("release_metadata"))
    for key in ("original_selector", "target_selector"):
        if (
            not isinstance(record.get(key), str)
            or _selector(record[key]) != record[key]
        ):
            raise NativeUpdateRecoveryRequired()
    if record["target_selector"] not in {
        "latest",
        record["release_metadata"]["release"],
    }:
        raise NativeUpdateRecoveryRequired()
    return record


def native_update_state(service, journal_root):
    """Bounded status only; no observation may clear an interrupted update."""
    try:
        path = _journal_path(service, journal_root)
        record = _load(path, runtime_dir(service))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError):
        return {
            "state": "recovery_required",
            "recovery_required": True,
            "message": _RECOVERY,
        }
    blocked = record["state"] not in _TERMINAL
    return {
        "id": record["id"],
        "state": record["state"],
        "stage": record["stage"],
        "release": record["release_metadata"]["release"],
        "launched": record["launched"],
        "was_running": record["was_running"],
        "target_selector": record["target_selector"],
        "recovery_required": blocked,
        "previous_runtime_retained": record["state"] != "restored",
        "message": (
            _RECOVERY if blocked else "Native runtime update evidence is retained."
        ),
    }


def validate_native_update_launch(service, journal_root):
    """Keep ordinary starts inside the last accepted runtime/database boundary.

    The private owner context belongs only to explicit update lifecycle callbacks.
    A controller restart, scheduler or unrelated thread cannot inherit it. No
    journal preserves the existing initial-install and pinned-import behavior.
    """
    record = None
    try:
        runtime = _direct(runtime_dir(service))
        record = _load(_journal_path(service, journal_root), runtime)
        state = record["state"]
        owner = _UPDATE_OWNER.get()
        if state == "restored":
            _same(runtime, record["previous_identity"])
            if _bindings(runtime) != record["previous_bindings"]:
                raise NativeUpdateRecoveryRequired()
        elif state == "completed":
            # The persistent journal may outlive a container's /opt inode.
            # Identical accepted bytes/metadata remain safe after a runtime
            # restore; a different release cannot inherit this authority.
            _artifact(runtime, record["release_metadata"], _directory(runtime))
        elif (
            owner is not None
            and owner["active"] is True
            and owner["runtime"] == str(runtime)
            and owner["id"] == record["id"]
            and _service_fingerprint(service) == record["service_fingerprint"]
        ):
            _artifact(runtime, record["release_metadata"], record["candidate_identity"])
        else:
            raise NativeUpdateRecoveryRequired()
        return True
    except FileNotFoundError:
        if record is None:
            return True
        raise NativeUpdateRecoveryRequired(
            receipt=record, stage=record["stage"]
        ) from None
    except (OSError, ValueError, TypeError):
        raise NativeUpdateRecoveryRequired(
            receipt=record,
            stage=record["stage"] if record else "launch_guard",
        ) from None


def native_runtime_restore_metadata(service, journal_root):
    """Read the exact accepted release for a missing container runtime.

    This grants no release change, database move or recovery bypass. Completed
    receipts can outlive /opt; unfinished attempts and changed service scope
    cannot authorize an initial-version fallback against upgraded data.
    """
    record = None
    try:
        runtime = _direct(runtime_dir(service))
        record = _load(_journal_path(service, journal_root), runtime)
        if (
            record["state"] != "completed"
            or _service_fingerprint(service) != record["service_fingerprint"]
            or _selector(service.get("v2_release_version"))
            not in {record["original_selector"], record["target_selector"]}
        ):
            raise NativeUpdateRecoveryRequired()
        return copy.deepcopy(record["release_metadata"])
    except FileNotFoundError:
        if record is None:
            return None
        raise NativeUpdateRecoveryRequired(
            receipt=record, stage=record["stage"]
        ) from None
    except (OSError, ValueError, TypeError):
        raise NativeUpdateRecoveryRequired(
            receipt=record, stage=record["stage"] if record else "runtime_restore"
        ) from None


# Compatibility name used by the updater's coordinated artifact adapter.
accepted_native_runtime_metadata = native_runtime_restore_metadata


@contextmanager
def _journal_lock(path):
    root = path.parent
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_identity = _directory(root)
    value = root.lstat()
    if value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) & 0o077:
        raise NativeUpdateRecoveryRequired(
            "The native update journal directory is not private to its controller."
        )
    lock = path.with_suffix(".lock")
    descriptor = os.open(
        lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    try:
        value = os.fstat(descriptor)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or value.st_uid != os.geteuid()
            or stat.S_IMODE(value.st_mode) & 0o077
        ):
            raise NativeUpdateRecoveryRequired()
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise NativeUpdateRecoveryRequired(
                "Another native runtime update owns its journal."
            ) from None
        _same(root, root_identity)
        yield root_identity
    finally:
        os.close(descriptor)


def _call(callback, *args):
    result = callback(*args)
    if result is False or (
        isinstance(result, tuple) and result and result[0] in (False, None)
    ):
        raise NativeUpdateError("A native update lifecycle operation did not succeed.")
    return result


def _owned_callback(record, callback, *args):
    # Child async contexts can outlive their creator. Share a revocable scope,
    # rather than a permanent tuple that a copied ContextVar could retain.
    owner = {"runtime": record["runtime"], "id": record["id"], "active": True}
    token = _UPDATE_OWNER.set(owner)
    try:
        return _call(callback, *args)
    finally:
        owner["active"] = False
        _UPDATE_OWNER.reset(token)


def _archive_journal(path, record):
    """Keep prior terminal receipts when a later update takes the current slot."""
    history = path.parent / f"{path.stem}-{record['id']}.history"
    encoded = json.dumps(record, sort_keys=True)
    if history.exists() or history.is_symlink():
        if read_private_text(history, max_bytes=32768) != encoded:
            raise NativeUpdateRecoveryRequired()
        return
    descriptor = os.open(
        history, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(descriptor, "w") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _save_record(path, record, journal_identity, stage, *, state="active"):
    _same(path.parent, journal_identity)
    record.update(stage=stage, state=state, updated_at=int(time.time()))
    atomic_write_private_text(path, json.dumps(record, sort_keys=True))
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _restore(
    runtime,
    candidate,
    previous,
    old_identity,
    new_identity,
    parent_identity,
    staging_identity,
):
    if identity(runtime) == old_identity:
        if identity(previous) is not None:
            raise NativeUpdateRecoveryRequired()
        return
    if identity(previous) != old_identity:
        raise NativeUpdateRecoveryRequired()
    if identity(runtime) == new_identity:
        rename_exclusive(
            runtime, candidate, new_identity, parent_identity, staging_identity
        )
    elif identity(runtime) is not None:
        raise NativeUpdateRecoveryRequired()
    rename_exclusive(previous, runtime, old_identity, parent_identity, parent_identity)
    _same(runtime, old_identity)


def perform_native_update(
    service,
    staged_directory,
    release_metadata,
    *,
    journal_root,
    stop_current,
    prepare_launch,
    start_candidate,
    wait_ready,
    save_selection,
    verify_admission=None,
    retire_rollback=None,
    was_running=True,
    target_selector=None,
):
    """Publish one staged runtime; callbacks own process/provider policy.

    A prelaunch failure restores only exact owned runtime paths and never starts
    a process. The caller may restart its verified previous runtime then. After
    launch authorization, all failures require recovery and retain both runtimes;
    starting an older binary on potentially upgraded data is never attempted.
    """
    runtime = _direct(runtime_dir(service))
    path = _journal_path(service, journal_root)
    verify_admission = verify_admission or (lambda: None)
    with _journal_lock(path) as journal_identity:
        try:
            previous_record = _load(path, runtime)
        except FileNotFoundError:
            previous_record = None
        except (OSError, ValueError, TypeError):
            raise NativeUpdateRecoveryRequired() from None
        if previous_record and previous_record["state"] not in _TERMINAL:
            raise NativeUpdateRecoveryRequired(
                receipt=previous_record, stage=previous_record["stage"]
            )
        record = {}
        try:
            if type(was_running) is not bool:
                raise NativeUpdateError(
                    "Native update running state must be verified before publication."
                )
            _call(verify_admission)
            metadata = _metadata(release_metadata)
            original_selector = _selector(service.get("v2_release_version"))
            selected = _selector(target_selector or metadata["release"])
            if selected not in {"latest", metadata["release"]}:
                raise NativeUpdateError(
                    "The native update selector differs from its verified release."
                )
            candidate = _direct(staged_directory)
            if (
                candidate == runtime
                or runtime in candidate.parents
                or candidate in runtime.parents
            ):
                raise NativeUpdateError(
                    "The staged native runtime overlaps the current runtime."
                )
            old_identity, new_identity = _directory(runtime), _directory(candidate)
            parent_identity, staging_identity = _directory(runtime.parent), _directory(
                candidate.parent
            )
            if old_identity["device"] != new_identity["device"] or os.path.ismount(
                runtime
            ):
                raise NativeUpdateError(
                    "Native runtime publication requires one local filesystem and a dedicated directory."
                )
            _artifact(candidate, metadata, new_identity)
            old_bindings = _bindings(runtime)
            if previous_record:
                _archive_journal(path, previous_record)
            identifier = secrets.token_hex(16)
            previous = runtime.parent / f".{runtime.name}.dumb-previous-{identifier}"
            record = {
                "format": _FORMAT,
                "id": identifier,
                "state": "active",
                "stage": "prepared",
                "runtime": str(runtime),
                "previous_runtime": str(previous),
                "previous_identity": old_identity,
                "previous_bindings": old_bindings,
                "candidate_identity": new_identity,
                "release_metadata": metadata,
                "service_fingerprint": _service_fingerprint(service),
                "original_selector": original_selector,
                "target_selector": selected,
                "launched": False,
                "was_running": was_running,
                "created_at": int(time.time()),
            }

            def save(stage, *, state="active"):
                _save_record(path, record, journal_identity, stage, state=state)

            save("prepared")
        except NativeUpdateError:
            raise
        except (OSError, ValueError, TypeError):
            raise NativeUpdateError(
                "The verified native runtime update could not be prepared."
            ) from None
        try:
            save("stop_current")
            _call(stop_current)
            _call(verify_admission)
            _same(runtime, old_identity)
            if _bindings(runtime) != old_bindings:
                raise NativeUpdateError(
                    "The previous native runtime changed before publication."
                )
            _artifact(candidate, metadata, new_identity)
            save("retain_previous")
            rename_exclusive(
                runtime, previous, old_identity, parent_identity, parent_identity
            )
            save("publish_candidate")
            rename_exclusive(
                candidate, runtime, new_identity, staging_identity, parent_identity
            )
            save("prepare_launch")
            _owned_callback(record, prepare_launch)
            _artifact(runtime, metadata, new_identity)
            _call(verify_admission)
            if retire_rollback is not None:
                _call(retire_rollback, copy.deepcopy(record))
            # Write-ahead boundary: a callback that raises may still have started
            # a process that applies native database migrations immediately.
            record["launched"] = True
            save("launch_authorized")
            _owned_callback(record, start_candidate)
            save("wait_ready")
            _owned_callback(record, wait_ready)
            _artifact(runtime, metadata, new_identity)
            save("save_selection")
            _call(save_selection, copy.deepcopy(metadata))
            save("completed", state="completed")
            return copy.deepcopy(record)
        except BaseException as error:
            stage = record["stage"]
            recovery = record["launched"]
            if not recovery:
                try:
                    _restore(
                        runtime,
                        candidate,
                        previous,
                        old_identity,
                        new_identity,
                        parent_identity,
                        staging_identity,
                    )
                    if _bindings(runtime) != old_bindings:
                        raise NativeUpdateRecoveryRequired()
                    save(stage, state="restored")
                except BaseException:
                    recovery = True
            if recovery:
                try:
                    save(stage, state="recovery_required")
                except (OSError, ValueError):
                    # The earlier write-ahead journal remains a blocker even if
                    # storage fails while recording the final recovery state.
                    pass
            if not isinstance(error, Exception):
                raise
            if recovery:
                raise NativeUpdateRecoveryRequired(
                    receipt=record, stage=stage
                ) from error
            raise NativeUpdateError(
                "The native update stopped before launch. The previous runtime was restored; no older binary was started automatically.",
                receipt=record,
                stage=stage,
            ) from error


def resume_native_update(
    service,
    *,
    journal_root,
    prepare_launch,
    start_candidate,
    wait_ready,
    save_selection,
    verify_admission=None,
):
    """Explicitly retry only the retained replacement, never an older binary.

    Only an already published, launch-authorized replacement is eligible. Its
    exact directory, marker and full binary hashes must still match. Interrupted
    prelaunch renames and changed evidence remain held for private review.
    """
    runtime = _direct(runtime_dir(service))
    path = _journal_path(service, journal_root)
    verify_admission = verify_admission or (lambda: None)
    with _journal_lock(path) as journal_identity:
        try:
            record = _load(path, runtime)
        except (OSError, ValueError, TypeError):
            raise NativeUpdateRecoveryRequired() from None
        if (
            record["state"] not in {"active", "recovery_required"}
            or not record["launched"]
        ):
            raise NativeUpdateRecoveryRequired(receipt=record, stage=record["stage"])
        metadata = record["release_metadata"]
        previous = _direct(record["previous_runtime"])
        try:
            if _service_fingerprint(service) != record["service_fingerprint"]:
                raise NativeUpdateRecoveryRequired()
            if _selector(service.get("v2_release_version")) not in {
                record["original_selector"],
                record["target_selector"],
            }:
                raise NativeUpdateRecoveryRequired()
            _call(verify_admission)
            _same(previous, record["previous_identity"])
            if _bindings(previous) != record["previous_bindings"]:
                raise NativeUpdateRecoveryRequired()
            _artifact(runtime, metadata, record["candidate_identity"])
            _save_record(path, record, journal_identity, "resume_prepare")
            _owned_callback(record, prepare_launch)
            _artifact(runtime, metadata, record["candidate_identity"])
            _call(verify_admission)
            _save_record(path, record, journal_identity, "resume_launch_authorized")
            _owned_callback(record, start_candidate)
            _save_record(path, record, journal_identity, "resume_wait_ready")
            _owned_callback(record, wait_ready)
            _artifact(runtime, metadata, record["candidate_identity"])
            _save_record(path, record, journal_identity, "resume_save_selection")
            _call(save_selection, copy.deepcopy(metadata))
            _save_record(path, record, journal_identity, "completed", state="completed")
            return copy.deepcopy(record)
        except BaseException as error:
            stage = record["stage"]
            try:
                _save_record(
                    path, record, journal_identity, stage, state="recovery_required"
                )
            except (OSError, ValueError):
                pass
            if not isinstance(error, Exception):
                raise
            raise NativeUpdateRecoveryRequired(receipt=record, stage=stage) from error
