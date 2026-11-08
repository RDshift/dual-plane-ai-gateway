"""On-disk SQLite sliding-window token rate limiter for the Model Inference Plane.

Interface: SqliteTokenRateLimiter.open() prepares the WAL-mode database,
consume(tenant_key, tokens) atomically evicts expired usage, sums the live
window and either records the usage or rejects it, returning a
RateLimitDecision. close() is idempotent and registered with
lifecycle_manager so shutdown is deterministic.

Concurrency: every consume() runs inside BEGIN IMMEDIATE (single writer
across processes, WAL readers unaffected) with bounded exponential backoff
and jitter on SQLITE_BUSY. In-process callers are serialized by an
asyncio.Lock and the blocking sqlite3 work runs on a worker thread.

Dependencies: standard library sqlite3, gateway.core.config,
gateway.core.lifecycle.
"""

import asyncio
import hashlib
import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from gateway.core.config import settings
from gateway.core.lifecycle import lifecycle_manager

BUSY_TIMEOUT_MS = 5000
BUSY_RETRY_ATTEMPTS = 6
BUSY_RETRY_BASE_S = 0.005
BUSY_RETRY_CAP_S = 0.25
BUSY_RETRY_JITTER_S = 0.005
TENANT_HASH_CHARS = 32

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_key TEXT NOT NULL,
    tokens INTEGER NOT NULL CHECK (tokens > 0),
    recorded_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_token_usage_tenant_time
    ON token_usage (tenant_key, recorded_at);
"""

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RateLimitDecision:
    """Outcome of one consume() call."""

    allowed: bool
    remaining: int
    retry_after_s: float


class RateLimiterClosedError(RuntimeError):
    """Raised when consume() is called before open() or after close()."""


def hash_tenant_key(raw_key: str) -> str:
    """Never persist raw credentials: tenants are keyed by a SHA-256 prefix."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:TENANT_HASH_CHARS]


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    text = str(exc).lower()
    return "locked" in text or "busy" in text


