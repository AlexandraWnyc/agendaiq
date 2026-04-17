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
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        # If WAL fails (disk full), try to recover by checkpointing
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.execute("PRAGMA journal_mode=WAL")
        except Exception:
            log.warning("Could not set WAL mode — disk may be full")
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

    # Checkpoint WAL first to reclaim space (helps recover from disk-full)
    if DB_PATH.exists():
        try:
            c = sqlite3.connect(str(DB_PATH))
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            c.close()
            log.info("WAL checkpoint completed on startup")
        except Exception as e:
            log.warning(f"WAL checkpoint failed on startup: {e}")

    with get_db() as conn:
        for stmt in DDL_STATEMENTS:
            conn.execute(stmt)
        # Always run migrations so existing DBs get new tables
        for stmt in MIGRATION_STATEMENTS:
            try:
                conn.execute(stmt)
            except Exception:
                pass
        # One-time fix: normalize M/D/YYYY dates → YYYY-MM-DD and merge dups
        _normalize_meeting_dates(conn)
    log.info(f"Database initialized: {DB_PATH}")


def _normalize_meeting_dates(conn):
    """Convert any M/D/YYYY meeting_date values to ISO YYYY-MM-DD and merge
    duplicate meetings that only differ by date format."""
    import re
    from datetime import datetime as dt

    # Tables that have a foreign key referencing appearances(id)
    FK_TABLES = ["workflow_history", "artifacts", "chat_messages"]

    rows = conn.execute("SELECT id, body_name, meeting_date FROM meetings").fetchall()
    for r in rows:
        md = r["meeting_date"]
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", md)
        if not m:
            continue
        iso = dt.strptime(md, "%m/%d/%Y").strftime("%Y-%m-%d")
        # Check if an ISO-dated duplicate already exists
        dup = conn.execute(
            "SELECT id FROM meetings WHERE body_name=? AND meeting_date=? AND id!=?",
            (r["body_name"], iso, r["id"])
        ).fetchone()
        if dup:
            # Merge: reassign appearances from old meeting to canonical one,
            # but skip any that would create a duplicate (same matter_id+meeting_id)
            old_apps = conn.execute(
                "SELECT id, matter_id FROM appearances WHERE meeting_id=?",
                (r["id"],)
            ).fetchall()
            for oa in old_apps:
                already = conn.execute(
                    "SELECT id FROM appearances WHERE matter_id=? AND meeting_id=?",
                    (oa["matter_id"], dup["id"])
                ).fetchone()
                if already:
                    # Reassign child records to the surviving appearance, then delete
                    for tbl in FK_TABLES:
                        try:
                            conn.execute(
                                f"UPDATE {tbl} SET appearance_id=? WHERE appearance_id=?",
                                (already["id"], oa["id"])
                            )
                        except Exception:
                            pass
                    # Also reassign prior_appearance_id references in other appearances
                    try:
                        conn.execute(
                            "UPDATE appearances SET prior_appearance_id=? WHERE prior_appearance_id=?",
                            (already["id"], oa["id"])
                        )
                    except Exception:
                        pass
                    conn.execute("DELETE FROM appearances WHERE id=?", (oa["id"],))
                else:
                    conn.execute(
                        "UPDATE appearances SET meeting_id=? WHERE id=?",
                        (dup["id"], oa["id"])
                    )
            # Also reassign any artifacts linked at meeting level
            try:
                conn.execute(
                    "UPDATE artifacts SET meeting_id=? WHERE meeting_id=?",
                    (dup["id"], r["id"])
                )
            except Exception:
                pass
            conn.execute("DELETE FROM meetings WHERE id=?", (r["id"],))
            log.info(f"  Merged meeting {r['id']} ({md}) -> {dup['id']} ({iso})")
        else:
            conn.execute(
                "UPDATE meetings SET meeting_date=? WHERE id=?",
                (iso, r["id"])
            )
            log.info(f"  Normalized meeting {r['id']} date: {md} -> {iso}")
