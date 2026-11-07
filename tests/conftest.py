"""Shared pytest fixtures and test primitives.

Interface: isolated_sqlite_path (volatile DB path), gateway_client (lifespan
bound TestClient), model_plane (component reset around each model plane test).
Also makes direct execution safe: python3 tests/<file>.py resolves the project
root and applies the same warning filters as pyproject.toml.
Dependencies: pytest, fastapi.testclient, gateway.*.
"""

import asyncio
import sys
import warnings
from collections.abc import Generator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DEPRECATION_MODULE_PATTERNS = (r"starlette(\..*)?", r"httpx(\..*)?")
DEPRECATION_MESSAGE_PREFIXES = (
    "Using `httpx` with `starlette.testclient` is deprecated",
    "The 'app' shortcut is now deprecated",
)
for _module_pattern in DEPRECATION_MODULE_PATTERNS:
    warnings.filterwarnings("ignore", category=DeprecationWarning, module=_module_pattern)
for _message_prefix in DEPRECATION_MESSAGE_PREFIXES:
    warnings.filterwarnings("ignore", message=_message_prefix)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from gateway.core.config import settings  # noqa: E402
from gateway.main import app  # noqa: E402
from gateway.model_plane.model_router import reset_model_plane  # noqa: E402


DIRECT_RUN_ARGS = (
    "-c",
    str(PROJECT_ROOT / "pyproject.toml"),
    f"--rootdir={PROJECT_ROOT}",
    "-W",
    "ignore::pytest.PytestAssertRewriteWarning",
    "-v",
)


def run_module_tests(module_file: str) -> int:
    """Entry point for python3 tests/<file>.py: run that file under pytest.

    The assert-rewrite filter is direct-run only: this module imports anyio
    (via fastapi.testclient) before pytest boots, which pytest otherwise flags.
    """
    return int(pytest.main([*DIRECT_RUN_ARGS, module_file]))


@pytest.fixture()
def isolated_sqlite_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated, volatile SQLite path for tests."""
    db_file = tmp_path / "test_gateway_ratelimit.db"
    monkeypatch.setattr(settings, "sqlite_db_path", str(db_file))
    return db_file


@pytest.fixture()
def model_plane(isolated_sqlite_path: Path) -> Generator[Path, None, None]:
    """Reset injected model plane components before and after each test."""
    asyncio.run(reset_model_plane())
    yield isolated_sqlite_path
    asyncio.run(reset_model_plane())


@pytest.fixture()
def gateway_client(model_plane: Path) -> Generator[TestClient, None, None]:
    """Provide a FastAPI TestClient bound to the isolated SQLite configuration."""
    with TestClient(app) as client:
        yield client


if __name__ == "__main__":
    raise SystemExit(run_module_tests(str(PROJECT_ROOT / "tests")))