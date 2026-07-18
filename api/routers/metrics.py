from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from utils.dependencies import (
    get_api_state,
    get_logger,
    get_metrics_collector,
    get_metrics_history_manager,
    get_optional_current_user,
    get_process_handler,
)
from utils.config_loader import CONFIG_MANAGER
from utils.metrics_history_reader import prepare_history_series
from utils.metrics_postgres import (
    MetricsPostgresActivationError,
    activate_metrics_postgresql,
)
import time

metrics_router = APIRouter()


@metrics_router.get("")
async def get_metrics_snapshot(
    collector=Depends(get_metrics_collector),
    current_user: str = Depends(get_optional_current_user),
):
    return collector.snapshot()


@metrics_router.get("/database-health")
async def get_database_health(
    refresh: bool = Query(default=False),
    process_name: str | None = Query(default=None),
    collector=Depends(get_metrics_collector),
    current_user: str = Depends(get_optional_current_user),
):
    if refresh:
        service_id = (
            collector.database_health.service_id_for_process(
                CONFIG_MANAGER.config, process_name
            )
            if process_name
            else None
        )
        if not process_name or service_id:
            collector.database_health.invalidate(service_id)
    result = collector.database_health.snapshot(
        CONFIG_MANAGER.config,
        details=True,
        refresh_if_stale=True,
        process_name=process_name,
    )
    return result


@metrics_router.get("/history")
async def get_metrics_history(
    since: float | None = Query(default=None),
    full: bool = Query(default=False),
    limit: int = Query(default=5000),
    history_manager=Depends(get_metrics_history_manager),
    current_user: str = Depends(get_optional_current_user),
):
    if since is None and not full:
        since = time.time() - (6 * 60 * 60)

    items, truncated = await run_in_threadpool(
        history_manager.read,
        since=since,
        full=full,
        limit=limit,
        default_hours=6,
    )
    return {"items": items, "truncated": truncated}


@metrics_router.get("/history_series")
async def get_metrics_history_series(
    since: float | None = Query(default=None),
    full: bool = Query(default=False),
    limit: int = Query(default=5000),
    bucket_seconds: int | None = Query(default=None),
    max_points: int = Query(default=600),
    history_manager=Depends(get_metrics_history_manager),
    current_user: str = Depends(get_optional_current_user),
):
    if since is None and not full:
        since = time.time() - (6 * 60 * 60)

    items, truncated = await run_in_threadpool(
        history_manager.read,
        since=since,
        full=full,
        limit=limit,
        default_hours=6,
    )
    items, series, truncated, stats, bucket_seconds = await run_in_threadpool(
        prepare_history_series,
        items,
        truncated=truncated,
        since=since,
        full=full,
        default_hours=6,
        bucket_seconds=bucket_seconds,
        max_points=max_points,
    )
    timestamps = [item.get("timestamp") for item in items]
    return {
        "items": items,
        "series": series,
        "timestamps": timestamps,
        "truncated": truncated,
        "stats": stats,
        "bucket_seconds": bucket_seconds,
    }


@metrics_router.get("/history/storage")
async def get_metrics_history_storage(
    probe_postgresql: bool = Query(default=False),
    history_manager=Depends(get_metrics_history_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return await run_in_threadpool(
        history_manager.status, probe_postgresql=probe_postgresql
    )


@metrics_router.post("/history/migrate")
async def migrate_metrics_history(
    force: bool = Query(default=False),
    history_manager=Depends(get_metrics_history_manager),
    current_user: str = Depends(get_optional_current_user),
):
    return await run_in_threadpool(history_manager.migrate_legacy, force=force)


@metrics_router.post("/history/storage/activate-postgresql")
async def activate_metrics_history_postgresql(
    process_handler=Depends(get_process_handler),
    api_state=Depends(get_api_state),
    history_manager=Depends(get_metrics_history_manager),
    logger=Depends(get_logger),
    current_user: str = Depends(get_optional_current_user),
):
    try:
        return await run_in_threadpool(
            activate_metrics_postgresql,
            CONFIG_MANAGER,
            process_handler,
            api_state,
            history_manager,
            logger,
        )
    except MetricsPostgresActivationError as exc:
        logger.error(
            "PostgreSQL Metrics activation failed during %s: %s",
            exc.stage,
            exc,
        )
        status_code = 409 if exc.stage == "busy" else 503
        raise HTTPException(
            status_code=status_code,
            detail=(
                "PostgreSQL Metrics activation is already running."
                if exc.stage == "busy"
                else f"PostgreSQL Metrics activation failed during {exc.stage}. "
                "SQLite remains active; check the DUMB logs and retry."
            ),
        ) from None
