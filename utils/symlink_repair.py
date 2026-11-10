from __future__ import annotations
from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
import hashlib
import json, os
from pathlib import Path
import re

from utils.backup_storage import get_backup_storage, is_backup_reference
from utils.private_files import atomic_write_private_text


@dataclass(frozen=True)
class RewriteRule:
    from_prefix: str
    to_prefix: str


@dataclass(frozen=True)
class RootMigration:
    from_root: str
    to_root: str


def _normalize_prefix(value: str) -> str:
    return (value or "").rstrip("/")


def _target_exists(link_path: str, target: str) -> bool:
    if os.path.isabs(target):
        return os.path.exists(target)
    return os.path.exists(
        os.path.normpath(os.path.join(os.path.dirname(link_path), target))
    )


def _rewrite_target(
    target: str, rules: list[RewriteRule]
) -> tuple[str, RewriteRule | None]:
    for rule in rules:
        src = _normalize_prefix(rule.from_prefix)
        dst = _normalize_prefix(rule.to_prefix)
        if not src:
            continue
        if target == src:
            return dst, rule
        if target.startswith(f"{src}/"):
            suffix = target[len(src) :]
            return f"{dst}{suffix}", rule
    return target, None


def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def default_symlink_roots() -> list[str]:
    roots = [
        "/mnt/debrid/decypharr_symlinks",
        "/mnt/debrid/infinidysk-symlinks",
        "/mnt/debrid/combined_symlinks",
        "/mnt/debrid/clid_symlinks",
    ]
    infinidysk_cfg = CONFIG_MANAGER.get("infinidysk") or {}
    configured_infinidysk_roots = infinidysk_cfg.get("symlink_backup_roots") or []
    roots.extend(configured_infinidysk_roots)
    riven_cfg = CONFIG_MANAGER.get("riven_backend") or {}
    riven_root = (riven_cfg.get("symlink_library_path") or "").strip()
    if riven_root:
        roots.append(riven_root)

    deduped = []
    seen = set()
    for root in roots:
        root = (root or "").strip()
        if not root or root in seen:
            continue
        seen.add(root)
        deduped.append(root)
    return deduped


def preset_rewrite_rules(presets: list[str] | None) -> list[RewriteRule]:
    if not presets:
        return []
    # Presets capture known path migrations that have occurred in DUMB workflows.
    known = {
        "decypharr_beta_consolidated": [
            RewriteRule(
                "/mnt/debrid/decypharr/realdebrid/__all__",
                "/mnt/debrid/decypharr/__all__",
            ),
        ]
    }
    result: list[RewriteRule] = []
    for preset in presets:
        result.extend(known.get((preset or "").strip(), []))
    return result


