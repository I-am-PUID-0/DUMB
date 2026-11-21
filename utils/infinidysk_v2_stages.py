"""Presentation-only migration checkpoints; never used to authorize execution."""

import copy
import time

_STAGES = {
    "revalidating": "check_plan",
    "quiescing": "pause_services",
    "postgres_snapshot": "source_database",
    "snapshotting": "save_recovery",
    "archive_inventory": "save_recovery",
    "archiving": "save_recovery",
    "verifying_archive": "save_recovery",
    "preparing_blob_staging": "prepare_input",
    "staging_blobs": "prepare_input",
    "verifying_blob_staging": "prepare_input",
    "releasing_local_blobs": "prepare_input",
    "verifying_blob_input": "prepare_input",
    "preparing_queue_input": "prepare_input",
    "preparing_native_database": "prepare_input",
    "rehearsing": "native_import",
    "importing": "native_import",
    "validating": "validate_import",
    "activating": "activate",
    "source_restored": "restore_v1",
    "discarding_rehearsal": "cleanup_test",
    "v2_activation": "activate",
    "v2_consumers": "resume_services",
    "v2_consumer_recovery": "resume_services",
}
_RECOVERY = {
    "rolling_back",
    "restoring_source",
    "restoring_archive",
    "restoring_metadata",
    "verifying_restored_source",
}
_STATES = {"pending", "running", "completed", "waiting", "not_run"}
_IDS = {
    "check_plan",
    "pause_services",
    "source_database",
    "save_recovery",
    "prepare_input",
    "native_import",
    "validate_import",
    "review",
    "activate",
    "resume_services",
    "restore_v1",
    "cleanup_test",
}


def _rows(value):
    if (
        not isinstance(value, dict)
        or type(value.get("protocol")) is not int
        or value.get("protocol") != 1
        or not isinstance(value.get("steps"), list)
    ):
        return None
    return [
        row
        for row in value["steps"][:16]
        if isinstance(row, dict)
        and isinstance(row.get("id"), str)
        and row["id"] in _IDS
        and isinstance(row.get("state"), str)
        and row["state"] in _STATES
    ]


def stage_plan(plan, mode="prepare"):
    """List only applicable milestones, without claiming any were executed."""
    migrate = plan.get("mode") != "fresh"
    ids = ["check_plan", "pause_services"]
    if migrate and (plan.get("source") or {}).get("provider") in {
        "postgres",
        "postgresql",
    }:
        ids.append("source_database")
    ids += ["save_recovery", "prepare_input", "native_import", "validate_import"]
    if mode == "rehearsal":
        ids += ["restore_v1", "cleanup_test"]
    else:
        if mode == "prepare":
            ids.append("review")
        ids += ["activate", "resume_services"]
    return {
        "protocol": 1,
        "tracking": "recorded",
        "steps": [{"id": key, "state": "pending"} for key in ids],
    }


def checkpoint(
    job, stage, *, completed=(), current=None, recovery=False, not_run=(), now=None
):
    """Persist explicit helper-return milestones, not positional completion guesses."""
    value = job.get("stage_progress")
    retained_rows = _rows(value)
    if retained_rows is None:
        return  # An older job must not acquire invented historical evidence.
    clock = int(time.time()) if now is None else now
    rows = {item["id"]: item for item in retained_rows}
    completed = completed if isinstance(completed, (list, tuple)) else ()
    for key in completed:
        if isinstance(key, str) and key in rows and rows[key]["state"] != "completed":
            rows[key].update(state="completed", completed_at=clock)
    for key in not_run if isinstance(not_run, (list, tuple)) else ():
        if isinstance(key, str) and key in rows and rows[key]["state"] != "completed":
            rows[key]["state"] = "not_run"
            if value.get("current_step_id") == key:
                value.pop("current_step_id", None)
    stage = stage if isinstance(stage, str) else ""
    current = current if isinstance(current, str) else None
    key = (
        "restore_v1"
        if recovery or stage in _RECOVERY
        else current or _STAGES.get(stage)
    )
    previous = value.get("current_step_id")
    if (
        recovery
        and isinstance(previous, str)
        and previous in rows
        and previous != "restore_v1"
    ):
        value.setdefault("failed_step_id", previous)
    if key == "restore_v1" and key not in rows:
        rows[key] = {"id": key, "state": "pending"}
        value["steps"].append(rows[key])
    if key in rows and rows[key]["state"] != "completed":
        rows[key].update(state="running")
        rows[key].setdefault("started_at", clock)
        value["current_step_id"] = key
    elif value.get("current_step_id") in completed:
        value.pop("current_step_id", None)


