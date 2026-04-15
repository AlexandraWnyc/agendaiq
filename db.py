"""
db.py — SQLite connection and initialization for OCA Agenda Intelligence v6
"""
import sqlite3
import logging
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger("oca-agent")

from paths import DB_PATH as _DEFAULT_DB_PATH
DB_PATH = _DEFAULT_DB_PATH


def set_db_path(path):
    global DB_PATH
    DB_PATH = Path(path)


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Create all tables if they do not exist, then run migrations."""
    from schema import DDL_STATEMENTS, MIGRATION_STATEMENTS
    with get_db() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        # Always run migrations so existing DBs get new tables
        for stmt in MIGRATION_STATEMENTS:
            try:
                conn.execute(stmt)
            except Exception:
                pass
    log.info(f"Database initialized: {DB_PATH}")
