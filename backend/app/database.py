from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from .config import settings


class Base(DeclarativeBase):
    pass


is_sqlite = settings.database_url.startswith("sqlite")
connect_args = {"check_same_thread": False} if is_sqlite else {}
# Background publishing and readback jobs can legitimately spend minutes in an
# upstream API.  SQLite's default QueuePool therefore turns slow platform calls
# into unrelated page/API timeouts.  A short-lived connection per Session keeps
# the WAL database bounded by the worker limits without an artificial 15-slot
# pool bottleneck.
engine_options = {"connect_args": connect_args, "pool_pre_ping": True}
if is_sqlite:
    # An in-memory SQLite database exists only for the lifetime of one DBAPI
    # connection, so tests/dev smoke checks must share that connection.  The
    # production file database uses short-lived connections to avoid holding a
    # small QueuePool slot while an upstream platform request is in flight.
    engine_options["poolclass"] = StaticPool if ":memory:" in settings.database_url else NullPool
engine = create_engine(settings.database_url, **engine_options)

if is_sqlite:
    @event.listens_for(engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