def public_stage_progress(job, state):
    """Expose bounded authored identifiers and truthful terminal/restart states."""
    expected = stage_plan(job.get("plan") or {}, job.get("mode") or "cutover")
    retained = job.get("stage_progress")
    retained_rows = _rows(retained)
    recorded = retained_rows is not None
    value = copy.deepcopy(expected)
    value["tracking"] = "recorded" if recorded else "legacy"
    allowed = {row["id"] for row in expected["steps"]} | {"restore_v1"}
    if recorded:
        seen = set()
        for row in retained_rows:
            if (
                not isinstance(row, dict)
                or row.get("id") not in allowed
                or row["id"] in seen
                or row.get("state") not in _STATES
            ):
                continue
            seen.add(row["id"])
            target = next(
                (item for item in value["steps"] if item["id"] == row["id"]), None
            )
            if target is None:
                target = {"id": row["id"], "state": "pending"}
                value["steps"].append(target)
            target["state"] = row["state"]
            for field in ("started_at", "completed_at"):
                if type(row.get(field)) is int and 0 <= row[field] <= 2**63 - 1:
                    target[field] = row[field]
        current = retained.get("current_step_id")
        if not isinstance(current, str):
            current = None
    else:
        current = (
            "restore_v1"
            if state == "rolling_back" or job.get("stage") in _RECOVERY
            else _STAGES.get(job.get("stage"))
        )
        if current == "restore_v1" and current not in {
            row["id"] for row in value["steps"]
        }:
            value["steps"].append({"id": current, "state": "pending"})
    rows = {row["id"]: row for row in value["steps"]}
    if state == "prepared" and "review" in rows:
        rows["review"]["state"] = "waiting"
        current = "review"
    terminal = state in {
        "failed",
        "failed_restored",
        "cancelled",
        "rolled_back",
        "completed",
        "rehearsed",
    }
    interrupted = state in {"interrupted", "recovery_required"}
    for row in value["steps"]:
        if row["state"] == "completed":
            continue
        if (
            recorded
            and row["id"] == retained.get("failed_step_id")
            and state not in {"cancelled", "rolled_back"}
        ):
            row["state"] = "interrupted" if job.get("cancel_requested") else "failed"
        elif terminal and recorded:
            if row["id"] == current and state in {"failed", "failed_restored"}:
                row["state"] = "failed"
            elif row["state"] == "running":
                row["state"] = "interrupted"
            else:
                row["state"] = "not_run"
        elif interrupted:
            row["state"] = "interrupted" if row["id"] == current else row["state"]
        elif row["id"] == current and state == "stage_retry_required":
            row["state"] = "waiting"
        elif row["id"] == current and state != "prepared":
            row["state"] = "running"
    if current in rows:
        value["current_step_id"] = current
    return value


