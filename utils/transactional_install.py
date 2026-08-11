"""Same-filesystem candidate activation with durable rollback state."""

from __future__ import annotations

import errno
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from utils.global_logger import logger
from utils.install_cache import install_cache_root


def _reflink_or_copy(source: str, destination: str) -> str:
    """Use a copy-on-write clone when supported, with a safe copy fallback."""
    try:
        import fcntl

        with (
            open(source, "rb") as source_handle,
            open(destination, "xb") as destination_handle,
        ):
            fcntl.ioctl(destination_handle.fileno(), 0x40049409, source_handle.fileno())
        shutil.copystat(source, destination, follow_symlinks=False)
        return destination
    except (ImportError, OSError):
        Path(destination).unlink(missing_ok=True)
        return shutil.copy2(source, destination)


class TransactionError(RuntimeError):
    pass


class DirectoryReleaseTransaction:
    """Prepare a replacement directory and swap it into place safely.

    The candidate is always adjacent to the target, which keeps the activation
    rename on one filesystem. Docker overlay lower/merged directories cannot
    be renamed when redirect_dir is disabled, even though ``st_dev`` matches.
    That one-time case uses a complete rollback copy plus a journaled replace.
    A durable journal allows the next attempt to recover an interrupted swap
    before new work begins.
    """

    def __init__(self, target_dir: str, process_name: str):
        self.target = Path(target_dir).resolve()
        self.process_name = str(process_name)
        self.identifier = uuid.uuid4().hex
        self.candidate = self.target.parent / (
            f".{self.target.name}.dumb-candidate-{self.identifier}"
        )
        self.previous = self.target.parent / (
            f".{self.target.name}.dumb-previous-{self.identifier}"
        )
        self.previous_staging = self.target.parent / (
            f".{self.target.name}.dumb-previous-staging-{self.identifier}"
        )
        journal_name = f"{self.target.name}-{self.identifier}.json"
        self.journal = install_cache_root() / "transactions" / journal_name
        self.activated = False

    def _write_journal(self, state: str, message: str = "") -> None:
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "format": 1,
            "process_name": self.process_name,
            "target": str(self.target),
            "candidate": str(self.candidate),
            "previous": str(self.previous),
            "previous_staging": str(self.previous_staging),
            "state": state,
            "message": message,
            "updated_at": int(time.time()),
        }
        temporary = self.journal.with_name(f".{self.journal.name}.tmp")
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.journal)

    @staticmethod
    def recover_incomplete(target_dir: str) -> list[str]:
        target = Path(target_dir).resolve()
        transaction_root = install_cache_root() / "transactions"
        recovered = []
        if not transaction_root.is_dir():
            return recovered
        for journal in transaction_root.glob(f"{target.name}-*.json"):
            try:
                payload = json.loads(journal.read_text(encoding="utf-8"))
                if Path(payload.get("target", "")).resolve() != target:
                    continue
                candidate = Path(payload.get("candidate", ""))
                previous = Path(payload.get("previous", ""))
                previous_staging_value = payload.get("previous_staging")
                previous_staging = (
                    Path(previous_staging_value) if previous_staging_value else None
                )
                state = payload.get("state")
                if state == "copying_previous":
                    if previous_staging and previous_staging.is_dir():
                        shutil.rmtree(previous_staging, ignore_errors=True)
                    if previous.exists():
                        shutil.rmtree(previous, ignore_errors=True)
                    if candidate.exists():
                        shutil.rmtree(candidate, ignore_errors=True)
                    journal.unlink(missing_ok=True)
                    recovered.append("discarded_incomplete_overlay_copy")
                    continue
                if state == "replacing_overlay_target" and previous.exists():
                    if target.exists():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink(missing_ok=True)
                    os.replace(previous, target)
                    recovered.append("restored_overlay_previous")
                elif not target.exists() and previous.exists():
                    os.replace(previous, target)
                    recovered.append("restored_previous")
                elif previous.exists() and state in {
                    "activating",
                    "activated",
                    "rolling_back",
                    "rollback_failed",
                }:
                    # Activation is not committed until health stabilization
                    # succeeds. If DUMB exited anywhere in that window, prefer
                    # the known-good previous runtime on the next attempt.
                    if target.exists():
                        failed = target.parent / (
                            f".{target.name}.dumb-recovery-failed-{uuid.uuid4().hex}"
                        )
                        os.replace(target, failed)
                        if failed.is_dir() and not failed.is_symlink():
                            shutil.rmtree(failed, ignore_errors=True)
                        else:
                            failed.unlink(missing_ok=True)
                    os.replace(previous, target)
                    recovered.append("rolled_back_uncommitted_activation")
                if state in {"prepared", "building", "failed"} and candidate.exists():
                    shutil.rmtree(candidate)
                    recovered.append("removed_candidate")
                if target.exists() and previous.exists():
                    shutil.rmtree(previous)
                if candidate.exists():
                    shutil.rmtree(candidate, ignore_errors=True)
                if previous_staging and previous_staging.is_dir():
                    shutil.rmtree(previous_staging, ignore_errors=True)
                journal.unlink(missing_ok=True)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                logger.warning(
                    "Unable to recover install transaction %s: %s", journal, error
                )
        return recovered

    def prepare(self) -> str:
        self.recover_incomplete(str(self.target))
        self.target.parent.mkdir(parents=True, exist_ok=True)
        if self.candidate.exists():
            shutil.rmtree(self.candidate)
        self.candidate.mkdir()
        self._write_journal("prepared")
        return str(self.candidate)

    def mark_building(self) -> None:
        self._write_journal("building")

    def _activate_overlay_fallback(self) -> None:
        """Replace a lower/merged overlay directory with a writable candidate."""
        self._write_journal("copying_previous")
        if self.previous_staging.exists():
            shutil.rmtree(self.previous_staging)
        shutil.copytree(
            self.target,
            self.previous_staging,
            symlinks=True,
            copy_function=_reflink_or_copy,
        )
        os.replace(self.previous_staging, self.previous)
        self._write_journal("replacing_overlay_target")
        shutil.rmtree(self.target)
        os.replace(self.candidate, self.target)

    def activate(self) -> None:
        if not self.candidate.is_dir():
            raise TransactionError("install candidate does not exist")
        self._write_journal("activating")
        target_existed = self.target.exists()
        try:
            if target_existed:
                try:
                    os.replace(self.target, self.previous)
                except OSError as error:
                    if error.errno != errno.EXDEV:
                        raise
                    logger.info(
                        "Using overlay-safe candidate activation for %s at %s.",
                        self.process_name,
                        self.target,
                    )
                    self._activate_overlay_fallback()
                else:
                    os.replace(self.candidate, self.target)
            else:
                os.replace(self.candidate, self.target)
            self.activated = True
            self._write_journal("activated")
        except OSError as error:
            if self.previous.exists():
                if self.target.exists():
                    if self.target.is_dir() and not self.target.is_symlink():
                        shutil.rmtree(self.target)
                    else:
                        self.target.unlink(missing_ok=True)
                os.replace(self.previous, self.target)
            if self.previous_staging.is_dir():
                shutil.rmtree(self.previous_staging, ignore_errors=True)
            self._write_journal("failed", str(error))
            raise TransactionError(
                f"failed activating install candidate: {error}"
            ) from error

    def rollback(self) -> bool:
        self._write_journal("rolling_back")
        try:
            if self.activated and self.target.exists():
                failed = self.target.parent / (
                    f".{self.target.name}.dumb-failed-{self.identifier}"
                )
                os.replace(self.target, failed)
                shutil.rmtree(failed, ignore_errors=True)
            if self.previous.exists():
                os.replace(self.previous, self.target)
            if self.candidate.exists():
                shutil.rmtree(self.candidate, ignore_errors=True)
            self.activated = False
            self._write_journal("rolled_back")
            self.journal.unlink(missing_ok=True)
            return True
        except OSError as error:
            self._write_journal("rollback_failed", str(error))
            logger.error("Install rollback failed for %s: %s", self.process_name, error)
            return False

    def commit(self, keep_previous: bool = False) -> None:
        self._write_journal("committing")
        if self.candidate.exists():
            shutil.rmtree(self.candidate, ignore_errors=True)
        if self.previous_staging.exists():
            shutil.rmtree(self.previous_staging, ignore_errors=True)
        if self.previous.exists() and not keep_previous:
            shutil.rmtree(self.previous)
        self._write_journal("committed")
        self.journal.unlink(missing_ok=True)

    def abandon(self, message: str = "") -> None:
        if self.activated:
            self.rollback()
            return
        if self.candidate.exists():
            shutil.rmtree(self.candidate, ignore_errors=True)
        if self.previous_staging.exists():
            shutil.rmtree(self.previous_staging, ignore_errors=True)
        self._write_journal("failed", message)
        self.journal.unlink(missing_ok=True)


