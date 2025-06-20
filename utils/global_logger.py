from utils.logger import get_logger

logger = None
_logger_initialized = False
websocket_manager = None


def initialize_logger():
    global logger, _logger_initialized, websocket_manager
    if not _logger_initialized:
        from api.connection_manager import ConnectionManager

        websocket_manager = ConnectionManager()
        logger = get_logger(
            log_name=None, log_dir=None, websocket_manager=websocket_manager
        )
        # logger.debug(f"Logger initialized with websocket manager: {websocket_manager}")
        _logger_initialized = True
    else:
        logger.debug("Logger already initialized.")


initialize_logger()
