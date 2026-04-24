"""
lifecycle_manager.py — Meeting lifecycle automation for AgendaIQ

Manages the full lifecycle of a meeting from preliminary agenda through
final briefing delivery:

  1. PRELIM_SCRAPED  — Preliminary agenda analyzed, items assigned
  2. FINAL_DETECTED  — Final/official agenda published, changes detected
  3. MEETING_COMPLETE — Meeting date has passed
  4. TRANSCRIPT_FETCHED — Recording found and transcribed
  5. FULLY_BRIEFED   — All items finalized with full context

Automated transitions:
  - Prelim → Final: agenda_monitor detects final agenda, re-analyzes changes
  - Any → Meeting Complete: meeting_date < today
  - Meeting Complete → Transcript: auto-fetch from Granicus/YouTube
  - Transcript → Fully Briefed: all items have status=Finalized

Each transition generates a notification so the team knows what happened
and what needs attention.
"""

import logging
from datetime import datetime, timedelta
from db import get_db
from utils import now_iso

log = logging.getLogger("oca-agent")

LIFECYCLE_STAGES = [
    "prelim_scraped",
    "final_detected",
    "meeting_complete",
    "transcript_fetched",
    "fully_briefed",
]

STAGE_LABELS = {
    "prelim_scraped": "Preliminary Agenda Analyzed",
    "final_detected": "Final Agenda Published",
    "meeting_complete": "Meeting Complete",
    "transcript_fetched": "Transcript Available",
    "fully_briefed": "Fully Briefed",
}

STAGE_COLORS = {
    "prelim_scraped": "#f59e0b",   # amber
    "final_detected": "#3b82f6",   # blue
    "meeting_complete": "#8b5cf6", # purple
    "transcript_fetched": "#059669", # green
    "fully_briefed": "#16a34a",    # dark green
}


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


