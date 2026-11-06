"""Dual-Plane AI Gateway FastAPI application entrypoint."""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from gateway.core.config import settings
from gateway.core.errors import build_gateway_error
from gateway.core.lifecycle import lifecycle_manager
from gateway.core.logging import configure_stderr_logging
from gateway.mcp_plane.mcp_router import mcp_router
from gateway.model_plane.model_router import model_router

logger = configure_stderr_logging(level=settings.log_level, logger_name="quilr.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Enforce startup substrate validation and teardown hygiene."""
    settings.verify_substrate()
    logger.info("Substrate verified; Dual-Plane AI Gateway online.")
    yield
    await lifecycle_manager.execute_shutdown()
    logger.info("Dual-Plane AI Gateway shutdown complete.")


def create_app() -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Quilr Dual-Plane AI Gateway",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        logger.error(f"Unhandled gateway exception on {request.url.path}: {exc}")
        return JSONResponse(
            status_code=500,
            content=build_gateway_error(
                code="internal_gateway_error",
                message="An unexpected error occurred within the gateway.",
                http_status=500,
            ),
        )

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        return {
            "status": "ok",
            "plane_mcp": "ready",
            "plane_model": "ready",
        }

    app.include_router(mcp_router)
    app.include_router(model_router)
    return app


app = create_app()