def storage_work(plan, snapshot=None):
    """Compare known *extra* logical I/O; not elapsed time or an admission check."""
    capacity = plan.get("capacity") or {}
    source = capacity.get("source") or {}

    def count(value):
        return value if type(value) is int and 0 <= value <= 2**63 - 1 else None

    total = count(source.get("logical_bytes")) if capacity.get("complete") else None
    data = next(
        (
            count(row.get("logical_bytes"))
            for row in source.get("roots", [])
            if isinstance(row, dict) and row.get("role") == "data_dir"
        ),
        None,
    )
    blob = count((capacity.get("legacy_blobs") or {}).get("logical_bytes"))
    options = plan.get("options") or {}
    selected = (
        "external_blobs"
        if options.get("offload_legacy_blobs")
        else "reuse_local" if options.get("reclaim_source_space") else "keep_local"
    )
    archive = plan.get("backup_storage") or {}
    archive_type = (
        archive.get("storage_type")
        if archive.get("storage_type") in {"local", "network"}
        else "local" if not archive else "unknown"
    )
    actual_archive = count(
        ((snapshot or {}).get("cold_archive") or {}).get("size_bytes")
    )
    archive_bytes = actual_archive if actual_archive is not None else total
    minimum = actual_archive is None

    def multiple(value, passes):
        return value * passes if value is not None else None

    def leg(key, read, write, storage="local", passes=1, lower_bound=False):
        return {
            "id": key,
            "read_bytes": read,
            "write_bytes": write,
            "storage": storage,
            "passes": passes,
            "minimum": lower_bound,
        }

    restoration = [
        leg(
            "archive_restore_checks",
            multiple(archive_bytes, 2),
            0,
            "archive",
            2,
            minimum,
        ),
        leg("archive_restore", archive_bytes, data, "archive", lower_bound=minimum),
        leg("restored_readback", data, 0),
    ]
    paths = [
        {
            "id": "reuse_local",
            "requires_registered_archive": True,
            "preparation": [
                leg(
                    "archive_reuse_checks",
                    multiple(archive_bytes, 2),
                    0,
                    "archive",
                    2,
                    minimum,
                )
            ],
            "activation": [
                leg(
                    "archive_acceptance",
                    archive_bytes,
                    0,
                    "archive",
                    lower_bound=minimum,
                )
            ],
            "restoration": restoration,
        },
        {
            "id": "keep_local",
            "requires_registered_archive": False,
            "preparation": [
                leg("candidate_copy", data, data),
                leg("retained_source_check", data, 0),
            ],
            "activation": [leg("retained_source_check", data, 0)],
            "restoration": [],
        },
        {
            "id": "external_blobs",
            "requires_registered_archive": True,
            "preparation": [
                leg(
                    "archive_reuse_checks",
                    multiple(archive_bytes, 3),
                    0,
                    "archive",
                    3,
                    minimum,
                ),
                leg("blob_copy", blob, blob, "archive"),
                leg("blob_readback", multiple(blob, 3), 0, "archive", 3),
            ],
            "activation": [
                leg(
                    "archive_acceptance",
                    archive_bytes,
                    0,
                    "archive",
                    lower_bound=minimum,
                )
            ],
            "restoration": restoration,
        },
    ]
    for path in paths:
        path["selected"] = path["id"] == selected
        path["preparation_order"] = None
    return {
        "protocol": 1,
        "comparison_basis": "logical_io_same_storage",
        "selected_path": selected,
        "source_data_bytes": data,
        "source_total_bytes": total,
        "legacy_blob_bytes": blob,
        "archive_storage_type": archive_type,
        "archive_required": bool(options.get("reclaim_source_space")),
        "archive_bytes": archive_bytes,
        "archive_bytes_is_minimum": minimum,
        "ranking": "not_estimated",
        "paths": paths if plan.get("mode") != "fresh" else [],
        "limitations": [
            "elapsed_ranking_needs_storage_rates",
            "common_backup_and_verification_excluded",
            "rehearsal_and_rollback_restore_v1",
            "archive_restore_read_is_minimum",
            "native_provider_and_database_work_unestimated",
            "network_metadata_and_import_reads_unestimated",
            "alternatives_need_independent_capacity_checks",
        ],
    }
