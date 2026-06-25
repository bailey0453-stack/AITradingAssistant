"""Database setup (SQLAlchemy 2.0).

SQLite for local development; Postgres-ready by changing DATABASE_URL.
"""

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()


def _resolve_database_url(url: str) -> str:
    """Pick a writable SQLite path on read-only serverless filesystems.

    On Vercel (and similar) only `/tmp` is writable, so a relative SQLite
    path would fail. When the `VERCEL` env var is present and SQLite is in
    use, store the DB under `/tmp` (ephemeral per instance). For durable
    storage in production, set `DATABASE_URL` to a Postgres URL instead.
    Local development is unaffected.
    """
    if url.startswith("sqlite") and os.environ.get("VERCEL"):
        return "sqlite:////tmp/aitrading.db"
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
