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


# ── Audit trail ───────────────────────────────────────────────

def log_history(appearance_id: int, action: str, old_value: str = None,
                new_value: str = None, note: str = None, changed_by: str = None,
                org_id=None):
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        conn.execute(
            """INSERT INTO workflow_history
               (appearance_id, changed_by, action, old_value, new_value, note, changed_at, org_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (appearance_id, changed_by or "system", action,
             old_value, new_value, note, now_iso(), oid)
        )


def get_history(appearance_id: int, org_id=None) -> list[dict]:
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM workflow_history
               WHERE appearance_id=? AND org_id = ?
               ORDER BY changed_at ASC""",
            (appearance_id, oid)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Helpers ───────────────────────────────────────────────────

def _require_appearance(appearance_id: int, org_id=None) -> dict:
    oid = _resolve_org_id(org_id)
    app = get_appearance_by_id(appearance_id, org_id=oid)
    if not app:
        raise ValueError(f"Appearance {appearance_id} not found.")
    return app


# ── Workflow actions ──────────────────────────────────────────

def set_workflow_status(appearance_id: int, status: str, changed_by: str = None,
                        org_id=None):
    oid = _resolve_org_id(org_id)
    if status not in WORKFLOW_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Valid: {WORKFLOW_STATUSES}")
    app = _require_appearance(appearance_id, org_id=oid)
    old_status = app.get("workflow_status")
    now = now_iso()
    completion = now if status == "Finalized" else None

    with get_db() as conn:
        if completion:
            conn.execute(
                """UPDATE appearances SET workflow_status=?, completion_date=?,
                   updated_at=? WHERE id=? AND org_id = ? AND (completion_date IS NULL OR completion_date='')""",
                (status, completion, now, appearance_id, oid)
            )
        else:
            conn.execute(
                "UPDATE appearances SET workflow_status=?, updated_at=? WHERE id=? AND org_id = ?",
                (status, now, appearance_id, oid)
            )
    log_history(appearance_id, "status_change", old_status, status, changed_by=changed_by, org_id=oid)
    log.info(f"  Appearance {appearance_id} → status: {status}")


def assign_appearance(
    appearance_id: int,
    assigned_to: str,
    changed_by: str = None,
    force: bool = False,
    org_id=None,
):
    """Assign an appearance to a researcher, enforcing Case coherence.

    Policy (Session 4): all items in one Case must share an assignee,
    and the same rule extends to companion Cases (CDMP + Zoning for
    same project). If this assignment would create a mismatch, raises
    CaseAssignmentConflict unless force=True.

    Callers who want to bypass the check (e.g. admins doing bulk
    reassignment) pass force=True.
    """
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
    old = app.get("assigned_to") or "unassigned"

    # Coherence check — lazily imported to avoid a hard dependency
    # if cases.py is missing or failing to load (keep workflow resilient)
    if not force and assigned_to and assigned_to.strip():
        try:
            from cases import check_case_assignment_coherence
            report = check_case_assignment_coherence(appearance_id, assigned_to)
            if not report["ok"]:
                raise CaseAssignmentConflict(
                    appearance_id=appearance_id,
                    proposed=assigned_to,
                    report=report,
                )
        except CaseAssignmentConflict:
            raise
        except Exception as _e:
            log.warning(f"  case-coherence check failed (proceeding anyway): {_e}")

    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET assigned_to=?, assigned_date=?,
               reviewer=CASE WHEN reviewer IS NULL OR reviewer='' THEN 'Rolando' ELSE reviewer END,
               workflow_status=CASE WHEN workflow_status='New' THEN 'Assigned' ELSE workflow_status END,
               updated_at=? WHERE id=? AND org_id = ?""",
            (assigned_to, now, now, appearance_id, oid)
        )
    log_history(appearance_id, "assigned", old, assigned_to, changed_by=changed_by, org_id=oid)
    log.info(f"  Appearance {appearance_id} → assigned to: {assigned_to}"
             f"{' (forced)' if force else ''}")


class CaseAssignmentConflict(Exception):
    """Raised when assign_appearance would violate Case-coherence.
    Carries the conflict report so callers (e.g. HTTP handlers) can
    render a useful error to the user."""
    def __init__(self, appearance_id: int, proposed: str, report: dict):
        self.appearance_id = appearance_id
        self.proposed = proposed
        self.report = report
        conflicts = report.get("conflicts", [])
        summary = "; ".join(
            f"File#{c.get('file_number')} assigned to "
            f"{c.get('assigned_to')!r} ({c.get('scope')})"
            for c in conflicts[:3]
        ) or "case coherence conflict"
        super().__init__(
            f"Cannot assign appearance {appearance_id} to {proposed!r}: "
            f"{summary}"
        )


def set_reviewer(appearance_id: int, reviewer: str, changed_by: str = None,
                 org_id=None):
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
    old = app.get("reviewer") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET reviewer=?, updated_at=? WHERE id=? AND org_id = ?",
            (reviewer, now_iso(), appearance_id, oid)
        )
    log_history(appearance_id, "reviewer_set", old, reviewer, changed_by=changed_by, org_id=oid)


def set_due_date(appearance_id: int, due_date: str, changed_by: str = None,
                 org_id=None):
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
    old = app.get("due_date") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET due_date=?, updated_at=? WHERE id=? AND org_id = ?",
            (due_date, now_iso(), appearance_id, oid)
        )
    log_history(appearance_id, "due_date_set", old, due_date, changed_by=changed_by, org_id=oid)


def set_priority(appearance_id: int, priority: str, changed_by: str = None,
                 org_id=None):
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
    old = app.get("priority") or ""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET priority=?, updated_at=? WHERE id=? AND org_id = ?",
            (priority, now_iso(), appearance_id, oid)
        )
    log_history(appearance_id, "priority_set", old, priority, changed_by=changed_by, org_id=oid)


def append_working_notes(appearance_id: int, note: str, replace: bool = False,
                          changed_by: str = None, org_id=None):
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
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
               updated_at=? WHERE id=? AND org_id = ?""",
            (new_notes, now, changed_by or "", now, appearance_id, oid)
        )
    log_history(appearance_id, "working_note_added", note=note[:200], changed_by=changed_by, org_id=oid)


