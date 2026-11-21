"""Engine / session factory / migrations.

Synchronous SQLAlchemy, always called from worker threads (see Repository). SQLAlchemy's asyncio layer needs
`greenlet`, a native extension that Windows Smart App Control blocks on this PC, so it is deliberately not used.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path.as_posix()}", connect_args={"timeout": 30, "check_same_thread": False}
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA journal_mode=WAL")  # readers never block the writer
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=30000")  # wait instead of failing with "database is locked"
        cur.close()

    return engine


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    # expire_on_commit=False: objects stay readable after commit
    return sessionmaker(engine, expire_on_commit=False)


def _alembic_config(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    return cfg


async def run_migrations(db_path: Path) -> None:
    """Bring the database to the latest schema. Alembic is synchronous, so run it in a worker thread."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(command.upgrade, _alembic_config(db_path), "head")
    logger.info("Database migrations applied (%s)", db_path)