def _collect_symlink_paths(roots: list[str]) -> tuple[list[str], list[str]]:
    paths: list[str] = []
    missing_roots: list[str] = []
    for root in roots:
        if not os.path.exists(root):
            missing_roots.append(root)
            continue
        pending = [root]
        while pending:
            current_root = pending.pop()
            with os.scandir(current_root) as iterator:
                entries = sorted(iterator, key=lambda item: item.name, reverse=True)
            for entry in entries:
                if entry.is_symlink():
                    paths.append(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(entry.path)
    return paths, missing_roots


def _collect_root_migration_moves(
    migrations: list[RootMigration],
) -> tuple[list[dict[str, str]], list[str]]:
    moves: list[dict[str, str]] = []
    missing_from_roots: list[str] = []
    for migration in migrations:
        src_root = _normalize_prefix(migration.from_root)
        dst_root = _normalize_prefix(migration.to_root)
        if not src_root or not dst_root:
            continue
        if not os.path.exists(src_root):
            missing_from_roots.append(src_root)
            continue
        for current_root, dirs, files in os.walk(src_root, followlinks=False):
            for name in dirs + files:
                src_path = os.path.join(current_root, name)
                if not os.path.islink(src_path):
                    continue
                rel_path = os.path.relpath(src_path, src_root)
                dst_path = os.path.join(dst_root, rel_path)
                moves.append(
                    {
                        "from_root": src_root,
                        "to_root": dst_root,
                        "source_path": src_path,
                        "target_path": dst_path,
                    }
                )
    return moves, missing_from_roots


def repair_symlinks(
    roots: list[str] | None,
    rewrite_rules: list[dict[str, Any]] | None,
    dry_run: bool = True,
    include_broken: bool = True,
    backup_path: str | None = None,
    presets: list[str] | None = None,
    root_migrations: list[dict[str, Any]] | None = None,
    overwrite_existing: bool = False,
    copy_instead_of_move: bool = False,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    resolved_roots = roots or default_symlink_roots()
    rules = preset_rewrite_rules(presets)
    migrations: list[RootMigration] = []
    for migration in root_migrations or []:
        src_root = (migration.get("from_root") or "").strip()
        dst_root = (migration.get("to_root") or "").strip()
        if src_root and dst_root:
            migrations.append(RootMigration(src_root, dst_root))
    for rule in rewrite_rules or []:
        src = (rule.get("from_prefix") or "").strip()
        dst = (rule.get("to_prefix") or "").strip()
        if src and dst:
            rules.append(RewriteRule(src, dst))

    if not rules and not migrations:
        raise ValueError(
            "At least one rewrite rule, preset, or root migration is required."
        )

    symlink_paths, missing_roots = _collect_symlink_paths(resolved_roots)
    root_moves, missing_migration_roots = _collect_root_migration_moves(migrations)
    total_items = len(symlink_paths) + len(root_moves)
    report: dict[str, Any] = {
        "dry_run": dry_run,
        "roots": resolved_roots,
        "missing_roots": missing_roots,
        "root_migrations": [
            {"from_root": migration.from_root, "to_root": migration.to_root}
            for migration in migrations
        ],
        "missing_migration_roots": missing_migration_roots,
        "scanned_symlinks": len(symlink_paths),
        "rules": [
            {"from_prefix": rule.from_prefix, "to_prefix": rule.to_prefix}
            for rule in rules
        ],
        "changed": 0,
        "moved": 0,
        "copied": 0,
        "skipped_unchanged": 0,
        "skipped_nonexistent_target": 0,
        "skipped_existing_destination": 0,
        "errors": [],
        "changes": [],
        "moves": [],
        "backup_manifest": None,
    }

    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "processing",
                    "processed_items": 0,
                    "total_items": total_items,
                    "changed": 0,
                    "moved": 0,
                    "copied": 0,
                    "errors": 0,
                }
            )
        except Exception:
            pass

    changes_for_backup: list[dict[str, str]] = []
    processed_items = 0
    for link_path in symlink_paths:
        try:
            old_target = os.readlink(link_path)
            new_target, matched_rule = _rewrite_target(old_target, rules)
            if not matched_rule or old_target == new_target:
                report["skipped_unchanged"] += 1
                continue
            if not include_broken and not _target_exists(link_path, old_target):
                report["skipped_nonexistent_target"] += 1
                continue

            change = {
                "link_path": link_path,
                "old_target": old_target,
                "new_target": new_target,
                "matched_rule": {
                    "from_prefix": matched_rule.from_prefix,
                    "to_prefix": matched_rule.to_prefix,
                },
            }
            report["changes"].append(change)
            changes_for_backup.append(
                {
                    "link_path": link_path,
                    "old_target": old_target,
                    "new_target": new_target,
                }
            )
            if not dry_run:
                os.unlink(link_path)
                os.symlink(new_target, link_path)
            report["changed"] += 1
        except Exception as e:
            report["errors"].append({"link_path": link_path, "error": str(e)})
        finally:
            processed_items += 1
            if progress_callback and (
                processed_items % 2000 == 0 or processed_items == total_items
            ):
                try:
                    progress_callback(
                        {
                            "stage": "processing",
                            "processed_items": processed_items,
                            "total_items": total_items,
                            "changed": report["changed"],
                            "moved": report["moved"],
                            "copied": report["copied"],
                            "errors": len(report["errors"]),
                        }
                    )
                except Exception:
                    pass

    # Root migration moves symlink entries between root trees (e.g., individual -> combined)
    for move in root_moves:
        src_path = move["source_path"]
        dst_path = move["target_path"]
        try:
            if not os.path.lexists(src_path) or not os.path.islink(src_path):
                continue
            if os.path.lexists(dst_path):
                if not overwrite_existing:
                    report["skipped_existing_destination"] += 1
                    continue
                if os.path.isdir(dst_path) and not os.path.islink(dst_path):
                    report["errors"].append(
                        {
                            "source_path": src_path,
                            "target_path": dst_path,
                            "error": "Destination exists as a real directory.",
                        }
                    )
                    continue
                if not dry_run:
                    os.unlink(dst_path)
            operation = "copy" if copy_instead_of_move else "move"
            move_record = {
                **move,
                "operation": operation,
            }
            report["moves"].append(move_record)
            link_target = os.readlink(src_path)
            changes_for_backup.append(
                {
                    "source_path": src_path,
                    "target_path": dst_path,
                    "old_target": link_target,
                    "operation": operation,
                }
            )
            if not dry_run:
                os.makedirs(os.path.dirname(dst_path), exist_ok=True)
                if copy_instead_of_move:
                    os.symlink(link_target, dst_path)
                else:
                    os.rename(src_path, dst_path)
            if copy_instead_of_move:
                report["copied"] += 1
            else:
                report["moved"] += 1
        except Exception as e:
            report["errors"].append(
                {
                    "source_path": src_path,
                    "target_path": dst_path,
                    "error": str(e),
                }
            )
        finally:
            processed_items += 1
            if progress_callback and (
                processed_items % 2000 == 0 or processed_items == total_items
            ):
                try:
                    progress_callback(
                        {
                            "stage": "processing",
                            "processed_items": processed_items,
                            "total_items": total_items,
                            "changed": report["changed"],
                            "moved": report["moved"],
                            "copied": report["copied"],
                            "errors": len(report["errors"]),
                        }
                    )
                except Exception:
                    pass

    if not dry_run and backup_path and changes_for_backup:
        _ensure_parent_dir(backup_path)
        manifest = {
            "created_at": datetime.utcnow().isoformat() + "Z",
            "roots": resolved_roots,
            "changes": changes_for_backup,
        }
        with open(backup_path, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, sort_keys=True)
        report["backup_manifest"] = backup_path

    logger.info(
        "Symlink repair completed: dry_run=%s scanned=%s changed=%s moved=%s copied=%s errors=%s",
        dry_run,
        report["scanned_symlinks"],
        report["changed"],
        report["moved"],
        report["copied"],
        len(report["errors"]),
    )
    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "completed",
                    "processed_items": total_items,
                    "total_items": total_items,
                    "changed": report["changed"],
                    "moved": report["moved"],
                    "copied": report["copied"],
                    "errors": len(report["errors"]),
                }
            )
        except Exception:
            pass
    return report


