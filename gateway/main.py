"""Dual-Plane AI Gateway FastAPI application entrypoint."""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from gateway.core.config import settings
from gateway.core.diagnostics import STATUS_OK, run_zero_trust_diagnostics
from gateway.core.errors import build_gateway_error
from gateway.core.lifecycle import lifecycle_manager
from gateway.core.logging import configure_stderr_logging
from gateway.mcp_plane.mcp_router import ROLE_ADMIN, mcp_router, resolve_bearer_role
from gateway.model_plane.model_router import model_router

HTTP_OK = 200
HTTP_UNAUTHORIZED = 401
HTTP_FORBIDDEN = 403
HTTP_SERVICE_UNAVAILABLE = 503
DIAGNOSTICS_PATH = "/health/diagnostics"
WWW_AUTHENTICATE_VALUE = 'Bearer realm="diagnostics"'
MSG_DIAGNOSTICS_AUTH = "Authentication required: missing, malformed, or invalid Bearer token."
MSG_DIAGNOSTICS_ROLE = "Admin role required for diagnostics."

configure_stderr_logging(level=settings.log_level, logger_name="gateway")
logger = configure_stderr_logging(level=settings.log_level, logger_name="dual_plane.main")


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
        title="Dual-Plane AI Gateway",
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

    @app.get(DIAGNOSTICS_PATH)
    async def health_diagnostics(
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        role = resolve_bearer_role(authorization)
        if role is None:
            logger.warning("event=diagnostics_auth_failed")
            return JSONResponse(
                status_code=HTTP_UNAUTHORIZED,
                content=build_gateway_error(
                    "authentication_required", MSG_DIAGNOSTICS_AUTH, HTTP_UNAUTHORIZED
                ),
                headers={"WWW-Authenticate": WWW_AUTHENTICATE_VALUE},
            )
        if role != ROLE_ADMIN:
            logger.warning("event=diagnostics_rbac_denied role=%s", role)
            return JSONResponse(
                status_code=HTTP_FORBIDDEN,
                content=build_gateway_error("insufficient_role", MSG_DIAGNOSTICS_ROLE, HTTP_FORBIDDEN),
            )
        report: dict[str, Any] = await run_zero_trust_diagnostics()
        status_code = HTTP_OK if report["status"] == STATUS_OK else HTTP_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=status_code, content=report)

    app.include_router(mcp_router)
    app.include_router(model_router)
    return app


app = create_app()
