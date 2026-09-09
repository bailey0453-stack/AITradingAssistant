"""Database setup (SQLAlchemy 2.0)."""

from __future__ import annotations
import logging, os
from collections.abc import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()
_TMP_SQLITE_URL = "sqlite:////tmp/aitrading.db"
_DEFAULT_SQLITE_URL = "sqlite:///./aitrading.db"
_POSTGRES_ENV_FALLBACKS = ("DATABASE_URL","POSTGRES_URL","POSTGRES_PRISMA_URL","POSTGRES_URL_NON_POOLING","DATABASE_URL_UNPOOLED")

def _sqlite_path(url: str) -> str | None:
    prefix="sqlite:///"
    if not url.startswith(prefix): return None
    path=url[len(prefix):]
    return None if not path or path==":memory:" else path

def _normalize_db_url(url:str)->str:
    if url.startswith("postgresql+"): return url
    if url.startswith("postgres://"): return "postgresql+psycopg://"+url[len("postgres://"):]
    if url.startswith("postgresql://"): return "postgresql+psycopg://"+url[len("postgresql://"):]
    return url

def _select_url()->str:
    url=settings.database_url
    if _sqlite_path(url) is not None and url==_DEFAULT_SQLITE_URL:
        for name in _POSTGRES_ENV_FALLBACKS:
            candidate=os.getenv(name)
            if candidate:
                logger.info("Using Postgres URL from %s.", name); return candidate
    return url

def _resolve_database_url(url:str)->str:
    path=_sqlite_path(url)
    if path is None: return url
    db_dir=os.path.dirname(os.path.abspath(path)) or "."
    if os.path.isdir(db_dir) and os.access(db_dir, os.W_OK): return url
    logger.warning("SQLite directory %r is not writable; using ephemeral %s instead.", db_dir, _TMP_SQLITE_URL)
    return _TMP_SQLITE_URL

DATABASE_URL=_normalize_db_url(_resolve_database_url(_select_url()))
_is_sqlite=DATABASE_URL.startswith("sqlite")
_engine_kwargs={"connect_args":{"check_same_thread":False} if _is_sqlite else {},"pool_pre_ping":True,"future":True}
if not _is_sqlite: _engine_kwargs["pool_recycle"]=300
engine=create_engine(DATABASE_URL, **_engine_kwargs)
SessionLocal=sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def database_kind()->str:
    name=engine.dialect.name
    return "postgres" if name.startswith("postgre") else name

def database_is_persistent()->bool: return database_kind()=="postgres"

class Base(DeclarativeBase):
    pass

def init_db()->None:
    from app import models  # noqa: F401
    Base.metadata.create_all(bind=engine)
    _apply_additive_migrations()

def _apply_additive_migrations()->None:
    from sqlalchemy import inspect, text
    insp=inspect(engine); stmts=[]
    if insp.has_table("recommendations"):
        cols={c["name"] for c in insp.get_columns("recommendations")}
        if "fix_bid" not in cols: stmts.append("ALTER TABLE recommendations ADD COLUMN fix_bid FLOAT")
        if "fix_ask" not in cols: stmts.append("ALTER TABLE recommendations ADD COLUMN fix_ask FLOAT")
    if insp.has_table("research_market_snapshots"):
        cols={c["name"] for c in insp.get_columns("research_market_snapshots")}
        for col in ("mx2y","mx10y","momentum_1h","momentum_2h","momentum_4h","intraday_vol_4h","intraday_vol_24h","ret_next_1h","ret_next_2h","ret_next_4h","iv_1w","iv_1m","rr25_1w","rr25_1m","butterfly_25d_1m","cftc_mxn_net","cftc_mxn_net_pct_oi","cftc_mxn_open_interest"):
            if col not in cols: stmts.append(f"ALTER TABLE research_market_snapshots ADD COLUMN {col} FLOAT")
    if insp.has_table("similarity_matches"):
        cols={c["name"] for c in insp.get_columns("similarity_matches")}
        if "research_snapshot_id" not in cols: stmts.append("ALTER TABLE similarity_matches ADD COLUMN research_snapshot_id INTEGER")
        if engine.dialect.name=="postgresql": stmts.append("ALTER TABLE similarity_matches ALTER COLUMN matched_event_id DROP NOT NULL")
    if insp.has_table("historical_market_snapshots"):
        cols={c["name"] for c in insp.get_columns("historical_market_snapshots")}
        if "value" not in cols: stmts.append("ALTER TABLE historical_market_snapshots ADD COLUMN value FLOAT")
    for sql in stmts:
        try:
            with engine.begin() as conn: conn.execute(text(sql))
        except Exception: pass

def get_db()->Generator[Session,None,None]:
    db=SessionLocal()
    try: yield db
    finally: db.close()
