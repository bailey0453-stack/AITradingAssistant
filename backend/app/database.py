"""Database setup (SQLAlchemy 2.0).

SQLite for local development; **persistent Postgres in production** whenever a
Postgres connection URL is provided (Vercel/Neon sets ``DATABASE_URL``). When no
Postgres URL is configured the app falls back to ephemeral SQLite under ``/tmp``
on read-only serverless filesystems — fine for local/demo, not durable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

logger = logging.getLogger(__name__)

settings = get_settings()

# Writable fallback for read-only serverless filesystems (Vercel, AWS Lambda),
# where only /tmp is writable. Ephemeral per instance.
_TMP_SQLITE_URL = "sqlite:////tmp/aitrading.db"
_DEFAULT_SQLITE_URL = "sqlite:///./aitrading.db"

# Vercel/Neon expose the connection string under several names. ``DATABASE_URL``
# is the pooled URL (best for serverless); the rest are fallbacks.
_POSTGRES_ENV_FALLBACKS = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRES_PRISMA_URL",
    "POSTGRES_URL_NON_POOLING",
    "DATABASE_URL_UNPOOLED",
)


def _sqlite_path(url: str) -> str | None:
    """Return the filesystem path for a file-based SQLite URL, else None."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix):]
    if not path or path == ":memory:":
        return None
    return path


def _normalize_db_url(url: str) -> str:
    """Force the psycopg (v3) driver for Postgres URLs; pass others unchanged.

    Vercel/Neon hand out ``postgres://`` or ``postgresql://`` URLs, which
    SQLAlchemy would otherwise route to psycopg2. We ship psycopg v3, so we
    rewrite the scheme to ``postgresql+psycopg://``. SSL params already present
    in the query string (e.g. ``sslmode=require``) are preserved.
    """
    if url.startswith("postgresql+"):
        return url  # already driver-qualified
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


def _select_url() -> str:
    """Resolve the effective DB URL, preferring a configured Postgres URL.

    ``settings.database_url`` already reflects ``DATABASE_URL`` (pydantic). If it
    is still the built-in SQLite default, look for any Vercel/Neon Postgres env
    var so a connected database is used even if only ``POSTGRES_URL`` is set.
    """
    url = settings.database_url
    if _sqlite_path(url) is not None and url == _DEFAULT_SQLITE_URL:
        for env_name in _POSTGRES_ENV_FALLBACKS:
            candidate = os.getenv(env_name)
            if candidate:
                logger.info("Using Postgres URL from %s.", env_name)
                return candidate
    return url


def _resolve_database_url(url: str) -> str:
    """Ensure SQLite writes to a writable location.

    Directory writability is the authoritative signal:

      - Non-SQLite URLs (e.g. Postgres) -> returned unchanged.
      - SQLite path in a writable directory -> respected as-is. This covers
        local development (`./aitrading.db`) and any explicitly configured,
        writable `DATABASE_URL` (including `sqlite:////tmp/custom.db`).
      - SQLite path in a non-writable / missing directory (e.g. Vercel's
        read-only deployment FS) -> redirected to `/tmp/aitrading.db` so table
        creation doesn't crash at startup.

    For durable storage in production, set `DATABASE_URL` to a Postgres URL.
    """
    path = _sqlite_path(url)
    if path is None:
        return url

    db_dir = os.path.dirname(os.path.abspath(path)) or "."
    writable = os.path.isdir(db_dir) and os.access(db_dir, os.W_OK)
    if writable:
        return url

    logger.warning(
        "SQLite directory %r is not writable; using ephemeral %s instead.",
        db_dir,
        _TMP_SQLITE_URL,
    )
    return _TMP_SQLITE_URL


# Resolve (prefer Postgres) -> redirect unwritable SQLite -> force psycopg driver.
DATABASE_URL = _normalize_db_url(_resolve_database_url(_select_url()))

_is_sqlite = DATABASE_URL.startswith("sqlite")

# check_same_thread is only needed for SQLite + FastAPI's threaded workers.
_engine_kwargs: dict = {
    "connect_args": {"check_same_thread": False} if _is_sqlite else {},
    "pool_pre_ping": True,  # validate connections (Neon closes idle ones)
    "future": True,
}
if not _is_sqlite:
    # Serverless cold starts + Neon's idle-connection reaping: recycle often.
    _engine_kwargs["pool_recycle"] = 300

engine = create_engine(DATABASE_URL, **_engine_kwargs)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def database_kind() -> str:
    """Coarse database type for diagnostics/dashboard: ``postgres`` or ``sqlite``."""
    name = engine.dialect.name  # e.g. 'postgresql', 'sqlite'
    return "postgres" if name.startswith("postgre") else name


def database_is_persistent() -> bool:
    """True when storage survives redeploys/cold starts (i.e. Postgres)."""
    return database_kind() == "postgres"


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def init_db() -> None:
    """Create tables. Import models so they register on the metadata."""
    from app import models  # noqa: F401  (ensures models are imported)

    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()


def _apply_additive_migrations() -> None:
    """Best-effort additive schema updates (no Alembic). Idempotent."""
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    if not insp.has_table("similarity_matches"):
        return
    cols = {c["name"] for c in insp.get_columns("similarity_matches")}
    stmts: list[str] = []
    if "research_snapshot_id" not in cols:
        stmts.append(
            "ALTER TABLE similarity_matches ADD COLUMN research_snapshot_id INTEGER"
        )
    # matched_event_id may have been NOT NULL on older deployments.
    if engine.dialect.name == "postgresql":
        stmts.append(
            "ALTER TABLE similarity_matches ALTER COLUMN matched_event_id DROP NOT NULL"
        )
    for sql in stmts:
        try:
            with engine.begin() as conn:
                conn.execute(text(sql))
        except Exception:  # noqa: BLE001 - migration is best-effort
            pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
