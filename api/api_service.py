from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.routing import APIRoute, APIWebSocketRoute
from scalar_fastapi import get_scalar_api_reference
from fastapi.responses import HTMLResponse
from contextlib import asynccontextmanager
from uvicorn.config import Config
from uvicorn.server import Server
from utils.dependencies import (
    get_api_state,
    get_process_handler,
    get_logger,
    get_websocket_manager,
    get_metrics_manager,
    get_metrics_collector,
)
from api.routers.process import process_router
from api.routers.config import config_router
from api.routers.health import health_router
from api.routers.logs import logs_router
from api.routers.websocket_logs import websocket_router
from api.routers.metrics import metrics_router
from api.routers.websocket_metrics import websocket_metrics_router
from api.routers.websocket_status import websocket_status_router
from utils.config_loader import CONFIG_MANAGER
import threading, tomllib


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    websocket_manager = get_websocket_manager()
    logger = get_logger()
    logger.info("Shutting down WebSocket manager...")
    await websocket_manager.shutdown()
    logger.info("WebSocket manager shutdown complete.")


def get_version_from_pyproject(path="pyproject.toml") -> str:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
            return data["tool"]["poetry"]["version"]
    except Exception:
        return "0.0.0"


def create_app() -> FastAPI:
    app = FastAPI(
        title="Debrid Unlimited Media Bridge",
        version=get_version_from_pyproject(),
        redoc_url=None,
        lifespan=lifespan,
    )

    app.dependency_overrides[get_process_handler] = get_process_handler
    app.dependency_overrides[get_logger] = get_logger
    app.dependency_overrides[get_api_state] = get_api_state
    app.dependency_overrides[get_websocket_manager] = get_websocket_manager
    app.dependency_overrides[get_metrics_manager] = get_metrics_manager
    app.dependency_overrides[get_metrics_collector] = get_metrics_collector

    app.include_router(process_router, prefix="/process", tags=["Process Management"])
    app.include_router(config_router, prefix="/config", tags=["Configuration"])
    app.include_router(health_router, prefix="/health", tags=["Health"])
    app.include_router(logs_router, prefix="/logs", tags=["Logs"])
    app.include_router(metrics_router, prefix="/metrics", tags=["Metrics"])
    app.include_router(websocket_router, prefix="/ws", tags=["WebSocket Logs"])
    app.include_router(websocket_metrics_router, prefix="/ws", tags=["WebSocket Metrics"])
    app.include_router(websocket_status_router, prefix="/ws", tags=["WebSocket Status"])

    @app.get("/scalar", include_in_schema=False)
    async def scalar_docs():
        return get_scalar_api_reference(
            openapi_url="openapi.json",
            title=app.title,
        )

    logger = get_logger()
    for route in app.routes:
        if isinstance(route, APIRoute):
            logger.debug(f"Route: {route.path} | Methods: {route.methods}")
        elif isinstance(route, APIWebSocketRoute):
            logger.debug(f"WebSocket Route: {route.path}")

    origin_from_config = (
        CONFIG_MANAGER.config.get("dumb", {}).get("frontend", {}).get("origins", None)
    )
    origins = (
        [origin_from_config]
        if origin_from_config
        else ["http://localhost", "http://localhost:8000"]
    )
    logger.info(f"Allowed CORS origins set to: {origins}")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    return app


def start_fastapi_process():
    app = create_app()

    host = (
        CONFIG_MANAGER.config.get("dumb", {})
        .get("api_service", {})
        .get("host", "0.0.0.0")
    )
    port = (
        CONFIG_MANAGER.config.get("dumb", {}).get("api_service", {}).get("port", 8000)
    )
    log_level = (
        CONFIG_MANAGER.config.get("dumb", {})
        .get("api_service", {})
        .get("log_level", "info")
        .lower()
    )
    logger = get_logger()

    def run_server():
        config = Config(
            app=app,
            host=host,
            port=port,
            log_config=None,
            log_level=log_level,
        )
        server = Server(config)
        server.run()

    uvicorn_thread = threading.Thread(target=run_server, daemon=True)
    uvicorn_thread.start()
    logger.info(f"Started FastAPI server at {host}:{port}")