class RuntimeRollbackSnapshot:
    """Best-effort rollback protection for installers not yet candidate-native.

    Persistent paths are left in place. Only replaceable runtime/source files
    are copied and restored. This contains failures without pretending an
    application-owned database migration can always be reversed safely.
    """

    def __init__(
        self,
        target_dir: str,
        process_name: str,
        persistent_paths: list[str] | None = None,
    ):
        self.target = Path(target_dir).resolve()
        self.process_name = process_name
        self.identifier = uuid.uuid4().hex
        self.snapshot = self.target.parent / (
            f".{self.target.name}.dumb-rollback-{self.identifier}"
        )
        self.persistent = self._normalize_persistent(persistent_paths or [])

    def _normalize_persistent(self, values: list[str]) -> set[str]:
        normalized = set()
        for value in values:
            candidate = Path(value)
            if candidate.is_absolute():
                try:
                    relative = candidate.resolve().relative_to(self.target)
                except ValueError:
                    continue
            else:
                relative = candidate
            text = str(relative).strip("/")
            if text and text not in {".", ".."} and ".." not in relative.parts:
                normalized.add(text)
        return normalized

    def _is_persistent(self, relative: str) -> bool:
        normalized = str(relative).strip("/")
        return any(
            normalized == value or normalized.startswith(f"{value}/")
            for value in self.persistent
        )

    def capture(self) -> bool:
        if not self.target.is_dir():
            return False
        if self.snapshot.exists():
            shutil.rmtree(self.snapshot)

        def ignore(directory, names):
            relative_root = Path(directory).resolve().relative_to(self.target)
            ignored = []
            for name in names:
                relative = str(relative_root / name)
                if self._is_persistent(relative):
                    ignored.append(name)
            return ignored

        shutil.copytree(
            self.target,
            self.snapshot,
            symlinks=True,
            ignore=ignore,
            copy_function=_reflink_or_copy,
        )
        return True

    def rollback(self) -> bool:
        if not self.snapshot.is_dir():
            return False
        try:
            self.target.mkdir(parents=True, exist_ok=True)
            for entry in list(self.target.iterdir()):
                relative = entry.name
                if self._is_persistent(relative):
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
            shutil.copytree(
                self.snapshot,
                self.target,
                dirs_exist_ok=True,
                symlinks=True,
                copy_function=_reflink_or_copy,
            )
            return True
        except OSError as error:
            logger.error(
                "Runtime rollback snapshot restore failed for %s: %s",
                self.process_name,
                error,
            )
            return False

    def commit(self) -> None:
        shutil.rmtree(self.snapshot, ignore_errors=True)


