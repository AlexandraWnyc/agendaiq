"""
workflow.py — Workflow update helpers + audit trail for OCA Agenda Intelligence v6
"""
import logging
from datetime import datetime
from db import get_db
from schema import WORKFLOW_STATUSES
from utils import now_iso
from repository import get_appearance_by_id

log = logging.getLogger("oca-agent")


# ── Audit trail ───────────────────────────────────────────────

def log_history(appearance_id: int, action: str, old_value: str = None,
                new_value: str = None, note: str = None, changed_by: str = None):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO workflow_history
               (appearance_id, changed_by, action, old_value, new_value, note, changed_at)
               VALUES (?,?,?,?,?,?,?)""",
            (appearance_id, changed_by or "system", action,
             old_value, new_value, note, now_iso())
        )


def get_history(appearance_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM workflow_history
               WHERE appearance_id=?
               ORDER BY changed_at ASC""",
            (appearance_id,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────

def _require_appearance(appearance_id: int) -> dict:
    app = get_appearance_by_id(appearance_id)
    if not app:
        raise ValueError(f"Appearance {appearance_id} not found.")
    return app


# ── Workflow actions ──────────────────────────────────────────

def set_workflow_status(appearance_id: int, status: str, changed_by: str = None):
    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {WORKFLOW_STATUSES}")
    app = _require_appearance(appearance_id)
    old_status = app.get("workflow_status")
    now = now_iso()
    completion = now if status == "Finalized" else None

    with get_db() as conn:
        if completion:
            conn.execute(
                """UPDATE appearances SET workflow_status=?, completion_date=?,
                   updated_at=? WHERE id=? AND (completion_date IS NULL OR completion_date='')""",
                (status, completion, now, appearance_id)
            )
        else:
            conn.execute(
                "UPDATE appearances SET workflow_status=?, updated_at=? WHERE id=?",
                (status, now, appearance_id)
            )
    log_history(appearance_id, "status_change", old_status, status, changed_by=changed_by)
    log.info(f"  Appearance {appearance_id} → status: {status}")


def assign_appearance(appearance_id: int, assigned_to: str, changed_by: str = None):
    app = _require_appearance(appearance_id)
    old = app.get("assigned_to") or "unassigned"
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET assigned_to=?, assigned_date=?,
               workflow_status=CASE WHEN workflow_status='New' THEN 'Assigned' ELSE workflow_status END,
               updated_at=? WHERE id=?""",
            (assigned_to, now, now, appearance_id)
        )
    log_history(appearance_id, "assigned", old, assigned_to, changed_by=changed_by)
    log.info(f"  Appearance {appearance_id} → assigned to: {assigned_to}")


def set_reviewer(appearance_id: int, reviewer: str, changed_by: str = None):
    app = _require_appearance(appearance_id)
    old = app.get("reviewer") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET reviewer=?, updated_at=? WHERE id=?",
            (reviewer, now_iso(), appearance_id)
        )
    log_history(appearance_id, "reviewer_set", old, reviewer, changed_by=changed_by)


def set_due_date(appearance_id: int, due_date: str, changed_by: str = None):
    app = _require_appearance(appearance_id)
    old = app.get("due_date") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET due_date=?, updated_at=? WHERE id=?",
            (due_date, now_iso(), appearance_id)
        )
    log_history(appearance_id, "due_date_set", old, due_date, changed_by=changed_by)


def set_priority(appearance_id: int, priority: str, changed_by: str = None):
    app = _require_appearance(appearance_id)
    old = app.get("priority") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET priority=?, updated_at=? WHERE id=?",
            (priority, now_iso(), appearance_id)
        )
    log_history(appearance_id, "priority_set", old, priority, changed_by=changed_by)


def append_working_notes(appearance_id: int, note: str, replace: bool = False,
                          changed_by: str = None):
    app = _require_appearance(appearance_id)
    if replace:
        new_notes = note
    else:
        existing = app.get("analyst_working_notes") or ""
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        by = f" ({changed_by})" if changed_by else ""
        sep = "\n\n" if existing else ""
        new_notes = f"{existing}{sep}[{ts}{by}] {note}"
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET analyst_working_notes=?,
               analyst_notes_updated_at=?, analyst_notes_updated_by=?,
               updated_at=? WHERE id=?""",
            (new_notes, now, changed_by or "", now, appearance_id)
        )
    log_history(appearance_id, "working_note_added", note=note[:200], changed_by=changed_by)


