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
            """SELECT m.*, COUNT(a.id) as appearance_count,
                      MAX(a.updated_at) as last_activity
               FROM meetings m
               LEFT JOIN appearances a ON a.meeting_id=m.id
               GROUP BY m.id
               HAVING appearance_count > 0
               ORDER BY m.meeting_date DESC, m.body_name ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.update(compute_meeting_status(d["id"]))
        out.append(d)

    # Build search keywords: concatenate item titles for each meeting so
    # the meetings list is searchable by item content (e.g., "microsoft")
    if out:
        meeting_ids = [d["id"] for d in out]
        placeholders = ",".join("?" * len(meeting_ids))
        with get_db() as conn:
            # Check if short_title column exists (older DBs may lack it)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(appearances)").fetchall()}
            title_expr = "COALESCE(short_title,'') || ' ' || COALESCE(appearance_title,'')" if "short_title" in cols else "COALESCE(appearance_title,'')"
            kw_rows = conn.execute(
                f"""SELECT meeting_id,
                           GROUP_CONCAT({title_expr}, ' ') as item_keywords
                    FROM appearances
                    WHERE meeting_id IN ({placeholders})
                    GROUP BY meeting_id""",
                meeting_ids,
            ).fetchall()
        kw_map = {r["meeting_id"]: r["item_keywords"] or "" for r in kw_rows}
        for d in out:
            d["search_keywords"] = kw_map.get(d["id"], "")

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

        # Cross-stage lookup: find ALL committee and BCC appearances so we
        # can show the full journey and use the LATEST dates.
        def _is_cmte(p):
            s = (p.get("agenda_stage") or "").lower()
            bn = (p.get("body_name") or "").lower()
            return s == "committee" or ("committee" in bn and "board of county" not in bn)

        def _is_bcc(p):
            s = (p.get("agenda_stage") or "").lower()
            bn = (p.get("body_name") or "").lower()
            return s == "bcc" or "board of county commissioners" in bn

        cmte_apps = sorted(
            [p for p in prior if _is_cmte(p)],
            key=lambda x: x.get("meeting_date") or "", reverse=True
        )
        bcc_apps = sorted(
            [p for p in prior if _is_bcc(p)],
            key=lambda x: x.get("meeting_date") or "", reverse=True
        )

        # Use the LATEST appearance at each stage for cross-stage columns
        cmte_app = cmte_apps[0] if cmte_apps else None
        bcc_app = bcc_apps[0] if bcc_apps else None

        cur_stage = (a.get("agenda_stage") or "").lower()
        if cur_stage == "committee" and not cmte_app:
            cmte_app = a
            cmte_apps = [a]
        if cur_stage == "bcc" and not bcc_app:
            bcc_app = a
            bcc_apps = [a]

        # Secondary fallback: parsed Legistar lifecycle events.
        cmte_date_from_lc = ""
        cmte_body_from_lc = ""
        cmte_item_from_lc = ""
        bcc_date_from_lc  = ""
        bcc_item_from_lc  = ""
        lc_events = []
        if matter.get("id"):
            try:
                import lifecycle as _lc
                lc_events = _lc.get_timeline_for_matter(matter["id"])
                if not cmte_app or not bcc_app:
                    for ev in lc_events:
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

        # ── Build journey: chronological list of stages this item passed through
        journey_steps = []
        all_sorted = sorted(prior, key=lambda x: x.get("meeting_date") or "")

        # ── Smart current/prior tagging based on dates ──
        # Rules:
        # - An appearance becomes "prior" one day after its meeting date
        # - The next appearance after that becomes "current"
        # - If no future appearance exists, the last one stays "current"
        from datetime import date, timedelta
        today_str = date.today().isoformat()
        yesterday_str = (date.today() - timedelta(days=1)).isoformat()

        # Find the "current" appearance: first one whose meeting_date > yesterday
        # (i.e., meeting hasn't passed yet, or just happened today).
        # If all are past, the last one is current.
        current_app_id = None
        for p in all_sorted:
            md = p.get("meeting_date") or ""
            if md > yesterday_str:  # meeting date is today or future
                current_app_id = p["id"]
                break
        if current_app_id is None and all_sorted:
            # All meetings are past — last one stays current
            current_app_id = all_sorted[-1]["id"]

        for p in all_sorted:
            md = p.get("meeting_date") or ""
            short_date = md[5:] if md else ""   # "03-10" from "2026-03-10"
            bn = (p.get("body_name") or "").lower()
            if _is_bcc(p):
                label = "BCC"
            elif _is_cmte(p):
                # Use short committee name
                full_name = p.get("body_name") or ""
                # Shorten "Housing Committee" → "Housing", etc.
                short_name = full_name.replace(" Committee", "").replace("committee", "").strip()
                if len(short_name) > 20:
                    short_name = short_name[:18] + "…"
                label = short_name or "Cmte"
            else:
                label = (p.get("body_name") or "Other")[:15]
            is_current = (p["id"] == current_app_id)
            is_past = md and md <= yesterday_str and not is_current
            journey_steps.append({
                "label": label,
                "date": short_date,
                "full_date": md,
                "is_current": is_current,
                "is_past": is_past,
                "body_name": p.get("body_name") or "",
                "stage": "bcc" if _is_bcc(p) else ("cmte" if _is_cmte(p) else "other"),
            })

        # Also add lifecycle-only events that predate our stored appearances
        earliest_stored = all_sorted[0].get("meeting_date") if all_sorted else "9999"
        for ev in lc_events:
            ed = ev.get("event_date") or ""
            if ed and ed < earliest_stored:
                bn = (ev.get("body_name") or "").lower()
                action = (ev.get("action") or "").lower()
                if "committee" in bn and "board of county" not in bn:
                    short_name = (ev.get("body_name") or "").replace(" Committee", "").strip()
                    journey_steps.insert(0, {
                        "label": short_name or "Cmte",
                        "date": ed[5:] if ed else "",
                        "full_date": ed,
                        "is_current": False,
                        "body_name": ev.get("body_name") or "",
                        "stage": "cmte",
                        "action": ev.get("action") or "",
                        "from_legistar": True,
                    })
                elif "board of county commissioners" in bn:
                    journey_steps.insert(0, {
                        "label": "BCC",
                        "date": ed[5:] if ed else "",
                        "full_date": ed,
                        "is_current": False,
                        "body_name": ev.get("body_name") or "",
                        "stage": "bcc",
                        "action": ev.get("action") or "",
                        "from_legistar": True,
                    })

        # Sort by full_date after inserting lifecycle events
        journey_steps.sort(key=lambda x: x.get("full_date") or "")

        # ── Derive "What's Next" from legislative status and lifecycle
        leg_status = (matter.get("current_status") or "").lower()
        control = (matter.get("control_body") or "").lower()
        next_step = ""
        next_step_type = ""  # "done", "bcc", "cmte", "pending"

        # Terminal states
        if any(t in leg_status for t in ["adopted", "approved", "passed"]) and \
           "first reading" not in leg_status and "tentatively" not in leg_status:
            next_step = "Adopted"
            next_step_type = "done"
        elif "failed" in leg_status or "withdrawn" in leg_status:
            next_step = leg_status.title()
            next_step_type = "done"
        # Scheduled for public hearing → goes to BCC
        elif "public hearing" in leg_status or "tentatively scheduled" in leg_status:
            next_step = "BCC Public Hearing"
            next_step_type = "bcc"
        # Adopted on first reading → needs second reading / public hearing
        elif "first reading" in leg_status:
            next_step = "BCC 2nd Reading"
            next_step_type = "bcc"
        # Deferred → comes back to same body
        elif "deferred" in leg_status or "continued" in leg_status:
            last_body = journey_steps[-1]["label"] if journey_steps else "Committee"
            next_step = f"Back to {last_body}"
            next_step_type = "cmte" if "bcc" not in last_body.lower() else "bcc"
        # At committee, favorably recommended → goes to BCC
        elif any(t in leg_status for t in ["favorably", "recommended", "forwarded"]):
            next_step = "BCC"
            next_step_type = "bcc"
        # Amended → still moving, check control body
        elif "amended" in leg_status:
            if "board" in control or "bcc" in control:
                next_step = "BCC (Amended)"
                next_step_type = "bcc"
            elif "committee" in control:
                next_step = "Committee (Amended)"
                next_step_type = "cmte"
            else:
                # Check if last step was committee → likely goes to BCC
                if journey_steps and journey_steps[-1].get("stage") == "cmte":
                    next_step = "BCC"
                    next_step_type = "bcc"
                else:
                    next_step = "Pending"
                    next_step_type = "pending"
        # Pending BCC assignment
        elif "pending" in leg_status and "bcc" in leg_status:
            next_step = "BCC Assignment"
            next_step_type = "bcc"
        elif "pending" in control:
            next_step = control.replace("pending", "").strip().title() or "Pending"
            next_step_type = "pending"
        # Fallback: infer from current stage
        elif cur_stage == "committee":
            next_step = "BCC"
            next_step_type = "bcc"
        elif cur_stage == "bcc":
            next_step = "Pending Final Action"
            next_step_type = "pending"
        else:
            next_step = "—"
            next_step_type = "pending"

        # ── Confidence / completeness flags ──────────────────────
        from confidence_flags import compute_confidence_flags
        _confidence, _flags = compute_confidence_flags(a)

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
            # Confidence flags for researcher attention
            "confidence":          _confidence,
            "confidence_flags":    _flags,
            "prior_appearance_count": len(prior_other),
            "has_prior_notes": any(
                (p.get("analyst_working_notes") or "").strip() or
                (p.get("finalized_brief") or "").strip() or
                (p.get("reviewer_notes") or "").strip()
                for p in prior_other
            ),
            "is_supplement": "supplement" in (a.get("agenda_stage") or "").lower(),
            # Cross-stage tracking (LATEST dates)
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
            # NEW: journey + what's next
            "cmte_appearance_count":  len(cmte_apps),
            "bcc_appearance_count":   len(bcc_apps),
            "journey":               journey_steps,
            "next_step":             next_step,
            "next_step_type":        next_step_type,
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
