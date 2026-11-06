"""Shared pytest fixtures and test primitives."""

from collections.abc import Generator
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from gateway.core.config import settings
from gateway.main import app


@pytest.fixture()
def isolated_sqlite_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated, volatile SQLite path for tests."""
    db_file = tmp_path / "test_gateway_ratelimit.db"
    monkeypatch.setattr(settings, "sqlite_db_path", str(db_file))
    return db_file


@pytest.fixture()
def gateway_client(isolated_sqlite_path: Path) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient bound to the isolated SQLite configuration."""
    with TestClient(app) as client:
        yield client
