"""Keep WAL bookkeeping available to read-only sidecars, without a transaction."""
from contextlib import contextmanager
from pathlib import Path
import sqlite3

@contextmanager
def sqlite_read_lifetime(database):
    if not database or database == ':memory:':
        yield
        return
    path=Path(database).resolve(strict=True)
    connection=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,check_same_thread=False)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        assert not connection.in_transaction
        yield connection
    finally:
        connection.close()
