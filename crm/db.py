"""Engine / session plumbing."""
from __future__ import annotations

import enum
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from crm.config import settings
from crm.models import Base

log = logging.getLogger(__name__)

_connect_args = {}
if settings.database_url.startswith("sqlite"):
    _connect_args = {"check_same_thread": False}

engine = create_engine(
    settings.database_url, connect_args=_connect_args, pool_pre_ping=True, future=True
)

if settings.database_url.startswith("sqlite"):

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
    ensure_schema()


def ensure_schema() -> None:
    """Add columns that exist in the models but not yet in the database.

    `create_all` creates missing tables but never alters existing ones, so a
    new field on an existing model breaks every query against it until the
    column is really there. Same convention the service tracker uses: columns
    are only ever appended, never renamed, reordered or dropped, which makes an
    additive ALTER the whole of the migration story.

    Only ever additive: it does not rename, reorder, drop or retype anything,
    and it does not touch a column that already exists.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    for table in Base.metadata.sorted_tables:
        if table.name not in existing_tables:
            continue  # create_all just made it, in full
        present = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            backfill = _default_value(column)
            ddl = column.type.compile(engine.dialect)
            # Always added nullable, even where the model says NOT NULL: SQLite
            # cannot add a NOT NULL column to a populated table at all, and the
            # ORM supplies the default on every insert from here on. Rows that
            # predate the column are backfilled below when there is a plain
            # default to use, and otherwise honestly read as unknown.
            # No FK clause either - SQLite rejects one on ALTER, and the
            # application checks these references anyway.
            with engine.begin() as conn:
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                if backfill is not None:
                    conn.execute(
                        text(
                            f'UPDATE "{table.name}" SET "{column.name}" = :value '
                            f'WHERE "{column.name}" IS NULL'
                        ),
                        {"value": backfill},
                    )
            log.info(
                "added column %s.%s%s",
                table.name, column.name,
                f" (backfilled {backfill!r})" if backfill is not None else "",
            )


def _default_value(column):
    """The model's Python-side default, when it is a plain value we can store.

    Callables (`utcnow`, `list`) are deliberately not resolved: stamping every
    historical row with today's date would be worse than leaving them null.
    """
    default = column.default
    if default is None or default.is_callable or default.is_sequence:
        return None
    value = default.arg
    return value.value if isinstance(value, enum.Enum) else value


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional scope. Commits on success, rolls back on error."""
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency."""
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()


def get_state(s: Session, key: str, default: str | None = None) -> str | None:
    from crm.models import SyncState

    row = s.get(SyncState, key)
    return row.value if row and row.value is not None else default


def set_state(s: Session, key: str, value: str | None) -> None:
    from crm.models import SyncState

    row = s.get(SyncState, key)
    if row is None:
        s.add(SyncState(key=key, value=value))
    else:
        row.value = value
