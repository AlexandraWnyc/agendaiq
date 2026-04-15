"""
meeting_service.py — Meeting package model for OCA Agenda Intelligence v6.

A "meeting package" is one persistent unit (e.g. "BCC 3/17/2026") that owns:
  - metadata (body, date, agenda urls)
  - a set of appearances (items)
  - current exports (draft + final artifacts)
  - a package-level status: Draft · In Progress · Final Ready · Final Generated

This module provides the service layer used by the Saved Meetings page
and the Meeting Detail view.
"""
import logging
from pathlib import Path
from db import get_db
from utils import now_iso
import artifacts

log = logging.getLogger("oca-agent")


# ── Status computation ────────────────────────────────────────

def compute_meeting_status(meeting_id: int) -> dict:
    """Return a dict describing the meeting's package status.
    Keys: status, total, finalized, in_progress, draft, final_generated."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT workflow_status FROM appearances WHERE meeting_id=?",
            (meeting_id,),
        ).fetchall()
    statuses = [r["workflow_status"] or "New" for r in rows]
    total = len(statuses)
    finalized = sum(1 for s in statuses if s in ("Finalized", "Archived"))

    in_progress = sum(
        1 for s in statuses if s in ("Assigned", "In Progress", "Draft Complete", "In Review")
    )
    new_count = sum(1 for s in statuses if s == "New")

    # Has a final export already been generated and is still current?
    final_artifacts = artifacts.get_final_export(meeting_id)

    if total == 0:
        status = "Empty"
    elif final_artifacts:
        status = "Final Generated"
    elif finalized == total:
        status = "Final Ready"
    elif in_progress > 0 or finalized > 0:
        status = "In Progress"
    else:
        status = "Draft"

    return {
        "status": status,
        "total": total,
        "finalized": finalized,
        "in_progress": in_progress,
        "new": new_count,
        "final_available": bool(final_artifacts),
    }


def all_items_finalized(meeting_id: int) -> bool:
    s = compute_meeting_status(meeting_id)
    return s["total"] > 0 and s["finalized"] == s["total"]


# ── Meeting package lookups ───────────────────────────────────

def list_saved_meetings(limit: int = 200) -> list[dict]:
    """Every meeting that has at least one appearance, with package status."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT m.*, COUNT(a.id) as item_count,
                      MAX(a.updated_at) as last_activity
               FROM meetings m
               LEFT JOIN appearances a ON a.meeting_id=m.id
               GROUP BY m.id
               HAVING item_count > 0
               ORDER BY m.meeting_date DESC, m.body_name ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(compute_meeting_status(d["id"]))
        out.append(d)
    return out


def get_meeting_package(meeting_id: int) -> dict | None:
    """Everything the Meeting Detail page needs in one blob."""
    from repository import get_meeting_by_id, get_appearances_for_meeting, get_matter_by_file_number

    m = get_meeting_by_id(meeting_id)
    if not m:
        return None

    from repository import get_all_appearances_for_matter

    apps = get_appearances_for_meeting(meeting_id)

    # Hydrate each appearance with matter info, legislative fields, and
    # cross-meeting case history so the UI can show everything inline.
    items = []
    for a in apps:
        matter = get_matter_by_file_number(a["file_number"]) or {}
        prior = get_all_appearances_for_matter(matter["id"]) if matter else []
        prior_other = [p for p in prior if p["id"] != a["id"]]

        # Cross-stage lookup: find the matching committee appearance and BCC
        # appearance across ALL meetings for this matter so the grid can show
        # Cmte Date/# and BCC Date/# on a single row regardless of which
        # agenda we are viewing.
        cmte_app = next(
            (p for p in prior if (p.get("agenda_stage") or "").lower() == "committee"
             or ("committee" in (p.get("body_name") or "").lower()
                 and "board of county" not in (p.get("body_name") or "").lower())),
            None,
        )
        bcc_app = next(
            (p for p in prior if (p.get("agenda_stage") or "").lower() == "bcc"
             or "board of county commissioners" in (p.get("body_name") or "").lower()),
            None,
        )

        # Fallback: if current appearance IS the committee/BCC one, use self
        cur_stage = (a.get("agenda_stage") or "").lower()
        if cur_stage == "committee" and not cmte_app:
            cmte_app = a
        if cur_stage == "bcc" and not bcc_app:
            bcc_app = a

        # Secondary fallback: parsed Legistar lifecycle events. If we have no
        # stored appearance at a given stage (e.g. the committee hearing was
        # before AgendaIQ was tracking) use the earliest matching event from
        # matter_timeline so cmte/BCC dates populate even on first encounter.
        cmte_date_from_lc = ""
        cmte_body_from_lc = ""
        cmte_item_from_lc = ""
        bcc_date_from_lc  = ""
        bcc_item_from_lc  = ""
        if matter.get("id") and (not cmte_app or not bcc_app):
            try:
                import lifecycle as _lc
                events = _lc.get_timeline_for_matter(matter["id"])
                for ev in events:
                    bn = (ev.get("body_name") or "").lower()
                    if not cmte_app and not cmte_date_from_lc and \
                       "committee" in bn and "board of county" not in bn:
                        cmte_date_from_lc = ev.get("event_date") or ""
                        cmte_body_from_lc = ev.get("body_name") or ""
                        cmte_item_from_lc = ev.get("agenda_item") or ""
                    if not bcc_app and not bcc_date_from_lc and \
                       "board of county commissioners" in bn:
                        bcc_date_from_lc = ev.get("event_date") or ""
                        bcc_item_from_lc = ev.get("agenda_item") or ""
            except Exception:
                pass

        item = {
            **a,
            "short_title":       matter.get("short_title", ""),
            "full_title":        matter.get("full_title", ""),
            "sponsor":           matter.get("sponsor", ""),
            "department":        matter.get("department", ""),
            "file_type":         matter.get("file_type", ""),
            "current_status":    matter.get("current_status", ""),
            "control_body":      matter.get("control_body", ""),
            "legislative_notes": matter.get("legislative_notes", ""),
            "has_analyst_notes":   bool((a.get("analyst_working_notes") or "").strip()),
            "has_reviewer_notes":  bool((a.get("reviewer_notes") or "").strip()),
            "has_finalized_brief": bool((a.get("finalized_brief") or "").strip()),
            "has_ai_summary":      bool((a.get("ai_summary_for_appearance") or "").strip()),
            "prior_appearance_count": len(prior_other),
            "has_prior_notes": any(
                (p.get("analyst_working_notes") or "").strip() or
                (p.get("finalized_brief") or "").strip() or
                (p.get("reviewer_notes") or "").strip()
                for p in prior_other
            ),
            "is_supplement": "supplement" in (a.get("agenda_stage") or "").lower(),
            # Cross-stage tracking
            "committee_appearance_date":   (cmte_app or {}).get("meeting_date", "") or cmte_date_from_lc,
            "committee_appearance_body":   (cmte_app or {}).get("body_name", "") or cmte_body_from_lc,
            "committee_item_number_x":     (cmte_app or {}).get("committee_item_number", "")
                                            or (cmte_app or {}).get("raw_agenda_item_number", "")
                                            or cmte_item_from_lc,
            "bcc_appearance_date":         (bcc_app or {}).get("meeting_date", "") or bcc_date_from_lc,
            "bcc_item_number_x":           (bcc_app or {}).get("bcc_item_number", "")
                                            or (bcc_app or {}).get("raw_agenda_item_number", "")
                                            or bcc_item_from_lc,
            "committee_date_source":       "stored" if cmte_app else ("legistar" if cmte_date_from_lc else ""),
            "bcc_date_source":             "stored" if bcc_app else ("legistar" if bcc_date_from_lc else ""),
        }
        items.append(item)

    status = compute_meeting_status(meeting_id)
    current_artifacts = artifacts.get_current_meeting_level_artifacts(meeting_id)

    return {
        "meeting":   m,
        "status":    status,
        "items":     items,
        "artifacts": current_artifacts,
    }