class SqliteTokenRateLimiter:
    """Token-aware sliding-window limiter backed by a WAL-mode SQLite file."""

    def __init__(
        self,
        db_path: str | None = None,
        tokens_per_window: int | None = None,
        window_seconds: int | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path or settings.sqlite_db_path).expanduser()
        self.tokens_per_window = int(
            settings.rate_limit_tokens_per_minute if tokens_per_window is None else tokens_per_window
        )
        self.window_seconds = int(
            settings.rate_limit_window_seconds if window_seconds is None else window_seconds
        )
        self.clock = clock
        if self.tokens_per_window <= 0 or self.window_seconds <= 0:
            raise ValueError("tokens_per_window and window_seconds must be positive")
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()
        self._hook_registered = False
        self.journal_mode: str = ""

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    async def open(self) -> None:
        """Create the database, apply PRAGMAs and schema, register teardown."""
        if self._conn is not None:
            return
        self._conn = await asyncio.to_thread(self._open_sync)
        if not self._hook_registered:
            lifecycle_manager.register_shutdown_hook(self.close)
            self._hook_registered = True
        logger.info(
            "event=rate_limiter_open path=%s journal_mode=%s limit=%s window_s=%s",
            self.db_path,
            self.journal_mode,
            self.tokens_per_window,
            self.window_seconds,
        )

    def _open_sync(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=BUSY_TIMEOUT_MS / 1000.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.journal_mode = str(conn.execute("PRAGMA journal_mode=WAL;").fetchone()[0]).lower()
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS};")
        conn.executescript(SCHEMA_SQL)
        return conn

    async def close(self) -> None:
        """Close the connection. Safe to call repeatedly or before open()."""
        conn, self._conn = self._conn, None
        if conn is not None:
            await asyncio.to_thread(conn.close)
            logger.info("event=rate_limiter_closed path=%s", self.db_path)

    async def consume(self, tenant_key: str, tokens: int) -> RateLimitDecision:
        """Atomically reserve tokens for tenant_key within the sliding window."""
        if tokens <= 0:
            raise ValueError("tokens must be positive")
        if self._conn is None:
            raise RateLimiterClosedError("rate limiter is not open")
        async with self._lock:
            return await asyncio.to_thread(
                self._consume_with_retry, hash_tenant_key(tenant_key), int(tokens)
            )

    async def window_usage(self, tenant_key: str) -> int:
        """Sum of live tokens for a tenant; diagnostic read, no mutation."""
        if self._conn is None:
            raise RateLimiterClosedError("rate limiter is not open")
        async with self._lock:
            return await asyncio.to_thread(self._window_usage_sync, hash_tenant_key(tenant_key))

    async def row_count(self) -> int:
        """Total usage rows currently stored; used to verify eviction."""
        if self._conn is None:
            raise RateLimiterClosedError("rate limiter is not open")
        async with self._lock:
            return await asyncio.to_thread(self._row_count_sync)

    def _consume_with_retry(self, tenant: str, tokens: int) -> RateLimitDecision:
        delay = BUSY_RETRY_BASE_S
        for attempt in range(1, BUSY_RETRY_ATTEMPTS + 1):
            try:
                return self._consume_sync(tenant, tokens)
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc) or attempt == BUSY_RETRY_ATTEMPTS:
                    raise
                logger.warning("event=rate_limiter_busy attempt=%s", attempt)
                time.sleep(min(delay, BUSY_RETRY_CAP_S) + random.uniform(0, BUSY_RETRY_JITTER_S))
                delay *= 2
        raise RuntimeError("unreachable")

    def _consume_sync(self, tenant: str, tokens: int) -> RateLimitDecision:
        conn = self._conn
        assert conn is not None
        now = float(self.clock())
        window_start = now - self.window_seconds
        limit = self.tokens_per_window
        conn.execute("BEGIN IMMEDIATE;")
        try:
            conn.execute("DELETE FROM token_usage WHERE recorded_at <= ?;", (window_start,))
            current = int(
                conn.execute(
                    "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_key = ?;",
                    (tenant,),
                ).fetchone()[0]
            )
            if current + tokens > limit:
                retry_after = self._retry_after_sync(conn, tenant, current, tokens, now)
                conn.execute("COMMIT;")
                return RateLimitDecision(
                    allowed=False,
                    remaining=max(0, limit - current),
                    retry_after_s=retry_after,
                )
            conn.execute(
                "INSERT INTO token_usage (tenant_key, tokens, recorded_at) VALUES (?, ?, ?);",
                (tenant, tokens, now),
            )
            conn.execute("COMMIT;")
        except BaseException:
            conn.execute("ROLLBACK;")
            raise
        return RateLimitDecision(
            allowed=True,
            remaining=limit - (current + tokens),
            retry_after_s=0.0,
        )

    def _retry_after_sync(
        self,
        conn: sqlite3.Connection,
        tenant: str,
        current: int,
        requested: int,
        now: float,
    ) -> float:
        """Seconds until enough of the oldest usage expires to admit the request."""
        limit = self.tokens_per_window
        if requested > limit:
            return float(self.window_seconds)
        needed = current + requested - limit
        freed = 0
        rows = conn.execute(
            "SELECT tokens, recorded_at FROM token_usage WHERE tenant_key = ? ORDER BY recorded_at ASC;",
            (tenant,),
        )
        for row_tokens, recorded_at in rows:
            freed += int(row_tokens)
            if freed >= needed:
                return max(0.0, (float(recorded_at) + self.window_seconds) - now)
        return float(self.window_seconds)

    def _window_usage_sync(self, tenant: str) -> int:
        conn = self._conn
        assert conn is not None
        window_start = float(self.clock()) - self.window_seconds
        row = conn.execute(
            "SELECT COALESCE(SUM(tokens), 0) FROM token_usage WHERE tenant_key = ? AND recorded_at > ?;",
            (tenant, window_start),
        ).fetchone()
        return int(row[0])

    def _row_count_sync(self) -> int:
        conn = self._conn
        assert conn is not None
        return int(conn.execute("SELECT COUNT(*) FROM token_usage;").fetchone()[0])
