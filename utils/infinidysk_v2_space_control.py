"""Archive-backed reuse of a stopped v1 tree, with durable recovery checkpoints."""

import copy
from contextlib import closing
import ctypes
import os
from pathlib import Path
import stat
import shutil

from utils.backup_storage import _directory, _mount, get_backup_storage, mount_records
from utils.infinidysk_v2_archive import verify_retained_archive
from utils.infinidysk_v2_rehearsal_cleanup import discard_rehearsal_candidate
from utils.infinidysk_v2_space import restore_archive_tree


def identity(path):
    try:
        value = Path(path).lstat()
    except FileNotFoundError:
        return None
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": stat.S_IFMT(value.st_mode),
    }


def rename_exclusive(source, destination, expected, source_parent, destination_parent):
    """Linux no-replace rename, anchored to the checkpointed parent identities."""
    source, destination = Path(source), Path(destination)
    with _directory(source.parent) as old, _directory(destination.parent) as new:
        for descriptor, expected_parent in (
            (old, source_parent),
            (new, destination_parent),
        ):
            value = os.fstat(descriptor)
            if {
                "device": value.st_dev,
                "inode": value.st_ino,
                "mode": stat.S_IFMT(value.st_mode),
            } != expected_parent:
                raise ValueError("A checkpointed rename parent changed.")
        value = os.stat(source.name, dir_fd=old, follow_symlinks=False)
        if {
            "device": value.st_dev,
            "inode": value.st_ino,
            "mode": stat.S_IFMT(value.st_mode),
        } != expected:
            raise ValueError("The owned source changed before rename.")
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ValueError("Atomic no-replace directory rename is unavailable.")
        if rename(old, os.fsencode(source.name), new, os.fsencode(destination.name), 1):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        os.fsync(old)
        os.fsync(new)
        if identity(destination) != expected:
            raise ValueError(
                "Directory ownership changed during rename; retain both paths for review."
            )


def preflight_reuse(plan):
    """A directory rename reuses blocks; it never manufactures free space."""
    source = Path(plan["source"]["data_dir"])
    runtime = Path(plan["source"]["runtime_dir"])
    target = Path(plan["target"]["data_dir"])
    backup = plan.get("backup_storage") or {}
    if (
        plan["mode"] != "migrate"
        or backup.get("mode") != "cold_archive"
        or not backup.get("artifact_root")
    ):
        raise ValueError(
            "Reusing local v1 storage requires an existing-source migration and a registered archive destination."
        )
    parent = target.parent
    while not parent.exists():
        parent = parent.parent
    if not source.is_dir() or source.is_symlink():
        raise ValueError("Local storage reuse requires a direct v1 data directory.")
    if _mount(source)["mount_id"] != _mount(source.parent)["mount_id"]:
        raise ValueError(
            "The v1 data directory is itself a mount point and cannot be reused by rename."
        )
    if (
        _mount(source.parent)["mount_id"] != _mount(parent)["mount_id"]
        or source.stat().st_dev != parent.stat().st_dev
    ):
        raise ValueError(
            "Reusing local v1 storage requires source and candidate on the same local mount."
        )
    if any(
        Path(row["path"]) == source or source in Path(row["path"]).parents
        for row in mount_records()
    ):
        raise ValueError(
            "The v1 tree contains a mounted filesystem; local reuse is unavailable."
        )
    if runtime != source and runtime in source.parents:
        raise ValueError(
            "A v1 runtime containing its data directory cannot use local storage reuse."
        )
    from utils.infinidysk_v2_inventory import iter_tree

    with closing(
        iter_tree(source, role="Source for local storage reuse", reject_hardlinks=True)
    ) as rows:
        for _ in rows:
            pass
    if Path(backup["artifact_root"]).stat().st_dev == source.stat().st_dev:
        raise ValueError(
            "Choose an archive destination on a different filesystem before reusing local v1 storage."
        )


