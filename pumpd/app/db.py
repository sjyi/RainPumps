"""Database session management."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base


def make_engine(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def _migrate_schema(engine: Engine) -> None:
    """Apply lightweight additive migrations for existing SQLite databases."""
    inspector = inspect(engine)
    if "pump_state" in inspector.get_table_names():
        columns = {col["name"] for col in inspector.get_columns("pump_state")}
        alters: list[str] = []
        if "post_rain_drain_started_at" not in columns:
            alters.append("ALTER TABLE pump_state ADD COLUMN post_rain_drain_started_at DATETIME")
        if "sensor_dry_since" not in columns:
            alters.append("ALTER TABLE pump_state ADD COLUMN sensor_dry_since DATETIME")
        if "duty_cycle_started_at" not in columns:
            alters.append("ALTER TABLE pump_state ADD COLUMN duty_cycle_started_at DATETIME")
        if "safety_override_approved" not in columns:
            alters.append(
                "ALTER TABLE pump_state ADD COLUMN safety_override_approved BOOLEAN DEFAULT 0"
            )
        if "manual_context_json" not in columns:
            alters.append("ALTER TABLE pump_state ADD COLUMN manual_context_json TEXT")
        if alters:
            with engine.begin() as conn:
                for stmt in alters:
                    conn.execute(text(stmt))


def init_db(database_url: str) -> sessionmaker[Session]:
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)
    _migrate_schema(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