def _symlink_backup_identity(process_name: str | None = None) -> str:
    identity = str(process_name or "symlink-manifest")
    return identity if identity.strip() else "symlink-manifest"


def symlink_backup_namespace(process_name: str | None = None) -> str:
    identity = _symlink_backup_identity(process_name)
    slug = re.sub(r"[^a-z0-9_-]+", "-", identity.lower()).strip("-_")[:55] or "service"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"symlink-{slug}-{suffix}"


def _managed_symlink_reference(reference: str) -> tuple[str, str]:
    if not is_backup_reference(reference):
        raise ValueError("A registered symlink backup reference is required.")
    parts = reference.removeprefix("dumb-backup://").split("/")
    if len(parts) != 3 or not re.fullmatch(r"symlink-[a-z0-9_-]{1,72}", parts[1]):
        raise ValueError("The reference does not identify a symlink backup.")
    return parts[0], parts[1]


def read_symlink_manifest(
    manifest_path: str,
    *,
    process_name: str | None = None,
    backup_destination_id: str | None = None,
) -> dict[str, Any]:
    source = str(manifest_path or "").strip()
    if not source:
        raise ValueError("manifest_path is required.")
    if is_backup_reference(source):
        profile, namespace = _managed_symlink_reference(source)
        storage = get_backup_storage(CONFIG_MANAGER)
        if process_name is not None and namespace != symlink_backup_namespace(
            process_name
        ):
            raise ValueError("The backup belongs to a different service process.")
        if backup_destination_id:
            selected = storage.bound_identity(backup_destination_id)["destination_id"]
            if profile != selected:
                raise ValueError(
                    "The backup belongs to a different selected destination."
                )
        manifest = storage.read_json(source, expected_kind="symlink_manifest")
        if (
            not isinstance(manifest, dict)
            or manifest.get("manifest_type") != "symlink_snapshot"
        ):
            raise ValueError("The registered artifact is not a symlink snapshot.")
        recorded_process = manifest.get("process_name")
        if recorded_process is not None and namespace != symlink_backup_namespace(
            recorded_process
        ):
            raise ValueError(
                "The backup process identity does not match its registered namespace."
            )
    else:
        if not os.path.exists(source):
            raise ValueError(f"Manifest does not exist: {source}")
        with open(source, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entries"), list):
        raise ValueError("Invalid manifest format: entries list is required.")
    return manifest


def list_symlink_backup_manifests(
    backup_destination_id: str | None = None,
    process_name: str | None = None,
    *,
    reference: str | None = None,
    scheduled_only: bool = False,
) -> dict[str, Any]:
    storage = get_backup_storage(CONFIG_MANAGER)
    if reference:
        profile_id, namespace = _managed_symlink_reference(reference)
        read_symlink_manifest(
            reference,
            process_name=process_name,
            backup_destination_id=backup_destination_id,
        )
    else:
        profile_id = backup_destination_id
        namespace = symlink_backup_namespace(process_name)
    if not profile_id:
        raise ValueError("Select a registered backup destination.")
    if profile_id == "@default":
        profile_id = storage.bound_identity(profile_id)["destination_id"]
    receipts = storage.list_artifacts(profile_id, namespace)
    rows = [
        {
            **receipt,
            "path": receipt["reference"],
            "name": receipt["artifact_id"] + ".json",
            "modified_at": receipt["created_at"],
        }
        for receipt in receipts
        if receipt.get("artifact_kind") == "symlink_manifest"
        and (not scheduled_only or receipt.get("retention_scope") == "scheduled")
    ]
    rows.sort(key=lambda item: (item["modified_at"], item["name"]), reverse=True)
    directory = f"dumb-backup://{profile_id}/{namespace}"
    return {
        "directory": directory,
        "manifest_path": reference or "",
        "files": rows[:500],
        "count": len(rows),
    }


def prune_symlink_backup_manifests(
    backup_destination_id: str,
    process_name: str,
    retention_count: int,
    *,
    latest_reference: str | None = None,
) -> dict[str, Any]:
    keep_count = max(0, int(retention_count))
    if keep_count <= 0:
        return {"pruned": 0, "kept": 0, "errors": []}
    storage = get_backup_storage(CONFIG_MANAGER)
    namespace = symlink_backup_namespace(process_name)
    profile_id = backup_destination_id
    if latest_reference:
        written_profile, written_namespace = _managed_symlink_reference(
            latest_reference
        )
        if written_namespace != namespace or (
            profile_id != "@default" and profile_id != written_profile
        ):
            raise ValueError(
                "The new backup does not match this retention destination."
            )
        profile_id = written_profile
    elif profile_id == "@default":
        profile_id = storage.bound_identity(profile_id)["destination_id"]
    receipts = [
        receipt
        for receipt in storage.list_artifacts(profile_id, namespace)
        if receipt.get("artifact_kind") == "symlink_manifest"
        and receipt.get("retention_scope") == "scheduled"
        and _managed_symlink_reference(receipt["reference"]) == (profile_id, namespace)
    ]
    if latest_reference and not any(
        receipt["reference"] == latest_reference for receipt in receipts
    ):
        raise ValueError(
            "The new verified backup receipt is missing; retention stopped."
        )
    receipts.sort(
        key=lambda item: (
            item["reference"] == latest_reference,
            item["created_at"],
            item["artifact_id"],
        ),
        reverse=True,
    )
    errors = []
    pruned = 0
    for receipt in receipts[keep_count:]:
        try:
            storage.delete_artifact(receipt["reference"])
            pruned += 1
        except Exception as error:
            errors.append({"reference": receipt["reference"], "error": str(error)})
    return {"pruned": pruned, "kept": min(len(receipts), keep_count), "errors": errors}


def backup_symlink_manifest(
    roots: list[str] | None,
    backup_path: str | None = None,
    include_broken: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    check_targets: bool = True,
    *,
    backup_destination_id: str | None = None,
    process_name: str | None = None,
    scheduled: bool = False,
) -> dict[str, Any]:
    destination = (backup_path or "").strip()
    if not destination and not backup_destination_id:
        raise ValueError("backup_path or a registered backup destination is required.")

    resolved_roots = roots or default_symlink_roots()
    symlink_paths, missing_roots = _collect_symlink_paths(resolved_roots)
    total_symlinks = len(symlink_paths)

    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "processing",
                    "processed_symlinks": 0,
                    "total_symlinks": total_symlinks,
                    "recorded_entries": 0,
                    "errors": 0,
                }
            )
        except Exception:
            pass
    entries: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skipped_broken = 0

    processed_symlinks = 0
    for link_path in symlink_paths:
        try:
            target = os.readlink(link_path)
            target_exists = _target_exists(link_path, target) if check_targets else None
            if check_targets and not include_broken and not target_exists:
                skipped_broken += 1
                continue
            entries.append(
                {
                    "link_path": link_path,
                    "target": target,
                    "target_exists": target_exists,
                }
            )
        except Exception as e:
            errors.append({"link_path": link_path, "error": str(e)})
        finally:
            processed_symlinks += 1
            if progress_callback and (
                processed_symlinks % 2000 == 0 or processed_symlinks == total_symlinks
            ):
                try:
                    progress_callback(
                        {
                            "stage": "processing",
                            "processed_symlinks": processed_symlinks,
                            "total_symlinks": total_symlinks,
                            "recorded_entries": len(entries),
                            "errors": len(errors),
                        }
                    )
                except Exception:
                    pass

    manifest = {
        "manifest_type": "symlink_snapshot",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "roots": resolved_roots,
        "include_broken": include_broken,
        "targets_checked": check_targets,
        "entries": entries,
    }
    receipt = None
    if backup_destination_id:
        identity = _symlink_backup_identity(process_name)
        manifest["process_name"] = identity
        scope = "scheduled" if scheduled is True else "manual"
        manifest["retention_scope"] = scope
        receipt = get_backup_storage(CONFIG_MANAGER).write_json(
            backup_destination_id,
            symlink_backup_namespace(identity),
            manifest,
            metadata={
                "kind": "symlink_manifest",
                "process_name": identity,
                "retention_scope": scope,
            },
        )
        destination = receipt["reference"]
        digest = receipt["sha256"]
        size_bytes = receipt["size_bytes"]
    else:
        content = json.dumps(manifest, indent=2, sort_keys=True)
        atomic_write_private_text(destination, content)
        expected = content.encode("utf-8")
        actual = Path(destination).read_bytes()
        if actual != expected:
            raise ValueError("The symlink backup did not pass readback verification.")
        digest = hashlib.sha256(actual).hexdigest()
        size_bytes = len(actual)

    report = {
        "sha256": digest,
        "size_bytes": size_bytes,
        "verified": True,
        "backup_manifest": destination,
        "roots": resolved_roots,
        "missing_roots": missing_roots,
        "scanned_symlinks": len(symlink_paths),
        "recorded_entries": len(entries),
        "skipped_broken": skipped_broken,
        "errors": errors,
    }
    if receipt is not None:
        report["storage_receipt"] = receipt
    logger.info(
        "Symlink manifest backup completed: roots=%s scanned=%s recorded=%s skipped_broken=%s errors=%s path=%s",
        len(resolved_roots),
        report["scanned_symlinks"],
        report["recorded_entries"],
        report["skipped_broken"],
        len(errors),
        destination,
    )
    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "completed",
                    "processed_symlinks": report["scanned_symlinks"],
                    "total_symlinks": report["scanned_symlinks"],
                    "recorded_entries": report["recorded_entries"],
                    "errors": len(errors),
                }
            )
        except Exception:
            pass
    return report


