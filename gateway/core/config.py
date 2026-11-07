"""Externalized configuration with Tier 1 storage detection."""

import os
import tempfile
from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_default_sqlite_path() -> str:
    if os.name != "nt":
        shm_dir = Path("/dev/shm")
        if shm_dir.exists() and shm_dir.is_dir() and os.access(shm_dir, os.W_OK):
            return str((shm_dir / "dual_plane_gateway_ratelimit.db").resolve())
    return str((Path(tempfile.gettempdir()) / "dual_plane_gateway_ratelimit.db").resolve())


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
    mcp_server_script: str = Field(
        default="servers/customer_service_mcp.py",
        alias="MCP_SERVER_SCRIPT",
    )
    primary_model_base_url: str = Field(
        default="https://api.primary-llm.internal/v1",
        alias="PRIMARY_MODEL_BASE_URL",
    )
    primary_model_api_key: str = Field(
        default="sk-primary-placeholder",
        alias="PRIMARY_MODEL_API_KEY",
    )
    secondary_model_base_url: str = Field(
        default="https://api.secondary-llm.internal/v1",
        alias="SECONDARY_MODEL_BASE_URL",
    )
    secondary_model_api_key: str = Field(
        default="sk-secondary-placeholder",
        alias="SECONDARY_MODEL_API_KEY",
    )
    default_model_name: str = Field(
        default="quilr-enterprise-default",
        alias="DEFAULT_MODEL_NAME",
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
        if not db_parent.exists() or not os.access(db_parent, os.W_OK):
            raise RuntimeError(
                f"Substrate check failed: SQLite directory not writable: {db_parent}"
            )
        if self.rate_limit_tokens_per_minute <= 0:
            raise ValueError("RATE_LIMIT_TOKENS_PER_MINUTE must be positive.")
        if self.upstream_timeout_ms <= 0:
            raise ValueError("UPSTREAM_TIMEOUT_MS must be positive.")


settings = GatewaySettings()
