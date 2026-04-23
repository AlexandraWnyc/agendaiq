"""
db.py — SQLite connection and initialization for OCA Agenda Intelligence v6

Designed to survive disk-full conditions on Render free tier:
- Reclaims space aggressively (WAL files, old PDFs, __pycache__)
- Skips DDL/migrations when core tables already exist
- Falls back to read-only mode if absolutely nothing can be written
- NEVER crashes on startup — the app must start so the user can fix things
"""
import sqlite3
import logging
import os
import glob
from pathlib import Path
from contextlib import contextmanager

log = logging.getLogger("oca-agent")

from paths import DB_PATH as _DEFAULT_DB_PATH
DB_PATH = _DEFAULT_DB_PATH

# Track if we're in degraded mode (disk full, writes may fail)
_disk_full_mode = False


def set_db_path(path):
    global DB_PATH
    DB_PATH = Path(path)


def _get_disk_free_mb() -> float:
    """Return free disk space in MB for the partition containing DB_PATH."""
    try:
        st = os.statvfs(str(DB_PATH.parent))
        return (st.f_bavail * st.f_frsize) / 1048576
    except Exception:
        return -1


def _try_reclaim_disk_space():
    """Aggressively free disk space. Called before any DB writes."""
    freed_total = 0

    # 1. WAL checkpoint — merges WAL back into main DB file
    try:
        c = sqlite3.connect(str(DB_PATH))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
        log.info("WAL checkpoint completed")
    except Exception as e:
        log.warning(f"WAL checkpoint failed: {e}")

    # 2. Delete WAL and SHM files (they get recreated automatically)
    for suffix in ("-wal", "-shm"):
        f = str(DB_PATH) + suffix
        if os.path.exists(f):
            try:
                sz = os.path.getsize(f)
                os.remove(f)
                freed_total += sz
                log.info(f"Deleted {f} ({sz / 1048576:.1f} MB)")
            except Exception as e:
                log.warning(f"Could not delete {f}: {e}")

    # 3. Delete ALL cached PDFs (can be re-downloaded)
    from paths import PDF_CACHE_DIR
    for pdf in glob.glob(str(PDF_CACHE_DIR / "*.pdf")):
        try:
            sz = os.path.getsize(pdf)
            os.remove(pdf)
            freed_total += sz
        except Exception:
            pass

    # 4. Clear __pycache__ directories
    from paths import PROJECT_DIR
    for root, dirs, files in os.walk(str(PROJECT_DIR)):
        if "__pycache__" in root:
            for f in files:
                fp = os.path.join(root, f)
                try:
                    sz = os.path.getsize(fp)
                    os.remove(fp)
                    freed_total += sz
                except Exception:
                    pass

    # 5. Delete old output/export files
    from paths import OUTPUT_DIR, EXPORTS_DIR
    for d in (OUTPUT_DIR, EXPORTS_DIR):
        for f in glob.glob(str(d / "*")):
            if os.path.isfile(f):
                try:
                    sz = os.path.getsize(f)
                    os.remove(f)
                    freed_total += sz
                except Exception:
                    pass

    if freed_total > 0:
        log.info(f"Total space reclaimed: {freed_total / 1048576:.1f} MB")

    free_mb = _get_disk_free_mb()
    log.info(f"Disk free after cleanup: {free_mb:.1f} MB")
    return freed_total


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    # Try WAL mode, fall back gracefully
    for mode in ("WAL", "DELETE", "MEMORY"):
        try:
            conn.execute(f"PRAGMA journal_mode={mode}")
            break
        except sqlite3.OperationalError:
            continue
    try:
        conn.execute("PRAGMA foreign_keys=ON")
    except Exception:
        pass
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


def _db_has_core_tables() -> bool:
    """Read-only check: do core tables exist? Uses no disk space."""
    try:
        c = sqlite3.connect(str(DB_PATH))
        c.row_factory = sqlite3.Row
        tables = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        c.close()
        return {"matters", "meetings", "appearances"}.issubset(tables)
    except Exception:
        return False


