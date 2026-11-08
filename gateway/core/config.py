"""Externalized configuration with durable on-disk SQLite defaults.

Interface: settings (GatewaySettings) loaded from environment and .env.
verify_substrate() resolves the SQLite path, creates its parent directory and
validates numeric bounds; it is called from the FastAPI lifespan and by the
diagnostics runner.
Dependencies: pydantic, pydantic-settings.
"""

import os
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SQLITE_RELATIVE_PATH = Path("data") / "gateway_ratelimit.db"


def _resolve_default_sqlite_path() -> str:
    """Durable, project-relative on-disk database path."""
    return str((PROJECT_ROOT / DEFAULT_SQLITE_RELATIVE_PATH).resolve())


class GatewaySettings(BaseSettings):
    """Runtime parameters governed by environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gateway_env: str = Field(default="development", alias="GATEWAY_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    sqlite_db_path: str = Field(
        default_factory=_resolve_default_sqlite_path,
        alias="SQLITE_DB_PATH",
    )
    rate_limit_tokens_per_minute: int = Field(
        default=50000,
        alias="RATE_LIMIT_TOKENS_PER_MINUTE",
    )
    rate_limit_window_seconds: int = Field(
        default=60,
        alias="RATE_LIMIT_WINDOW_SECONDS",
    )
    upstream_timeout_ms: int = Field(
        default=3000,
        alias="UPSTREAM_TIMEOUT_MS",
    )
    primary_model_base_url: str = Field(
        default="https://api.openai.com/v1",
        alias="PRIMARY_MODEL_BASE_URL",
    )
    primary_model_api_key: str = Field(default="", alias="PRIMARY_MODEL_API_KEY")
    secondary_model_base_url: str = Field(
        default="https://api.openai.com/v1",
        alias="SECONDARY_MODEL_BASE_URL",
    )
    secondary_model_api_key: str = Field(default="", alias="SECONDARY_MODEL_API_KEY")
    default_model_name: str = Field(default="dual-plane-enterprise-default", alias="DEFAULT_MODEL_NAME")
    stream_guardrail_max_holdback_chars: int = Field(
        default=256,
        alias="STREAM_GUARDRAIL_MAX_HOLDBACK_CHARS",
    )
    mcp_server_script: str = Field(
        default="servers/customer_service_mcp.py",
        alias="MCP_SERVER_SCRIPT",
    )
    token_registry: dict[str, str] = Field(
        default_factory=lambda: {
            "tok_admin_live_9x8a7b": "admin",
            "tok_viewer_live_3c4d5e": "viewer",
        }
    )

    def verify_substrate(self) -> None:
        if not self.sqlite_db_path or not self.sqlite_db_path.strip():
            self.sqlite_db_path = _resolve_default_sqlite_path()
        db_parent = Path(self.sqlite_db_path).expanduser().resolve().parent
        try:
            db_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"Substrate check failed: cannot create SQLite directory: {db_parent}"
            ) from exc
        if not os.access(db_parent, os.W_OK):
            raise RuntimeError(
                f"Substrate check failed: SQLite directory not writable: {db_parent}"
            )
        if self.rate_limit_tokens_per_minute <= 0:
            raise ValueError("RATE_LIMIT_TOKENS_PER_MINUTE must be positive.")
        if self.rate_limit_window_seconds <= 0:
            raise ValueError("RATE_LIMIT_WINDOW_SECONDS must be positive.")
        if self.upstream_timeout_ms <= 0:
            raise ValueError("UPSTREAM_TIMEOUT_MS must be positive.")
        if self.stream_guardrail_max_holdback_chars <= 0:
            raise ValueError("STREAM_GUARDRAIL_MAX_HOLDBACK_CHARS must be positive.")


settings = GatewaySettings()
