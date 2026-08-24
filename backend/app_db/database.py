"""SQLAlchemy engine, session, and base for the application database."""

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

# SQLite needs check_same_thread=False for FastAPI's threaded request handling;
# the option is harmless/ignored for PostgreSQL.
_connect_args = {"check_same_thread": False} if settings.APP_DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    settings.APP_DATABASE_URL,
    connect_args=_connect_args,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


class Base(DeclarativeBase):
    """Declarative base for all application-database models."""


def get_db() -> Iterator[Session]:
    """FastAPI dependency: yield a session and always close it."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _run_migrations() -> None:
    """Bring the schema to head via Alembic (the single source of schema truth).

    A database created before Alembic was adopted (M1, via ``create_all``) has the
    app tables but no ``alembic_version``; we stamp it at the baseline first so the
    forward migrations apply cleanly. Fresh databases upgrade from scratch.
    """
    import os

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg = Config(os.path.join(backend_dir, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(backend_dir, "migrations"))

    tables = set(inspect(engine).get_table_names())
    if "alembic_version" not in tables and "users" in tables:
        # Pre-Alembic database → adopt it at the M1 baseline, then upgrade forward.
        command.stamp(cfg, "0001")
    command.upgrade(cfg, "head")


def init_db() -> None:
    """Migrate to head, seed RBAC, and bootstrap the default org. Idempotent."""
    # Import models so they are registered on Base.metadata for Alembic autogenerate.
    from . import models  # noqa: F401
    from .seed import (
        bootstrap_admin, bootstrap_ai_providers, bootstrap_default_org,
        seed_roles_and_permissions,
    )

    _run_migrations()
    with SessionLocal() as db:
        seed_roles_and_permissions(db)
        org = bootstrap_default_org(db)
        bootstrap_admin(db, org)
        bootstrap_ai_providers(db, org)
        db.commit()
