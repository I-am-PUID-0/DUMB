"""Identity-bound per-filesystem restore transactions; no filename-only rollback."""

import os
import hashlib
from contextlib import closing
from pathlib import Path
import stat

from utils.backup_storage import _directory, mount_records
from utils.installation_backup_archive import (
    InstallationArchiveError,
    identity,
    source_rows,
    open_leaf,
)


def stamp(path):
    return identity(Path(path).lstat()) if os.path.lexists(path) else None


def _metadata(value):
    return {
        "uid": value.st_uid,
        "gid": value.st_gid,
        "mode": stat.S_IMODE(value.st_mode),
        "mtime_ns": value.st_mtime_ns,
    }


def _root_metadata(path, record):
    with _directory(path) as descriptor:
        if os.geteuid() == 0:
            os.fchown(descriptor, record["uid"], record["gid"])
        elif (record["uid"], record["gid"]) != (os.geteuid(), os.getegid()):
            raise InstallationArchiveError(
                "The archived directory-root ownership cannot be restored."
            )
        os.fchmod(descriptor, record["mode"])
        os.utime(descriptor, ns=(record["mtime_ns"], record["mtime_ns"]))
        os.fsync(descriptor)


def _same(path, expected):
    return stamp(path) == expected


def _unmounted(path):
    root = Path(path)
    if any(
        Path(row["path"]) == root or root in Path(row["path"]).parents
        for row in mount_records()
    ):
        raise InstallationArchiveError(
            "A transaction subtree is mounted; no detach or recursive mutation is allowed."
        )


def _copy(source, destination, allocated=None):
    source, destination = Path(source), Path(destination)
    root = {"id": "copy", "path": str(source), "identity": stamp(source)}
    with closing(source_rows(root)) as rows:
        for row, original, expected in rows:
            relative = original.relative_to(source)
            copied = destination / relative
            mode = row["mode"]
            if stat.S_ISDIR(mode):
                copied.mkdir(mode=0o700)
            elif stat.S_ISLNK(mode):
                os.symlink(row["link"], copied)
            else:
                with open_leaf(original) as (parent, leaf):
                    incoming = os.open(
                        leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
                    )
                    outgoing = None
                    try:
                        outgoing = os.open(
                            copied,
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600,
                        )
                        if allocated is not None:
                            allocated(identity(os.fstat(outgoing)))
                        value = os.fstat(incoming)
                        actual = [
                            value.st_dev,
                            value.st_ino,
                            value.st_mode,
                            value.st_size,
                            value.st_mtime_ns,
                            value.st_ctime_ns,
                        ]
                        if actual != expected:
                            raise InstallationArchiveError(
                                "A verified staging entry changed before its target copy."
                            )
                        digest = hashlib.sha256()
                        while chunk := os.read(incoming, 1024 * 1024):
                            digest.update(chunk)
                            pending = memoryview(chunk)
                            while pending:
                                pending = pending[os.write(outgoing, pending) :]
                        os.fsync(outgoing)
                    finally:
                        if outgoing is not None:
                            os.close(outgoing)
                        os.close(incoming)
                verification = hashlib.sha256()
                descriptor = os.open(copied, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    while chunk := os.read(descriptor, 1024 * 1024):
                        verification.update(chunk)
                finally:
                    os.close(descriptor)
                if digest.digest() != verification.digest():
                    raise InstallationArchiveError(
                        "A target filesystem copy failed complete checksum readback."
                    )
    # Namespace is now complete; metadata changes do not create/remove children.
    with closing(source_rows(root)) as rows:
        for row, original, _expected in rows:
            copied = destination / original.relative_to(source)
            if os.geteuid() == 0:
                os.chown(copied, row["uid"], row["gid"], follow_symlinks=False)
            elif (row["uid"], row["gid"]) != (os.geteuid(), os.getegid()):
                raise InstallationArchiveError(
                    "The original file ownership cannot be restored."
                )
            if not stat.S_ISLNK(row["mode"]):
                os.chmod(copied, stat.S_IMODE(row["mode"]))
            os.utime(
                copied, ns=(row["mtime_ns"], row["mtime_ns"]), follow_symlinks=False
            )
            if stat.S_ISDIR(row["mode"]):
                with _directory(copied) as descriptor:
                    os.fsync(descriptor)
            elif stat.S_ISREG(row["mode"]):
                descriptor = os.open(copied, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)


def _sync_move(source, target):
    # The journal intent is already durable. Both directory entries must be too.
    with _directory(Path(source).parent) as first:
        with _directory(Path(target).parent) as second:
            os.rename(
                Path(source).name,
                Path(target).name,
                src_dir_fd=first,
                dst_dir_fd=second,
            )
            os.fsync(first)
            os.fsync(second)


def _file_proof(path):
    with open_leaf(path) as (parent, leaf):
        descriptor = os.open(
            leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
        )
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise InstallationArchiveError(
                    "A controller file must be an unlinked regular file."
                )
            digest = hashlib.sha256()
            while chunk := os.read(descriptor, 1024 * 1024):
                digest.update(chunk)
            after = os.fstat(descriptor)
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)

            def signature(value):
                return (
                    identity(value),
                    value.st_size,
                    value.st_mtime_ns,
                    value.st_ctime_ns,
                )

            if signature(before) != signature(after) or signature(after) != signature(
                named
            ):
                raise InstallationArchiveError(
                    "A controller file changed while being verified."
                )
            return {
                "sha256": digest.hexdigest(),
                "size": before.st_size,
                "metadata": _metadata(before),
            }
        finally:
            os.close(descriptor)


