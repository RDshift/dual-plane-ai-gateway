from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from gateway.core.config import GatewaySettings, settings
from gateway.main import app


@pytest.fixture()
def isolated_sqlite_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_file = tmp_path / "test_gateway_ratelimit.db"
    monkeypatch.setattr(settings, "sqlite_db_path", str(db_file))
    return db_file


@pytest.fixture()
def gateway_client(isolated_sqlite_path: Path) -> TestClient:
    with TestClient(app) as client:
        yield client