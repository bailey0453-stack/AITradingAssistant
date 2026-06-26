"""Database setup (SQLAlchemy 2.0).

SQLite for local development; Postgres-ready by changing DATABASE_URL.
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

# Env vars that signal a serverless runtime with a read-only filesystem
# (only /tmp is writable). Used to relocate SQLite storage.
_SERVERLESS_ENV_MARKERS = (
    "VERCEL",
    "AWS_LAMBDA_FUNCTION_NAME",
    "LAMBDA_TASK_ROOT",
    "AWS_EXECUTION_ENV",
)

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

    Serverless platforms (Vercel, AWS Lambda) expose a read-only filesystem
    except for `/tmp`, so a relative SQLite path crashes table creation at
    startup. If we detect a serverless runtime OR the target directory is not
    writable, redirect SQLite to `/tmp` (ephemeral per instance). Non-SQLite
    URLs (e.g. Postgres) are returned unchanged, and local development — where
    the working directory is writable — is unaffected.

    For durable storage in production, set `DATABASE_URL` to a Postgres URL.
    """
    path = _sqlite_path(url)
    if path is None:
        return url

    on_serverless = any(os.environ.get(marker) for marker in _SERVERLESS_ENV_MARKERS)
    db_dir = os.path.dirname(os.path.abspath(path)) or "."
    writable = os.path.isdir(db_dir) and os.access(db_dir, os.W_OK)

    if on_serverless or not writable:
        return _TMP_SQLITE_URL
    return url


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
