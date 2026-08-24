"""Alembic environment for the application database.

The URL comes from ``app_db.config.settings`` (APP_DATABASE_URL) so migrations
always target the same DB the app uses. Batch mode is enabled on SQLite so
ALTER COLUMN / constraint changes work via table-rebuild.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

# Make ``app_db`` importable when alembic runs from the backend/ directory.
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from app_db.config import settings  # noqa: E402
from app_db.database import Base  # noqa: E402
from app_db import models  # noqa: E402,F401  (register models on Base.metadata)

config = context.config
if config.config_file_name is not None:
    try:
        # disable_existing_loggers=False so running migrations from inside the
        # app (init_db) doesn't silence the application's own loggers.
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        pass

target_metadata = Base.metadata
_URL = settings.APP_DATABASE_URL
_IS_SQLITE = _URL.startswith("sqlite")


def run_migrations_offline() -> None:
    context.configure(
        url=_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_IS_SQLITE,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_URL)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=_IS_SQLITE,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
