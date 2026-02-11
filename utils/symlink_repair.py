from __future__ import annotations
from utils.config_loader import CONFIG_MANAGER
from utils.global_logger import logger
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable
import json, os


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
        "/mnt/debrid/nzbdav-symlinks",
        "/mnt/debrid/combined_symlinks",
        "/mnt/debrid/clid_symlinks",
    ]
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
        for current_root, dirs, files in os.walk(root, followlinks=False):
            for name in dirs + files:
                full_path = os.path.join(current_root, name)
                if os.path.islink(full_path):
                    paths.append(full_path)
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


def backup_symlink_manifest(
    roots: list[str] | None,
    backup_path: str,
    include_broken: bool = True,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    destination = (backup_path or "").strip()
    if not destination:
        raise ValueError("backup_path is required.")

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
            target_exists = _target_exists(link_path, target)
            if not include_broken and not target_exists:
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
        "entries": entries,
    }
    _ensure_parent_dir(destination)
    with open(destination, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)

    report = {
        "backup_manifest": destination,
        "roots": resolved_roots,
        "missing_roots": missing_roots,
        "scanned_symlinks": len(symlink_paths),
        "recorded_entries": len(entries),
        "skipped_broken": skipped_broken,
        "errors": errors,
    }
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
) -> dict[str, Any]:
    source = (manifest_path or "").strip()
    if not source:
        raise ValueError("manifest_path is required.")
    if not os.path.exists(source):
        raise ValueError(f"Manifest does not exist: {source}")

    with open(source, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid manifest format: entries list is required.")

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
) -> dict[str, Any]:
    source = (manifest_path or "").strip()
    if not source:
        raise ValueError("manifest_path is required.")
    if not os.path.exists(source):
        raise ValueError(f"Manifest does not exist: {source}")

    with open(source, "r", encoding="utf-8") as handle:
        manifest = json.load(handle)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Invalid manifest format: entries list is required.")

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