class DeferredClearTransaction:
    """Move cleared runtime files aside until an install phase succeeds."""

    def __init__(self, target_dir: str, excluded_paths: list[str] | None = None):
        self.target = Path(target_dir).resolve()
        self.identifier = uuid.uuid4().hex
        self.backup = self.target.parent / (
            f".{self.target.name}.dumb-clear-{self.identifier}"
        )
        self.excluded = self._normalize_excluded(excluded_paths or [])
        self.captured = False
        self.capture_complete = False

    def _normalize_excluded(self, values: list[str]) -> set[str]:
        normalized = set()
        for value in values:
            candidate = Path(value)
            if candidate.is_absolute():
                try:
                    relative = candidate.resolve().relative_to(self.target)
                except ValueError:
                    continue
            else:
                relative = candidate
            text = str(relative).strip("/")
            if text and text not in {".", ".."} and ".." not in relative.parts:
                normalized.add(text)
        return normalized

    def _is_excluded(self, relative: str) -> bool:
        normalized = str(relative).strip("/")
        return any(
            normalized == value or normalized.startswith(f"{value}/")
            for value in self.excluded
        )

    def capture(self) -> None:
        if not self.target.is_dir():
            raise OSError(f"Directory {self.target} does not exist")
        self.backup.mkdir(parents=True)
        # Mark the transaction recoverable before the first rename. A later
        # ENOSPC/permission/rename failure can otherwise strand files in the
        # backup while the caller believes nothing was captured.
        self.captured = True
        for current_root, directories, filenames in os.walk(
            self.target, topdown=False, followlinks=False
        ):
            current = Path(current_root)
            for filename in filenames:
                source = current / filename
                relative = str(source.relative_to(self.target))
                if self._is_excluded(relative):
                    continue
                destination = self.backup / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, destination)
            for directory in directories:
                source = current / directory
                relative = str(source.relative_to(self.target))
                if self._is_excluded(relative):
                    continue
                if source.is_symlink():
                    destination = self.backup / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(source, destination)
                else:
                    try:
                        source.rmdir()
                    except OSError:
                        pass
        self.capture_complete = True

    def rollback(self) -> bool:
        if not self.captured:
            return True
        try:
            if self.capture_complete:
                for current_root, directories, filenames in os.walk(
                    self.target, topdown=False, followlinks=False
                ):
                    current = Path(current_root)
                    for filename in filenames:
                        path = current / filename
                        relative = str(path.relative_to(self.target))
                        if not self._is_excluded(relative):
                            path.unlink(missing_ok=True)
                    for directory in directories:
                        path = current / directory
                        relative = str(path.relative_to(self.target))
                        if self._is_excluded(relative):
                            continue
                        if path.is_symlink():
                            path.unlink(missing_ok=True)
                        else:
                            try:
                                path.rmdir()
                            except OSError:
                                pass
            shutil.copytree(
                self.backup,
                self.target,
                dirs_exist_ok=True,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            shutil.rmtree(self.backup)
            self.captured = False
            self.capture_complete = False
            return True
        except OSError as error:
            logger.error("Deferred directory clear rollback failed: %s", error)
            return False

    def commit(self) -> None:
        shutil.rmtree(self.backup, ignore_errors=True)
        self.captured = False
        self.capture_complete = False