# ── Export orchestrators that also register artifacts ─────────

def generate_draft_export(meeting_id: int, base_output_dir: Path) -> list[dict]:
    """Regenerate Excel + Word drafts from the latest DB state and register
    them as current draft artifacts.  Returns the artifact rows."""
    from repository import get_meeting_by_id
    from exporters import export_for_meeting

    m = get_meeting_by_id(meeting_id)
    if not m:
        return []

    output_dir = Path(base_output_dir) / f"meeting_{meeting_id}" / "drafts"
    output_dir.mkdir(parents=True, exist_ok=True)
    files = export_for_meeting(meeting_id, output_dir)

    registered = []
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".xlsx":
            atype, label = "excel_draft", "Part 1 — Tracking (Draft)"
        elif suffix == ".docx":
            atype, label = "word_draft",  "Part 2 — Research Brief (Draft)"
        else:
            continue
        aid = artifacts.register_artifact(
            atype, f,
            meeting_id=meeting_id,
            label=label,
            is_final=False,
            supersede_previous=True,
        )
        registered.append(artifacts.get_artifact(aid))

    # Mark last_exported on meeting
    with get_db() as conn:
        conn.execute(
            "UPDATE meetings SET last_exported_at=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), meeting_id),
        )
    return registered


def generate_final_export(meeting_id: int, base_output_dir: Path) -> tuple[bool, str, list[dict]]:
    """Only succeeds if every appearance is Finalized.  Returns (ok, message, artifact_rows)."""
    if not all_items_finalized(meeting_id):
        status = compute_meeting_status(meeting_id)
        return False, (
            f"Cannot generate final export: {status['finalized']}/{status['total']} "
            f"items are finalized."
        ), []

    from repository import get_meeting_by_id
    from exporters import export_for_meeting

    m = get_meeting_by_id(meeting_id)
    if not m:
        return False, "Meeting not found.", []

    output_dir = Path(base_output_dir) / f"meeting_{meeting_id}" / "final"
    output_dir.mkdir(parents=True, exist_ok=True)
    files = export_for_meeting(meeting_id, output_dir)

    registered = []
    for f in files:
        suffix = f.suffix.lower()
        if suffix == ".xlsx":
            atype, label = "excel_final", "Part 1 — Tracking (FINAL)"
        elif suffix == ".docx":
            atype, label = "word_final",  "Part 2 — Research Brief (FINAL)"
        else:
            continue
        aid = artifacts.register_artifact(
            atype, f,
            meeting_id=meeting_id,
            label=label,
            is_final=True,
            supersede_previous=True,
        )
        registered.append(artifacts.get_artifact(aid))

    with get_db() as conn:
        conn.execute(
            "UPDATE meetings SET finalized_at=?, last_exported_at=?, export_status=?, updated_at=? WHERE id=?",
            (now_iso(), now_iso(), "Final Generated", now_iso(), meeting_id),
        )
    return True, "Final export generated.", registered
