"""Alembic environment. Uses a plain synchronous SQLite connection (migrations are not performance critical)."""
from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

from app.config import get_settings
from app.database import models

config = context.config
target_metadata = models.Base.metadata


def _url() -> str:
    # `Config` built in code sets this; `alembic` run from the CLI falls back to the app settings.
    return config.get_main_option("sqlalchemy.url") or f"sqlite:///{get_settings().database_path.as_posix()}"


def _render_item(type_: str, obj: object, autogen_context) -> str | bool:  # noqa: ANN001
    if type_ == "type" and isinstance(obj, models.UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=_url(), target_metadata=target_metadata, literal_binds=True, render_as_batch=True,
        render_item=_render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata, render_as_batch=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