def _retain_file(row, record, field, source, destination, proof, persist):
    """Retain a verified copy, journaling its inode before the first write."""
    destination = Path(destination)
    ready = field + "_ready"
    if record.get(ready):
        if stamp(destination) != record.get(field) or _file_proof(destination) != proof:
            raise InstallationArchiveError("A retained controller copy changed.")
        return
    if _file_proof(source) != proof:
        raise InstallationArchiveError("The controller file changed before retention.")
    if os.path.lexists(destination):
        with open_leaf(destination) as (parent, leaf):
            value = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if (
                record.get(field) is None
                or identity(value) != record[field]
                or not stat.S_ISREG(value.st_mode)
                or value.st_nlink != 1
            ):
                raise InstallationArchiveError(
                    "An unrecognized partial controller copy exists."
                )
            # Only this journal's incomplete copy can be replaced. The live
            # original and any completed recovery copy remain untouched.
            os.unlink(leaf, dir_fd=parent)
            os.fsync(parent)

    def allocated(value):
        record[field], record[ready] = value, False
        persist(row)
        with _directory(destination.parent) as descriptor:
            os.fsync(descriptor)

    _copy(source, destination, allocated=allocated)
    if _file_proof(source) != proof or _file_proof(destination) != proof:
        raise InstallationArchiveError("Controller retention failed complete readback.")
    record[ready] = True
    persist(row)


def _prepare_atomic(row, name, source, staged, previous, persist):
    old = row["old"].get(name) if name is not None else row["old"]
    new = row["new"].get(name) if name is not None else row["new"]
    if (
        new is None
        or new[2] != stat.S_IFREG
        or (old is not None and old[2] != stat.S_IFREG)
    ):
        raise InstallationArchiveError(
            "Atomic controller recovery requires regular files."
        )
    record = {
        "original": old,
        "new": new,
        "previous": None,
        "previous_ready": old is None,
        "discarded": None,
        "discarded_ready": False,
        "old_proof": _file_proof(source) if old is not None else None,
        "new_proof": _file_proof(staged),
    }
    if name is None:
        row["atomic_file"] = record
    else:
        row.setdefault("atomic_files", {})[name] = record
    persist(row)
    if old is not None:
        _retain_file(
            row, record, "previous", source, previous, record["old_proof"], persist
        )


def _validate_atomic(record, target, previous, discarded):
    current, saved, retained_new = stamp(target), stamp(previous), stamp(discarded)
    if current not in (None, record["original"], record["previous"], record["new"]):
        raise InstallationArchiveError(
            "An atomic controller target has an unrecognized identity."
        )
    if saved not in (None, record["previous"]) or retained_new not in (
        None,
        record["discarded"],
    ):
        raise InstallationArchiveError("Atomic controller recovery evidence changed.")
    if (
        saved is not None
        and record["previous_ready"]
        and _file_proof(previous) != record["old_proof"]
    ):
        raise InstallationArchiveError(
            "The original controller copy failed verification."
        )
    if (
        retained_new is not None
        and record["discarded_ready"]
        and _file_proof(discarded) != record["new_proof"]
    ):
        raise InstallationArchiveError(
            "The discarded controller copy failed verification."
        )
    if record["original"] is not None and current not in (
        record["original"],
        record["previous"],
    ):
        if not record["previous_ready"] or saved != record["previous"]:
            raise InstallationArchiveError(
                "The verified original controller copy is missing."
            )