class SpaceReuse:
    def __init__(self, manager):
        self.manager = manager

    def _check_owner(self, job):
        check = getattr(self.manager, "_assert_job_owner", None)
        if callable(check):
            check(job)

    def _save(self, job):
        self._check_owner(job)
        self.manager._write(self.manager._job_path(job["job_id"]), job)

    def _storage(self, job):
        storage = get_backup_storage(self.manager.config_manager)
        selected = job["plan"]["backup_storage"]
        if (
            storage.bound_identity(selected["destination_id"])["binding_fingerprint"]
            != selected["binding_fingerprint"]
        ):
            raise ValueError(
                "The archive destination changed; v1 recovery remains pending."
            )
        return storage

    def verify(self, job):
        snapshot = job.get("snapshot") or {}
        recovery = snapshot.get("space_recovery")
        if not recovery:
            raise ValueError("Archive-backed source recovery evidence is missing.")
        proof = verify_retained_archive(snapshot, job["plan"], self._storage(job))
        return proof

    def prepare(self, job, workspace, snapshot, owner_check):
        plan = job["plan"]
        preflight_reuse(plan)
        source, candidate = Path(plan["source"]["data_dir"]), Path(
            workspace["candidate_data"]
        )
        candidate.parent.mkdir(parents=True, exist_ok=True)
        if (
            candidate.exists()
            or candidate.is_symlink()
            or identity(source) != plan["source"]["data_identity"]
        ):
            raise ValueError(
                "The source or private candidate changed before local storage reuse."
            )
        recovery = {
            "state": "prepared",
            "source": str(source),
            "candidate": str(candidate),
            "source_identity": identity(source),
            "source_parent_identity": identity(source.parent),
            "candidate_parent_identity": identity(candidate.parent),
        }
        snapshot["space_recovery"] = recovery
        snapshot["candidate_identity"] = identity(source)
        # The complete archive receipt and intent must survive an API restart
        # before the first rename makes the original path unavailable.
        job["snapshot"] = snapshot
        self._save(job)
        self.verify(job)
        owner_check()
        self.manager._assert_stopped(plan)
        rename_exclusive(
            source,
            candidate,
            recovery["source_identity"],
            recovery["source_parent_identity"],
            recovery["candidate_parent_identity"],
        )
        recovery["state"] = "reused"
        self._save(job)
        return identity(candidate)

    def _restore_capacity(self, job, candidate):
        from utils.infinidysk_v2_archive import _source_leaf
        from utils.infinidysk_v2_inventory import MAX_TREE_MANIFEST_BYTES, iter_tree

        snapshot = job["snapshot"]
        required = snapshot.get(
            "source_logical_bytes", snapshot["cold_archive"]["size_bytes"]
        )
        if type(required) is not int or required < 0:
            raise ValueError("The archived source storage requirement is unavailable.")
        allocated = 0
        expected = identity(candidate)
        if expected is not None:
            with closing(
                iter_tree(candidate, include_identity=True, reject_hardlinks=True)
            ) as rows:
                for row in rows:
                    if not stat.S_ISREG(row["mode"]):
                        continue
                    with _source_leaf(candidate, row["path"], expected) as (
                        parent,
                        name,
                    ):
                        value = os.stat(name, dir_fd=parent, follow_symlinks=False)
                        if (value.st_dev, value.st_ino) != (
                            row["identity"]["device"],
                            row["identity"]["inode"],
                        ):
                            raise ValueError(
                                "Candidate storage identity changed during restore capacity estimation."
                            )
                        allocated += min(value.st_size, value.st_blocks * 512)
        # Allocation/reclaim is an estimate on filesystems with shared extents;
        # no free-space figure is inflated or represented as a reservation.
        margin = max(64 * 1024 * 1024, required // 10)
        available = shutil.disk_usage(candidate.parent).free + allocated
        if required + MAX_TREE_MANIFEST_BYTES + margin > available:
            raise ValueError(
                "Estimated space for archive restoration is insufficient. The v2 candidate was retained; make local space before retrying recovery."
            )

    def restore(self, job, assert_unmounted):
        self._check_owner(job)
        snapshot = job.get("snapshot") or {}
        recovery = snapshot.get("space_recovery")
        if not recovery:
            return
        plan, workspace = job["plan"], self.manager._workspace(job)
        source, candidate = Path(recovery["source"]), Path(recovery["candidate"])
        target = Path(plan["target"]["data_dir"])
        self.manager._assert_stopped(plan)
        group = (job.get("execution") or {}).get("native_process_group")
        payload = Path(workspace["root"]) / "candidate-payload.json"
        if type(group) is not int and (payload.exists() or payload.is_symlink()):
            raise ValueError(
                "An interrupted contained importer has unknown ownership; retain its candidate for review."
            )
        if type(group) is int:
            from utils.infinidysk_v2_execution import _native_group_has_live_members

            if _native_group_has_live_members(group):
                raise ValueError(
                    "The contained importer is still alive; archive recovery is blocked."
                )
        for path in (source, candidate, target):
            assert_unmounted(str(path))
        if (
            identity(source.parent) != recovery["source_parent_identity"]
            or identity(candidate.parent) != recovery["candidate_parent_identity"]
        ):
            raise ValueError("A recovery parent changed; no source was replaced.")
        if (
            recovery["state"] == "prepared"
            and identity(source) == recovery["source_identity"]
            and identity(candidate) is None
        ):
            recovery["state"] = "restored"
            recovery["restored_identity"] = identity(source)
            self._save(job)
            return
        if identity(source) is not None:
            expected = recovery.get("restored_identity") or recovery.get(
                "restore_identity"
            )
            if (
                recovery["state"] in {"restoring", "restored"}
                and identity(source) == expected
            ):
                # Covers a crash after publishing a complete restored tree.
                if recovery["state"] != "restored":
                    self._verify_restored(job, source)
                recovery["state"] = "restored"
                recovery["restored_identity"] = identity(source)
                self._save(job)
                return
            raise ValueError(
                "The original v1 path is occupied; archive recovery refused to replace it."
            )
        # Never discard the sole local working tree until the full external
        # archive has been opened and verified again at recovery time.
        self.verify(job)
        if recovery["state"] != "restoring":
            if (
                identity(target) == snapshot["candidate_identity"]
                and identity(candidate) is None
            ):
                recovery["state"] = "returning_candidate"
                self._save(job)
                rename_exclusive(
                    target,
                    candidate,
                    snapshot["candidate_identity"],
                    recovery["candidate_parent_identity"],
                    recovery["candidate_parent_identity"],
                )
            if identity(candidate) != snapshot["candidate_identity"]:
                if identity(candidate) is not None or recovery["state"] != "discarding":
                    raise ValueError(
                        "The owned native candidate is missing or changed; retain recovery evidence."
                    )
            else:
                self._restore_capacity(job, candidate)
                if recovery.get("native_mutations_authorized") is not True:
                    self._verify_restored(job, candidate)
                recovery["state"] = "discarding"
                self._save(job)
                discard_rehearsal_candidate(plan, workspace, snapshot, assert_unmounted)
            recovery["state"] = "restoring"
            self._save(job)
        elif identity(candidate) is not None:
            # A prior extraction stopped. Its exclusively created identity is
            # required before removing only that private partial restore.
            expected = recovery.get("restore_identity")
            if identity(candidate) != expected:
                raise ValueError(
                    "The private partial restore is unowned; recovery stopped."
                )
            discard_rehearsal_candidate(
                plan, workspace, {"candidate_identity": expected}, assert_unmounted
            )
        if candidate.exists() or candidate.is_symlink():
            raise ValueError("The private restore path is occupied.")
        self._restore_capacity(job, candidate)
        candidate.mkdir(mode=0o700)
        recovery["restore_identity"] = identity(candidate)
        self._save(job)
        restore_archive_tree(
            snapshot,
            plan,
            self._storage(job),
            source=str(source),
            destination=str(candidate),
            expected_identity=recovery["restore_identity"],
            owner_check=lambda: self._check_owner(job),
        )
        self.manager._assert_stopped(plan)
        if (
            identity(source) is not None
            or identity(candidate) != recovery["restore_identity"]
        ):
            raise ValueError(
                "Source ownership changed before publishing archive recovery."
            )
        rename_exclusive(
            candidate,
            source,
            recovery["restore_identity"],
            recovery["candidate_parent_identity"],
            recovery["source_parent_identity"],
        )
        recovery["state"] = "restored"
        recovery["restored_identity"] = identity(source)
        self._save(job)

    def _verify_restored(self, job, source):
        from utils.infinidysk_v2_inventory import tree_fingerprint

        snapshot = job["snapshot"]
        if (
            tree_fingerprint(
                source, scratch_root=snapshot["source_snapshot"], validate_links=True
            )
            != snapshot["original_fingerprints"][job["plan"]["source"]["data_dir"]]
        ):
            raise ValueError("The restored v1 tree does not match its archived source.")


def restored_plan_matches(previous, current):
    """Only verified restored inodes may change after a low-space rehearsal."""

    def binding(plan):
        result = copy.deepcopy(
            {
                key: plan.get(key)
                for key in (
                    "mode",
                    "options",
                    "source",
                    "target",
                    "inventory",
                    "backup_storage",
                )
            }
        )
        source = result["source"]
        source.pop("process_identity", None)
        source.pop("data_identity", None)
        data = Path(source["data_dir"])
        if (
            Path(source["runtime_dir"]) == data
            or data in Path(source["runtime_dir"]).parents
        ):
            source["runtime_identity"].pop("directory", None)
        for alias in source.get("aliases") or []:
            if Path(alias["target"]) == data or data in Path(alias["target"]).parents:
                alias.pop("target_identity", None)
        for item in result["inventory"]["consumers"]:
            item.pop("process_identity", None)
        for item in result["inventory"]["mounts"]:
            item.pop("identity", None)
            item.pop("mounted", None)
        return result

    return binding(previous) == binding(current)
