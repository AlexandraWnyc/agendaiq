"""
search.py — Search and history functions for OCA Agenda Intelligence v6
"""
import logging
from db import get_db

log = logging.getLogger("oca-agent")


def _resolve_org_id(org_id=None) -> int:
    if org_id is not None:
        return org_id
    try:
        from flask import g
        if hasattr(g, 'org_id') and g.org_id is not None:
            return g.org_id
    except (ImportError, RuntimeError):
        pass
    return 1


def search_by_file_number(file_number: str, org_id=None) -> dict | None:
    """Return matter + all appearances for an exact file number."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        matter = conn.execute(
            "SELECT * FROM matters WHERE file_number=? AND org_id = ?", (str(file_number), oid)
        ).fetchone()
        if not matter:
            return None
        matter = dict(matter)
        apps = conn.execute(
            """SELECT a.*, mt.meeting_date, mt.body_name, mt.meeting_type
               FROM appearances a
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.matter_id=? AND a.org_id = ?
               ORDER BY mt.meeting_date ASC, a.id ASC""",
            (matter["id"], oid)
        ).fetchall()
        matter["appearances"] = [dict(r) for r in apps]

        # Include legislative timeline events for the status ladder
        try:
            timeline = conn.execute(
                """SELECT * FROM matter_timeline
                   WHERE matter_id=? AND org_id = ?
                   ORDER BY event_date ASC""",
                (matter["id"], oid)
            ).fetchall()
            matter["timeline"] = [dict(r) for r in timeline]
        except Exception:
            matter["timeline"] = []

        return matter


def search_by_keyword(keyword: str, limit: int = 20, org_id=None) -> list[dict]:
    """
    FTS5 full-text search across appearance content.
    Falls back to LIKE search if FTS5 query fails.
    Returns list of (matter, appearance, meeting) dicts.
    """
    oid = _resolve_org_id(org_id)
    results = []
    with get_db() as conn:
        # Try FTS5 first
        try:
            rows = conn.execute(
                """SELECT a.*, m.short_title, m.full_title, m.file_number as matter_file,
                          m.sponsor, m.current_status,
                          mt.meeting_date, mt.body_name
                   FROM appearances_fts fts
                   JOIN appearances a ON a.id = fts.rowid
                   JOIN matters m ON m.id = a.matter_id
                   JOIN meetings mt ON mt.id = a.meeting_id
                   WHERE appearances_fts MATCH ?
                     AND a.org_id = ?
                   ORDER BY rank
                   LIMIT ?""",
                (keyword, oid, limit)
            ).fetchall()
            results = [dict(r) for r in rows]
        except Exception:
            # FTS failed — fallback to LIKE
            like = f"%{keyword}%"
            rows = conn.execute(
                """SELECT a.*, m.short_title, m.full_title, m.file_number as matter_file,
                          m.sponsor, m.current_status,
                          mt.meeting_date, mt.body_name
                   FROM appearances a
                   JOIN matters m ON m.id = a.matter_id
                   JOIN meetings mt ON mt.id = a.meeting_id
                   WHERE (m.short_title LIKE ?
                      OR m.full_title LIKE ?
                      OR a.ai_summary_for_appearance LIKE ?
                      OR a.watch_points_for_appearance LIKE ?
                      OR a.analyst_working_notes LIKE ?
                      OR a.reviewer_notes LIKE ?
                      OR a.appearance_title LIKE ?)
                     AND a.org_id = ?
                   ORDER BY mt.meeting_date DESC
                   LIMIT ?""",
                (like, like, like, like, like, like, like, oid, limit)
            ).fetchall()
            results = [dict(r) for r in rows]
    return results


def search_by_sponsor(sponsor: str, limit: int = 50, org_id=None) -> list[dict]:
    oid = _resolve_org_id(org_id)
    like = f"%{sponsor}%"
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.file_number, m.short_title, m.sponsor, m.current_status,
                      mt.meeting_date, mt.body_name, a.workflow_status, a.id as appearance_id
               FROM matters m
               JOIN appearances a ON a.matter_id = m.id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE m.sponsor LIKE ?
                 AND a.org_id = ?
               ORDER BY mt.meeting_date DESC
               LIMIT ?""",
            (like, oid, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_history(file_number: str, org_id=None) -> dict | None:
    """Return full matter history with all appearances, chronological."""
    oid = _resolve_org_id(org_id)
    return search_by_file_number(file_number, org_id=oid)


def list_all_matters(limit: int = 100, offset: int = 0, org_id=None) -> list[dict]:
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.*, COUNT(a.id) as appearance_count
               FROM matters m
               LEFT JOIN appearances a ON a.matter_id = m.id
               WHERE m.org_id = ?
               GROUP BY m.id
               ORDER BY m.last_seen_date DESC, m.updated_at DESC
               LIMIT ? OFFSET ?""",
            (oid, limit, offset)
        ).fetchall()
        return [dict(r) for r in rows]


def list_appearances_by_status(status: str, limit: int = 50, org_id=None) -> list[dict]:
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.file_number, m.short_title, m.sponsor,
                      mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.workflow_status = ?
                 AND a.org_id = ?
               ORDER BY mt.meeting_date DESC
               LIMIT ?""",
            (status, oid, limit)
        ).fetchall()
        return [dict(r) for r in rows]


def get_dashboard_stats(org_id=None) -> dict:
    """Return summary counts for the dashboard."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        total_matters = conn.execute(
            "SELECT COUNT(*) FROM matters WHERE org_id = ?", (oid,)
        ).fetchone()[0]
        total_appearances = conn.execute(
            "SELECT COUNT(*) FROM appearances WHERE org_id = ?", (oid,)
        ).fetchone()[0]
        total_meetings = conn.execute(
            "SELECT COUNT(*) FROM meetings WHERE org_id = ?", (oid,)
        ).fetchone()[0]
        by_status = conn.execute(
            "SELECT workflow_status, COUNT(*) as cnt FROM appearances WHERE org_id = ? GROUP BY workflow_status",
            (oid,)
        ).fetchall()
        recent = conn.execute(
            """SELECT m.file_number, m.short_title, mt.meeting_date, mt.body_name,
                      a.workflow_status, a.id as appearance_id
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.org_id = ?
               ORDER BY a.created_at DESC LIMIT 10""",
            (oid,)
        ).fetchall()
        return {
            "total_matters": total_matters,
            "total_appearances": total_appearances,
            "total_meetings": total_meetings,
            "by_status": {r["workflow_status"]: r["cnt"] for r in by_status},
            "recent": [dict(r) for r in recent],
        }