def _rollback_atomic(row, record, target, previous, discarded, persist):
    if stamp(target) == record["new"]:
        if _file_proof(target) != record["new_proof"]:
            raise InstallationArchiveError(
                "The live controller replacement changed before rollback."
            )
        if record["original"] is None:
            record["discarded"], record["discarded_ready"] = record["new"], True
            persist(row)
            _sync_move(target, discarded)
        else:
            _retain_file(
                row,
                record,
                "discarded",
                target,
                discarded,
                record["new_proof"],
                persist,
            )
            if (
                stamp(previous) != record["previous"]
                or _file_proof(previous) != record["old_proof"]
            ):
                raise InstallationArchiveError(
                    "The original controller copy changed before rollback."
                )
            _sync_move(previous, target)
    elif stamp(target) is None and record["original"] is not None:
        if (
            not record["previous_ready"]
            or stamp(previous) != record["previous"]
            or _file_proof(previous) != record["old_proof"]
        ):
            raise InstallationArchiveError(
                "The original controller file cannot be recovered."
            )
        _sync_move(previous, target)


def prepare(root, staged, identifier, protected, persist):
    target = Path(root["path"])
    if root["kind"] == "directory":
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _directory(target) as descriptor:
            old_metadata = _metadata(os.fstat(descriptor))
            new_metadata = _metadata(Path(staged).lstat())
            holder = target / f".dumb-installation-restore-{identifier}"
            old = {}
            protected_names = set()
            for raw in protected:
                path = Path(raw)
                if target in path.parents:
                    relative = path.relative_to(target)
                    if len(relative.parts) != 1:
                        raise InstallationArchiveError(
                            "A nested retained backup/control path requires relocation outside this restore root."
                        )
                    protected_names.add(relative.parts[0])
            with os.scandir(descriptor) as iterator:
                for entry in iterator:
                    if entry.name not in protected_names:
                        old[entry.name] = identity(
                            os.stat(
                                entry.name, dir_fd=descriptor, follow_symlinks=False
                            )
                        )
            if holder.name in old:
                raise InstallationArchiveError(
                    "A foreign restore reservation already exists."
                )
            row = {
                "root": root,
                "target_identity": identity(os.fstat(descriptor)),
                "old_metadata": old_metadata,
                "new_metadata": new_metadata,
                "holder": str(holder),
                "old": old,
                "new": {},
                "phase": "preparing",
                "protected": sorted(protected_names),
            }
            persist(row)
            os.mkdir(holder.name, 0o700, dir_fd=descriptor)
            row["holder_identity"] = stamp(holder)
            persist(row)
        _copy(staged, holder / "staged")
        (holder / "previous").mkdir(mode=0o700)
        (holder / "restored").mkdir(mode=0o700)
        new = {}
        for child in (holder / "staged").iterdir():
            if child.name in protected_names or child.name == holder.name:
                raise InstallationArchiveError(
                    "The archive selects a retained recovery/backup path."
                )
            new[child.name] = stamp(child)
        if len(old) + len(new) > 16384:
            raise InstallationArchiveError(
                "Restore exceeds its bounded transaction journal."
            )
        row.update(new=new)
        for name in root.get("atomic_names") or []:
            if (
                not isinstance(name, str)
                or name in {"", ".", ".."}
                or Path(name).name != name
                or "\x00" in name
            ):
                raise InstallationArchiveError(
                    "An atomic controller filename is invalid."
                )
            _prepare_atomic(
                row,
                name,
                target / name,
                holder / "staged" / name,
                holder / "previous" / name,
                persist,
            )
        row["phase"] = "prepared"
        persist(row)
        return row
    with _directory(target.parent) as descriptor:
        holder = target.parent / f".dumb-installation-file-{identifier}-{root['id']}"
        row = {
            "root": root,
            "target_parent": identity(os.fstat(descriptor)),
            "holder": str(holder),
            "old": stamp(target),
            "new": None,
            "phase": "preparing",
        }
        persist(row)
        os.mkdir(holder.name, 0o700, dir_fd=descriptor)
        row["holder_identity"] = stamp(holder)
        persist(row)
    _copy(staged, holder / "staged")
    row.update(new=stamp(holder / "staged"))
    if root.get("atomic_restore"):
        _prepare_atomic(
            row, None, target, holder / "staged", holder / "previous", persist
        )
    row["phase"] = "prepared"
    persist(row)
    return row


