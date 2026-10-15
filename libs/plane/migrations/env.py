import asyncio
import importlib
import importlib.util

from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

from app.config import settings
from app.db.engine import Base


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if importlib.util.find_spec("app.db.models") is not None:
    importlib.import_module("app.db.models")

target_metadata = Base.metadata

MIGRATION_LOCK = 0x68616E676172
"""Advisory lock key held for the length of an online migration run.

Two `alembic upgrade` runs at once (two replicas starting, a Job retried while the
first attempt is still running) would otherwise both read the same version and both
apply the next revision. The lock is transaction-scoped: the second run blocks until
the first commits, then reads the version the first one wrote and has nothing to do.
"""


def include_object(obj, name, type_, reflected, compare_to) -> bool:
    """Keeps views and other opted-out tables out of autogenerate."""
    if type_ == "table" and obj.info.get("skip_autogenerate", False):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        version_table="_version",
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
        version_table="_version",
    )
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_LOCK})
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