def restore_symlink_manifest(
    manifest_path: str,
    dry_run: bool = True,
    overwrite_existing: bool = False,
    restore_broken: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    *,
    process_name: str | None = None,
    backup_destination_id: str | None = None,
) -> dict[str, Any]:
    source = (manifest_path or "").strip()
    if not source:
        raise ValueError("manifest_path is required.")
    manifest = read_symlink_manifest(
        source,
        process_name=process_name,
        backup_destination_id=backup_destination_id,
    )
    entries = manifest["entries"]

    report = {
        "manifest_path": source,
        "manifest_created_at": manifest.get("created_at"),
        "dry_run": dry_run,
        "overwrite_existing": overwrite_existing,
        "restore_broken": restore_broken,
        "total_entries": len(entries),
        "restored": 0,
        "skipped_existing": 0,
        "skipped_unchanged": 0,
        "skipped_invalid_entries": 0,
        "skipped_nonexistent_target": 0,
        "errors": [],
    }

    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "processing",
                    "processed_entries": 0,
                    "total_entries": report["total_entries"],
                    "restored": 0,
                    "errors": 0,
                }
            )
        except Exception:
            pass

    processed_entries = 0
    for entry in entries:
        link_path = (
            (entry.get("link_path") or "").strip() if isinstance(entry, dict) else ""
        )
        target = (entry.get("target") or "").strip() if isinstance(entry, dict) else ""
        if not link_path or not target:
            report["skipped_invalid_entries"] += 1
            continue

        try:
            if not restore_broken and not _target_exists(link_path, target):
                report["skipped_nonexistent_target"] += 1
                continue

            if os.path.lexists(link_path):
                if os.path.islink(link_path):
                    current_target = os.readlink(link_path)
                    if current_target == target:
                        report["skipped_unchanged"] += 1
                        continue
                if not overwrite_existing:
                    report["skipped_existing"] += 1
                    continue
                if os.path.isdir(link_path) and not os.path.islink(link_path):
                    report["errors"].append(
                        {
                            "link_path": link_path,
                            "error": "Existing path is a real directory; cannot overwrite.",
                        }
                    )
                    continue
                if not dry_run:
                    os.unlink(link_path)

            if not dry_run:
                _ensure_parent_dir(link_path)
                os.symlink(target, link_path)
            report["restored"] += 1
        except Exception as e:
            report["errors"].append({"link_path": link_path, "error": str(e)})
        finally:
            processed_entries += 1
            if progress_callback and (
                processed_entries % 2000 == 0
                or processed_entries == report["total_entries"]
            ):
                try:
                    progress_callback(
                        {
                            "stage": "processing",
                            "processed_entries": processed_entries,
                            "total_entries": report["total_entries"],
                            "restored": report["restored"],
                            "errors": len(report["errors"]),
                        }
                    )
                except Exception:
                    pass

    logger.info(
        "Symlink manifest restore completed: dry_run=%s entries=%s restored=%s errors=%s source=%s",
        dry_run,
        report["total_entries"],
        report["restored"],
        len(report["errors"]),
        source,
    )
    if progress_callback:
        try:
            progress_callback(
                {
                    "stage": "completed",
                    "processed_entries": report["total_entries"],
                    "total_entries": report["total_entries"],
                    "restored": report["restored"],
                    "errors": len(report["errors"]),
                }
            )
        except Exception:
            pass
    return report