def append_reviewer_notes(appearance_id: int, note: str, replace: bool = False,
                           changed_by: str = None, org_id=None):
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
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
               updated_at=? WHERE id=? AND org_id = ?""",
            (new_notes, now, changed_by or "", now, appearance_id, oid)
        )
    log_history(appearance_id, "reviewer_note_added", note=note[:200], changed_by=changed_by, org_id=oid)


def replace_working_notes(appearance_id: int, notes: str, changed_by: str = None,
                          org_id=None):
    oid = _resolve_org_id(org_id)
    append_working_notes(appearance_id, notes, replace=True, changed_by=changed_by, org_id=oid)

def replace_reviewer_notes(appearance_id: int, notes: str, changed_by: str = None,
                           org_id=None):
    oid = _resolve_org_id(org_id)
    append_reviewer_notes(appearance_id, notes, replace=True, changed_by=changed_by, org_id=oid)

def update_ai_summary(appearance_id: int, summary: str, watch_points: str = None,
                       changed_by: str = None, org_id=None):
    """Allow analysts to edit the AI-generated summary inline."""
    oid = _resolve_org_id(org_id)
    app = _require_appearance(appearance_id, org_id=oid)
    now = now_iso()
    with get_db() as conn:
        if watch_points is not None:
            conn.execute(
                """UPDATE appearances SET ai_summary_for_appearance=?,
                   watch_points_for_appearance=?, updated_at=? WHERE id=? AND org_id = ?""",
                (summary, watch_points, now, appearance_id, oid)
            )
        else:
            conn.execute(
                "UPDATE appearances SET ai_summary_for_appearance=?, updated_at=? WHERE id=? AND org_id = ?",
                (summary, now, appearance_id, oid)
            )
    log_history(appearance_id, "ai_summary_edited",
                note="Summary edited by analyst", changed_by=changed_by, org_id=oid)


def set_finalized_brief(appearance_id: int, brief_text: str, changed_by: str = None,
                        org_id=None):
    import os
    from pathlib import Path
    oid = _resolve_org_id(org_id)
    _require_appearance(appearance_id, org_id=oid)
    if os.path.exists(brief_text):
        brief_text = Path(brief_text).read_text(encoding="utf-8")
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE appearances SET finalized_brief=?,
               finalized_brief_updated_at=?, finalized_brief_updated_by=?,
               updated_at=? WHERE id=? AND org_id = ?""",
            (brief_text, now, changed_by or "", now, appearance_id, oid)
        )
    log_history(appearance_id, "brief_finalized",
                note="Finalized brief set", changed_by=changed_by, org_id=oid)


# ── Alert queries ─────────────────────────────────────────────

def get_overdue_appearances(org_id=None) -> list[dict]:
    """Return appearances past their due date that are not yet Finalized/Archived."""
    oid = _resolve_org_id(org_id)
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
                 AND a.org_id = ?
               ORDER BY a.due_date ASC""",
            (today, oid)
        ).fetchall()
        return [dict(r) for r in rows]


def get_due_soon_appearances(days: int = 7, org_id=None) -> list[dict]:
    """Return appearances due within the next N days."""
    oid = _resolve_org_id(org_id)
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
                 AND a.org_id = ?
               ORDER BY a.due_date ASC""",
            (today_str, cutoff, oid)
        ).fetchall()
        return [dict(r) for r in rows]


def get_unassigned_appearances(org_id=None) -> list[dict]:
    """Return New items with no assignee."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.file_number, m.short_title,
                      mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.workflow_status = 'New'
                 AND (a.assigned_to IS NULL OR a.assigned_to = '')
                 AND a.org_id = ?
               ORDER BY mt.meeting_date DESC""",
            (oid,)
        ).fetchall()
        return [dict(r) for r in rows]
