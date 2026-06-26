"""Database setup (SQLAlchemy 2.0).

SQLite for local development; Postgres-ready by changing DATABASE_URL.
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


def _sqlite_path(url: str) -> str | None:
    """Return the filesystem path for a file-based SQLite URL, else None."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        return None
    path = url[len(prefix):]
    if not path or path == ":memory:":
        return None
    return path


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


DATABASE_URL = _resolve_database_url(settings.database_url)

# check_same_thread is only needed for SQLite + FastAPI's threaded workers.
_connect_args = (
    {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
)

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def init_db() -> None:
    """Create tables. Import models so they register on the metadata."""
    from app import models  # noqa: F401  (ensures models are imported)

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