def _backup_db_before_migration(tag: str = "pre-migration") -> Path | None:
    """Copy the current DB file to DB_PATH.parent/backups/<tag>_<timestamp>.db
    before running a schema change. Returns the backup Path or None on
    failure. Never raises — backup is a courtesy, not a blocker."""
    try:
        if not DB_PATH.exists():
            return None
        from datetime import datetime as _dt
        backup_dir = DB_PATH.parent / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
        dst = backup_dir / f"{tag}_{stamp}_{DB_PATH.name}"
        # Use SQLite's online backup so we grab a consistent snapshot
        # even if the app is holding connections.
        with sqlite3.connect(str(DB_PATH)) as src_conn, \
                sqlite3.connect(str(dst)) as dst_conn:
            src_conn.backup(dst_conn)
        # Prune old backups: keep the 10 most recent matching this tag
        existing = sorted(
            backup_dir.glob(f"{tag}_*_{DB_PATH.name}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old in existing[10:]:
            try:
                old.unlink()
            except Exception:
                pass
        log.info(f"DB backup written: {dst} ({dst.stat().st_size:,} bytes)")
        return dst
    except Exception as e:
        log.warning(f"DB backup failed (continuing anyway): {e}")
        return None


def _migration_marker_file() -> Path:
    """Marker file tracking which migration blocks have already run.
    Prevents redundant backups on every app start."""
    return DB_PATH.parent / f".{DB_PATH.stem}_migrations.marker"


def _case_layer_migrated() -> bool:
    """Fast check: does the cases table already exist?"""
    try:
        c = sqlite3.connect(str(DB_PATH))
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='cases'"
        ).fetchone()
        c.close()
        return row is not None
    except Exception:
        return False


def init_db():
    """Initialize database. NEVER raises — the app must always start.

    Strategy:
    1. Reclaim disk space aggressively
    2. If core tables exist → skip DDL, try migrations (ignore failures)
    3. If fresh DB → create tables (will fail if disk truly full, but
       that means there's no data to serve anyway)
    4. Any unhandled error → log warning, continue startup

    Case-layer migration (April 2026): if the existing DB doesn't yet
    have the `cases` table, take a timestamped backup before applying
    migrations.
    """
    global _disk_full_mode

    try:
        # Step 1: Reclaim space
        if DB_PATH.exists():
            _try_reclaim_disk_space()

        # Step 2: Check existing tables
        already_initialized = DB_PATH.exists() and _db_has_core_tables()

        # Pre-migration backup: only when we're about to run a
        # schema-changing migration (cases layer absent but core tables
        # present). Fresh DBs don't need backup, fully-migrated DBs
        # don't need backup, only in-between does.
        if already_initialized and not _case_layer_migrated():
            log.info("Case-layer migration detected — taking DB backup first")
            _backup_db_before_migration(tag="pre-case-layer")

        if already_initialized:
            # Existing DB — try migrations, skip on any disk error
            try:
                from schema import MIGRATION_STATEMENTS
                with get_db() as conn:
                    for stmt in MIGRATION_STATEMENTS:
                        try:
                            conn.execute(stmt)
                        except Exception:
                            pass
                    # Seed default organization if missing
                    _seed_default_org(conn)
                    # Normalize any legacy M/D/YYYY dates to ISO format
                    _normalize_meeting_dates(conn)
            except Exception as e:
                _disk_full_mode = True
                log.warning(f"Migrations skipped (disk issue): {e}")
            log.info(f"Database ready (existing): {DB_PATH}")
        else:
            # Fresh DB — must create tables
            from schema import DDL_STATEMENTS, MIGRATION_STATEMENTS
            with get_db() as conn:
                for stmt in DDL_STATEMENTS:
                    conn.execute(stmt)
                for stmt in MIGRATION_STATEMENTS:
                    try:
                        conn.execute(stmt)
                    except Exception:
                        pass
                _seed_default_org(conn)
                _normalize_meeting_dates(conn)
            log.info(f"Database initialized (fresh): {DB_PATH}")

    except Exception as e:
        # NEVER crash here — the app must start
        _disk_full_mode = True
        log.error(f"Database init encountered error (app will start anyway): {e}")
        log.error("Some features may not work until disk space is freed.")
        log.error(f"Current disk free: {_get_disk_free_mb():.1f} MB")


def _seed_default_org(conn):
    """Ensure the default Miami-Dade organization (id=1) exists.
    All existing data rows have org_id DEFAULT 1, so this org must exist
    for FK integrity. Idempotent — skips if already present."""
    try:
        row = conn.execute(
            "SELECT id FROM organizations WHERE id=1"
        ).fetchone()
        if not row:
            from datetime import datetime as _dt
            now = _dt.utcnow().isoformat()
            conn.execute(
                """INSERT INTO organizations (id, name, slug, settings, is_active, created_at, updated_at)
                   VALUES (1, 'Miami-Dade County OCA', 'miami-dade-oca', '{}', 1, ?, ?)""",
                (now, now)
            )
            log.info("Seeded default organization: Miami-Dade County OCA (id=1)")
    except Exception as e:
        log.warning(f"Could not seed default org (may not exist yet): {e}")


def _normalize_meeting_dates(conn):
    """Convert any M/D/YYYY meeting_date values to ISO YYYY-MM-DD and merge
    duplicate meetings that only differ by date format."""
    import re
    from datetime import datetime as dt

    FK_TABLES = ["workflow_history", "artifacts", "chat_messages"]

    rows = conn.execute("SELECT id, body_name, meeting_date FROM meetings").fetchall()
    for r in rows:
        md = r["meeting_date"]
        m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", md)
        if not m:
            continue
        iso = dt.strptime(md, "%m/%d/%Y").strftime("%Y-%m-%d")
        dup = conn.execute(
            "SELECT id FROM meetings WHERE body_name=? AND meeting_date=? AND id!=?",
            (r["body_name"], iso, r["id"])
        ).fetchone()
        if dup:
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
                    for tbl in FK_TABLES:
                        try:
                            conn.execute(
                                f"UPDATE {tbl} SET appearance_id=? WHERE appearance_id=?",
                                (already["id"], oa["id"])
                            )
                        except Exception:
                            pass
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