def append_reviewer_notes(appearance_id: int, note: str, replace: bool = False,
                           changed_by: str = None):
    app = _require_appearance(appearance_id)
    if replace:
        new_notes = note
    else:
        existing = app.get("reviewer_notes") or ""
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
        by = f" ({changed_by})" if changed_by else ""
        sep = "\n\n" if existing else ""
        new_notes = f"{existing}{sep}[{ts}{by}] {note}"
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET reviewer_notes=?,
               reviewer_notes_updated_at=?, reviewer_notes_updated_by=?,
               updated_at=? WHERE id=?""",
            (new_notes, now, changed_by or "", now, appearance_id)
        )
    log_history(appearance_id, "reviewer_note_added", note=note[:200], changed_by=changed_by)


def update_ai_summary(appearance_id: int, summary: str, watch_points: str = None,
                       changed_by: str = None):
    """Allow analysts to edit the AI-generated summary inline."""
    app = _require_appearance(appearance_id)
    now = now_iso()
    with get_db() as conn:
        if watch_points is not None:
            conn.execute(
                """UPDATE appearances SET ai_summary_for_appearance=?,
                   watch_points_for_appearance=?, updated_at=? WHERE id=?""",
                (summary, watch_points, now, appearance_id)
            )
        else:
            conn.execute(
                "UPDATE appearances SET ai_summary_for_appearance=?, updated_at=? WHERE id=?",
                (summary, now, appearance_id)
            )
    log_history(appearance_id, "ai_summary_edited",
                note="Summary edited by analyst", changed_by=changed_by)


def set_finalized_brief(appearance_id: int, brief_text: str, changed_by: str = None):
    import os
    from pathlib import Path
    _require_appearance(appearance_id)
    if os.path.exists(brief_text):
        brief_text = Path(brief_text).read_text(encoding="utf-8")
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET finalized_brief=?,
               finalized_brief_updated_at=?, finalized_brief_updated_by=?,
               updated_at=? WHERE id=?""",
            (brief_text, now, changed_by or "", now, appearance_id)
        )
    log_history(appearance_id, "brief_finalized",
                note="Finalized brief set", changed_by=changed_by)


# ── Alert queries ─────────────────────────────────────────────

def get_overdue_appearances() -> list[dict]:
    """Return appearances past their due date that are not yet Finalized/Archived."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.file_number, m.short_title, m.sponsor,
                      mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.due_date IS NOT NULL AND a.due_date != ''
                 AND a.due_date < ?
                 AND a.workflow_status NOT IN ('Finalized','Archived')
               ORDER BY a.due_date ASC""",
            (today,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_due_soon_appearances(days: int = 7) -> list[dict]:
    """Return appearances due within the next N days."""
    from datetime import timedelta
    today = datetime.utcnow().date()
    cutoff = (today + timedelta(days=days)).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.file_number, m.short_title, m.sponsor,
                      mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.due_date >= ? AND a.due_date <= ?
                 AND a.workflow_status NOT IN ('Finalized','Archived')
               ORDER BY a.due_date ASC""",
            (today_str, cutoff)
        ).fetchall()
        return [dict(r) for r in rows]


def get_unassigned_appearances() -> list[dict]:
    """Return New items with no assignee."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.file_number, m.short_title,
                      mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.workflow_status = 'New'
                 AND (a.assigned_to IS NULL OR a.assigned_to = '')
               ORDER BY mt.meeting_date DESC""",
        ).fetchall()
        return [dict(r) for r in rows]
