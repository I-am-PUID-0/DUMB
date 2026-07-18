import threading

from utils.postgres import initialize_postgres_databases
from utils.setup import setup_project


class MetricsPostgresActivationError(RuntimeError):
    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


_activation_lock = threading.Lock()


def _config_root(config_manager):
    return (
        config_manager.config if hasattr(config_manager, "config") else config_manager
    )


def ensure_metrics_postgres_config(config_manager):
    """Enable PostgreSQL and register the configured Metrics database."""
    root = _config_root(config_manager)
    metrics = (root.get("dumb", {}) or {}).get("metrics", {}) or {}
    storage = metrics.get("storage", {}) or {}
    if str(storage.get("provider", "sqlite")).lower() != "postgresql":
        return False

    postgres = root.setdefault("postgres", {})
    changed = False
    if not postgres.get("enabled"):
        postgres["enabled"] = True
        changed = True

    database = (
        str(
            (storage.get("postgresql", {}) or {}).get("database", "dumb_metrics")
        ).strip()
        or "dumb_metrics"
    )
    databases = postgres.setdefault("databases", [])
    existing = next(
        (entry for entry in databases if entry.get("name") == database), None
    )
    if existing is None:
        databases.append({"name": database, "enabled": True})
        changed = True
    elif not existing.get("enabled", True):
        existing["enabled"] = True
        changed = True
    return changed


def activate_metrics_postgresql(
    config_manager,
    process_handler,
    api_state,
    history_manager,
    logger,
):
    """Provision PostgreSQL in place and promote it after history replay."""
    if not _activation_lock.acquire(blocking=False):
        raise MetricsPostgresActivationError(
            "busy", "PostgreSQL metrics activation is already running."
        )

    try:
        root = _config_root(config_manager)
        metrics = (root.get("dumb", {}) or {}).get("metrics", {}) or {}
        storage = metrics.get("storage", {}) or {}
        if str(storage.get("provider", "sqlite")).lower() != "postgresql":
            raise MetricsPostgresActivationError(
                "configuration",
                "Metrics history is not configured to use PostgreSQL.",
            )

        changed = ensure_metrics_postgres_config(config_manager)
        if changed:
            config_manager.save_config()
            logger.info(
                "Enabled PostgreSQL and registered the metrics history database."
            )

        postgres = root.get("postgres", {}) or {}
        process_name = postgres.get("process_name", "PostgreSQL")
        was_running = api_state.get_status(process_name) == "running"

        if not was_running:
            logger.info("Starting PostgreSQL in place for the Metrics history backend.")
            with process_handler.setup_tracker_lock:
                process_handler.setup_tracker.discard(process_name)
            success, error = setup_project(process_handler, process_name)
            if not success:
                raise MetricsPostgresActivationError(
                    "startup", error or "PostgreSQL setup failed."
                )
        else:
            success, error = initialize_postgres_databases(
                postgres.get("host", "127.0.0.1"),
                postgres.get("port", 5432),
                postgres.get("user", "DUMB"),
                postgres.get("password", "postgres"),
                postgres.get("databases", []),
            )
            if not success:
                raise MetricsPostgresActivationError(
                    "database", error or "Metrics database initialization failed."
                )

        logger.info("Synchronizing local Metrics history to PostgreSQL.")
        try:
            history_status = history_manager.activate_postgresql()
        except Exception as exc:
            raise MetricsPostgresActivationError("synchronization", str(exc)) from exc

        logger.info(
            "PostgreSQL Metrics history activation completed; synchronized %s sample(s).",
            history_status.get("synced_samples", 0),
        )
        return {
            "status": "active",
            "provider": "postgresql",
            "postgres_enabled": bool(postgres.get("enabled")),
            "postgres_running": True,
            "postgres_started": not was_running,
            "postgres_reused": was_running,
            "database": (storage.get("postgresql", {}) or {}).get(
                "database", "dumb_metrics"
            ),
            **history_status,
        }
    finally:
        _activation_lock.release()