def validate(row):
    target, holder = Path(row["root"]["path"]), Path(row["holder"])
    expected_parent = (
        row.get("target_identity")
        if row["root"]["kind"] == "directory"
        else row["target_parent"]
    )
    with _directory(
        target if row["root"]["kind"] == "directory" else target.parent
    ) as descriptor:
        if identity(os.fstat(descriptor)) != expected_parent:
            raise InstallationArchiveError("A restore target anchor was replaced.")
    if row.get("holder_identity") and not _same(holder, row["holder_identity"]):
        raise InstallationArchiveError("The owned rollback holder was replaced.")
    if holder.exists():
        _unmounted(holder)
    if row["root"]["kind"] == "directory":
        old, new = row["old"], row["new"]
        allowed = set(old) | set(new) | set(row.get("protected") or []) | {holder.name}
        with os.scandir(target) as entries:
            if any(entry.name not in allowed for entry in entries):
                raise InstallationArchiveError(
                    "An unexpected entry appeared in the restore root."
                )
        for name in set(old) | set(new):
            atomic = row.get("atomic_files", {}).get(name)
            if atomic is not None:
                _validate_atomic(
                    atomic,
                    target / name,
                    holder / "previous" / name,
                    holder / "restored" / name,
                )
                continue
            current = stamp(target / name)
            previous = stamp(holder / "previous" / name)
            discarded = stamp(holder / "restored" / name)
            if current not in (None, old.get(name), new.get(name)):
                raise InstallationArchiveError(
                    "Rollback refuses an entry with an unrecognized identity."
                )
            if previous not in (None, old.get(name)) or discarded not in (
                None,
                new.get(name),
            ):
                raise InstallationArchiveError(
                    "Retained rollback entries no longer match the owned manifest."
                )
            if old.get(name) is not None and previous is None and current != old[name]:
                raise InstallationArchiveError("An original rollback entry is missing.")
    else:
        if row.get("atomic_file") is not None:
            _validate_atomic(
                row["atomic_file"], target, holder / "previous", holder / "restored"
            )
            return
        current, previous = stamp(target), stamp(holder / "previous")
        if stamp(holder / "restored") not in (None, row["new"]):
            raise InstallationArchiveError(
                "An unrecognized discarded replacement blocks rollback."
            )
        if current not in (None, row["old"], row["new"]) or previous not in (
            None,
            row["old"],
        ):
            raise InstallationArchiveError("A file rollback identity changed.")
        if row["old"] is not None and previous is None and current != row["old"]:
            raise InstallationArchiveError(
                "The original file rollback entry is missing."
            )