def preview_symlink_manifest_restore(
    manifest_path: str,
    overwrite_existing: bool = False,
    restore_broken: bool = True,
    sample_limit: int = 50,
    *,
    process_name: str | None = None,
    backup_destination_id: str | None = None,
) -> dict[str, Any]:
    source = (manifest_path or "").strip()
    if not source:
        raise ValueError("manifest_path is required.")
    manifest = read_symlink_manifest(
        source,
        process_name=process_name,
        backup_destination_id=backup_destination_id,
    )
    entries = manifest["entries"]

    report = {
        "manifest_path": source,
        "manifest_created_at": manifest.get("created_at"),
        "overwrite_existing": bool(overwrite_existing),
        "restore_broken": bool(restore_broken),
        "sample_limit": int(sample_limit),
        "total_entries": len(entries),
        "projected_restored": 0,
        "projected_skipped_existing": 0,
        "projected_skipped_unchanged": 0,
        "projected_skipped_invalid_entries": 0,
        "projected_skipped_nonexistent_target": 0,
        "projected_errors": 0,
        "sample_changes": [],
        "errors": [],
    }

    normalized_sample_limit = max(0, int(sample_limit))
    for entry in entries:
        link_path = (
            (entry.get("link_path") or "").strip() if isinstance(entry, dict) else ""
        )
        target = (entry.get("target") or "").strip() if isinstance(entry, dict) else ""
        if not link_path or not target:
            report["projected_skipped_invalid_entries"] += 1
            continue

        try:
            target_exists = _target_exists(link_path, target)
            if not restore_broken and not target_exists:
                report["projected_skipped_nonexistent_target"] += 1
                if len(report["sample_changes"]) < normalized_sample_limit:
                    report["sample_changes"].append(
                        {
                            "action": "skip_missing_target",
                            "link_path": link_path,
                            "target": target,
                        }
                    )
                continue

            if os.path.lexists(link_path):
                if os.path.islink(link_path):
                    current_target = os.readlink(link_path)
                    if current_target == target:
                        report["projected_skipped_unchanged"] += 1
                        if len(report["sample_changes"]) < normalized_sample_limit:
                            report["sample_changes"].append(
                                {
                                    "action": "skip_unchanged",
                                    "link_path": link_path,
                                    "target": target,
                                }
                            )
                        continue
                else:
                    current_target = None

                if not overwrite_existing:
                    report["projected_skipped_existing"] += 1
                    if len(report["sample_changes"]) < normalized_sample_limit:
                        report["sample_changes"].append(
                            {
                                "action": "skip_existing",
                                "link_path": link_path,
                                "target": target,
                                "current_target": current_target,
                            }
                        )
                    continue

                report["projected_restored"] += 1
                if len(report["sample_changes"]) < normalized_sample_limit:
                    report["sample_changes"].append(
                        {
                            "action": "overwrite",
                            "link_path": link_path,
                            "target": target,
                            "current_target": current_target,
                        }
                    )
                continue

            report["projected_restored"] += 1
            if len(report["sample_changes"]) < normalized_sample_limit:
                report["sample_changes"].append(
                    {
                        "action": "create",
                        "link_path": link_path,
                        "target": target,
                    }
                )
        except Exception as e:
            report["projected_errors"] += 1
            report["errors"].append({"link_path": link_path, "error": str(e)})

    logger.info(
        "Symlink manifest preview completed: entries=%s projected_restored=%s skipped_existing=%s skipped_unchanged=%s skipped_missing_target=%s errors=%s source=%s",
        report["total_entries"],
        report["projected_restored"],
        report["projected_skipped_existing"],
        report["projected_skipped_unchanged"],
        report["projected_skipped_nonexistent_target"],
        report["projected_errors"],
        source,
    )
    return report
