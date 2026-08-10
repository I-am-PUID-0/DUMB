import logging
import threading

from utils.config_loader import CONFIG_MANAGER

_RUNTIME_LOG_LEVEL_LOCK = threading.RLock()
_runtime_debug_override_active = False
_UVICORN_LOGGER_NAMES = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")
_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _normalize_log_level(value, default="INFO"):
    normalized = str(value or "").strip().upper()
    if normalized == "WARN":
        normalized = "WARNING"
    return normalized if normalized in _VALID_LOG_LEVELS else default


def _configured_log_levels():
    dumb_config = CONFIG_MANAGER.get("dumb") or {}
    app_level = _normalize_log_level(dumb_config.get("log_level"), "INFO")
    api_config = dumb_config.get("api_service") or {}
    uvicorn_level = _normalize_log_level(api_config.get("log_level"), app_level)
    return app_level, uvicorn_level


def _level_name(logger):
    return logging.getLevelName(logger.getEffectiveLevel()).upper()


def get_runtime_log_level_state(logger):
    configured_level, configured_uvicorn_level = _configured_log_levels()
    with _RUNTIME_LOG_LEVEL_LOCK:
        override_active = _runtime_debug_override_active
        effective_level = _level_name(logger)
    return {
        "configured_level": configured_level,
        "configured_uvicorn_level": configured_uvicorn_level,
        "effective_level": effective_level,
        "debug_enabled": effective_level == "DEBUG",
        "override_active": override_active,
        "temporary": override_active,
        "resets_on_restart": True,
    }


def set_runtime_debug_logging(enabled, logger):
    global _runtime_debug_override_active

    configured_level, configured_uvicorn_level = _configured_log_levels()
    with _RUNTIME_LOG_LEVEL_LOCK:
        if enabled:
            logger.setLevel(logging.DEBUG)
            for logger_name in _UVICORN_LOGGER_NAMES:
                logging.getLogger(logger_name).setLevel(logging.DEBUG)
            _runtime_debug_override_active = True
            logger.info(
                "Temporary DEBUG logging enabled for the DUMB API; the override "
                "will reset when the container restarts."
            )
        else:
            if _runtime_debug_override_active:
                logger.info(
                    "Temporary DEBUG logging disabled; restoring configured DUMB "
                    "API log levels."
                )
            logger.setLevel(getattr(logging, configured_level))
            uvicorn_numeric_level = getattr(logging, configured_uvicorn_level)
            for logger_name in _UVICORN_LOGGER_NAMES:
                logging.getLogger(logger_name).setLevel(uvicorn_numeric_level)
            _runtime_debug_override_active = False

    return get_runtime_log_level_state(logger)