def apply(row, persist, checkpoint=None):
    checkpoint = checkpoint or (lambda *_: None)
    target, holder = Path(row["root"]["path"]), Path(row["holder"])
    validate(row)
    for name, record in row.get("atomic_files", {}).items():
        if (
            stamp(holder / "staged" / name) != record["new"]
            or _file_proof(holder / "staged" / name) != record["new_proof"]
        ):
            raise InstallationArchiveError(
                "The staged controller file failed full verification."
            )
    if row.get("atomic_file") is not None:
        record = row["atomic_file"]
        if (
            stamp(holder / "staged") != record["new"]
            or _file_proof(holder / "staged") != record["new_proof"]
        ):
            raise InstallationArchiveError(
                "The staged controller file failed full verification."
            )
    if row["root"]["kind"] == "directory":
        for name, expected in row["old"].items():
            _unmounted(target / name)
            if not _same(target / name, expected):
                raise InstallationArchiveError(
                    "An original entry changed before replacement."
                )
            checkpoint("before_old", name)
            if name not in row.get("atomic_files", {}):
                _sync_move(target / name, holder / "previous" / name)
            checkpoint("after_old", name)
        row["phase"] = "old_moved"
        persist(row)
        for name, expected in row["new"].items():
            atomic = row.get("atomic_files", {}).get(name)
            if atomic is not None:
                original = atomic["original"]
                if _file_proof(holder / "staged" / name) != atomic["new_proof"]:
                    raise InstallationArchiveError(
                        "The staged controller file changed before publication."
                    )
                if original is not None and (
                    not atomic["previous_ready"]
                    or stamp(holder / "previous" / name) != atomic["previous"]
                ):
                    raise InstallationArchiveError(
                        "The original controller copy is not ready for replacement."
                    )
                if not _same(target / name, original) or (
                    original is not None
                    and _file_proof(target / name) != atomic["old_proof"]
                ):
                    raise InstallationArchiveError(
                        "The original controller file changed before atomic replacement."
                    )
            if (atomic is None and os.path.lexists(target / name)) or not _same(
                holder / "staged" / name, expected
            ):
                raise InstallationArchiveError(
                    "The staged replacement identity changed."
                )
            checkpoint("before_new", name)
            _sync_move(holder / "staged" / name, target / name)
            checkpoint("after_new", name)
    else:
        if row["old"] is not None:
            checkpoint("before_old", target.name)
            if row.get("atomic_file") is None:
                _sync_move(target, holder / "previous")
            checkpoint("after_old", target.name)
        row["phase"] = "old_moved"
        persist(row)
        if row.get("atomic_file") is not None:
            record = row["atomic_file"]
            if _file_proof(holder / "staged") != record["new_proof"]:
                raise InstallationArchiveError(
                    "The staged controller file changed before publication."
                )
            if record["original"] is not None and (
                not record["previous_ready"]
                or stamp(holder / "previous") != record["previous"]
            ):
                raise InstallationArchiveError(
                    "The original controller copy is not ready for replacement."
                )
            if not _same(target, record["original"]) or (
                record["original"] is not None
                and _file_proof(target) != record["old_proof"]
            ):
                raise InstallationArchiveError(
                    "The original controller file changed before atomic replacement."
                )
        checkpoint("before_new", target.name)
        _sync_move(holder / "staged", target)
        checkpoint("after_new", target.name)
    if row["root"]["kind"] == "directory":
        _root_metadata(target, row["new_metadata"])
    row["phase"] = "installed"
    persist(row)
    with _directory(
        target if row["root"]["kind"] == "directory" else target.parent
    ) as descriptor:
        os.fsync(descriptor)


def rollback(row, persist):
    validate(row)
    target, holder = Path(row["root"]["path"]), Path(row["holder"])
    if row["root"]["kind"] == "directory":
        for name, expected in row["new"].items():
            if name in row.get("atomic_files", {}):
                record = row["atomic_files"][name]
                _rollback_atomic(
                    row,
                    record,
                    target / name,
                    holder / "previous" / name,
                    holder / "restored" / name,
                    persist,
                )
                continue
            if stamp(target / name) == expected:
                destination = holder / "restored" / name
                if os.path.lexists(destination):
                    raise InstallationArchiveError(
                        "An unexpected discarded replacement already exists."
                    )
                _sync_move(target / name, destination)
        for name, expected in row["old"].items():
            if name in row.get("atomic_files", {}):
                continue
            if stamp(holder / "previous" / name) == expected:
                if os.path.lexists(target / name):
                    raise InstallationArchiveError(
                        "An unexpected entry blocks exact original restoration."
                    )
                _sync_move(holder / "previous" / name, target / name)
    else:
        if row.get("atomic_file") is not None:
            _rollback_atomic(
                row,
                row["atomic_file"],
                target,
                holder / "previous",
                holder / "restored",
                persist,
            )
            row["phase"] = "rolled_back"
            persist(row)
            return
        if row["new"] is not None and stamp(target) == row["new"]:
            _sync_move(target, holder / "restored")
        if row["old"] is not None and stamp(holder / "previous") == row["old"]:
            if os.path.lexists(target):
                raise InstallationArchiveError(
                    "An unexpected file blocks original restoration."
                )
            _sync_move(holder / "previous", target)
    if row["root"]["kind"] == "directory":
        _root_metadata(target, row["old_metadata"])
    row["phase"] = "rolled_back"
    persist(row)