def get_meeting_lifecycle(meeting_id: int, org_id=None) -> dict:
    """Get the current lifecycle stage and what needs to happen next."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        m = conn.execute(
            """SELECT id, body_name, meeting_date, meeting_type,
                      agenda_status, lifecycle_stage,
                      transcript_checked_at, transcript_check_count
               FROM meetings WHERE id=? AND org_id=?""",
            (meeting_id, oid)
        ).fetchone()
        if not m:
            return {"error": "meeting not found"}

        m = dict(m)
        stage = m.get("lifecycle_stage") or "prelim_scraped"
        meeting_date = m.get("meeting_date", "")
        today = datetime.utcnow().strftime("%Y-%m-%d")

        # Check if meeting has passed
        meeting_passed = meeting_date and meeting_date <= today

        # Check transcript status
        has_transcript = False
        transcript_count = conn.execute(
            """SELECT COUNT(*) as cnt FROM appearances
               WHERE meeting_id=? AND org_id=?
               AND transcript_analysis IS NOT NULL
               AND LENGTH(transcript_analysis) > 10""",
            (meeting_id, oid)
        ).fetchone()["cnt"]
        has_transcript = transcript_count > 0

        # Check finalized status
        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM appearances WHERE meeting_id=? AND org_id=?",
            (meeting_id, oid)
        ).fetchone()["cnt"]
        finalized = conn.execute(
            """SELECT COUNT(*) as cnt FROM appearances
               WHERE meeting_id=? AND org_id=?
               AND workflow_status='Finalized'""",
            (meeting_id, oid)
        ).fetchone()["cnt"]

        # Check agenda type
        is_final = (m.get("agenda_status") or "").upper() in (
            "FINAL", "OFFICIAL", "PUBLISHED", "OTHER"
        )

        # Determine actual stage (may need updating)
        actual_stage = stage
        if stage == "prelim_scraped" and is_final:
            actual_stage = "final_detected"
        if meeting_passed and actual_stage in ("prelim_scraped", "final_detected"):
            actual_stage = "meeting_complete"
        if has_transcript and actual_stage == "meeting_complete":
            actual_stage = "transcript_fetched"
        if total > 0 and finalized == total and actual_stage in ("transcript_fetched", "meeting_complete"):
            actual_stage = "fully_briefed"

        # Update if stage changed
        if actual_stage != stage:
            conn.execute(
                "UPDATE meetings SET lifecycle_stage=? WHERE id=? AND org_id=?",
                (actual_stage, meeting_id, oid)
            )

        # Determine what needs to happen next
        next_actions = []
        if actual_stage == "prelim_scraped":
            next_actions.append({
                "action": "wait_for_final",
                "label": "Waiting for final agenda",
                "description": "The system will automatically detect when the final agenda is published and notify you.",
                "auto": True,
            })
        elif actual_stage == "final_detected":
            next_actions.append({
                "action": "review_changes",
                "label": "Review agenda changes",
                "description": "Final agenda detected. Check for new items or changes from the preliminary version.",
                "auto": False,
            })
            if not meeting_passed:
                days_until = (datetime.strptime(meeting_date, "%Y-%m-%d") - datetime.utcnow()).days
                next_actions.append({
                    "action": "prepare",
                    "label": f"Meeting in {days_until} day{'s' if days_until != 1 else ''}",
                    "description": "Ensure all items are analyzed and assigned before the meeting.",
                    "auto": False,
                })
        elif actual_stage == "meeting_complete":
            check_count = m.get("transcript_check_count") or 0
            last_check = m.get("transcript_checked_at")
            next_actions.append({
                "action": "fetch_transcript",
                "label": "Fetch meeting transcript",
                "description": f"Meeting has passed. Recording may be available on Granicus. "
                               f"Checked {check_count} time{'s' if check_count != 1 else ''}"
                               + (f" (last: {last_check[:10]})" if last_check else "") + ".",
                "auto": True,
                "button": "🎙 Check for Recording",
            })
        elif actual_stage == "transcript_fetched":
            remaining = total - finalized
            if remaining > 0:
                next_actions.append({
                    "action": "finalize",
                    "label": f"Finalize {remaining} remaining item{'s' if remaining > 1 else ''}",
                    "description": "Transcript is available. Review and finalize remaining items.",
                    "auto": False,
                })
        elif actual_stage == "fully_briefed":
            next_actions.append({
                "action": "done",
                "label": "All items finalized",
                "description": "This meeting is fully briefed. Ready for export.",
                "auto": False,
            })

    return {
        "meeting_id": meeting_id,
        "stage": actual_stage,
        "stage_label": STAGE_LABELS.get(actual_stage, actual_stage),
        "stage_color": STAGE_COLORS.get(actual_stage, "#6b7280"),
        "meeting_date": meeting_date,
        "meeting_passed": meeting_passed,
        "is_final_agenda": is_final,
        "has_transcript": has_transcript,
        "items_total": total,
        "items_finalized": finalized,
        "next_actions": next_actions,
        "stages": [
            {"id": s, "label": STAGE_LABELS[s],
             "color": STAGE_COLORS[s],
             "completed": LIFECYCLE_STAGES.index(s) <= LIFECYCLE_STAGES.index(actual_stage),
             "current": s == actual_stage}
            for s in LIFECYCLE_STAGES
        ],
    }


def check_meetings_needing_transcript(org_id=None) -> list[dict]:
    """Find meetings that have passed but don't have transcripts yet.
    Used by the scheduler to auto-fetch recordings."""
    oid = _resolve_org_id(org_id)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    cutoff = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")

    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.id, m.body_name, m.meeting_date, m.lifecycle_stage,
                      m.transcript_checked_at, m.transcript_check_count
               FROM meetings m
               WHERE m.org_id = ?
                 AND m.meeting_date <= ?
                 AND m.meeting_date >= ?
                 AND m.lifecycle_stage IN ('meeting_complete', 'final_detected', 'prelim_scraped')
                 AND (m.transcript_check_count IS NULL OR m.transcript_check_count < 5)
               ORDER BY m.meeting_date DESC""",
            (oid, today, cutoff)
        ).fetchall()
    return [dict(r) for r in rows]


def record_transcript_check(meeting_id: int, org_id=None):
    """Record that we checked for a transcript (even if not found)."""
    oid = _resolve_org_id(org_id)
    now = now_iso()
    with get_db() as conn:
        conn.execute(
            """UPDATE meetings SET
               transcript_checked_at = ?,
               transcript_check_count = COALESCE(transcript_check_count, 0) + 1
               WHERE id = ? AND org_id = ?""",
            (now, meeting_id, oid)
        )


def create_lifecycle_notification(meeting_id: int, stage: str,
                                   title: str, body: str, org_id=None):
    """Create a notification for a lifecycle transition."""
    oid = _resolve_org_id(org_id)
    now = now_iso()
    import json
    with get_db() as conn:
        conn.execute(
            """INSERT INTO notifications
               (org_id, type, title, body, meeting_id, metadata, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (oid, f"lifecycle_{stage}", title, body, meeting_id,
             json.dumps({"stage": stage}), now)
        )
