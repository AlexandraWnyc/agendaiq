"""
repository.py — CRUD and lookup helpers for OCA Agenda Intelligence v6

Continuity logic:
  Case A: file_number not seen before → create matter + appearance (carried_forward=0)
  Case B: file_number already exists  → create new appearance, copy forward useful
          fields from the most recent prior appearance (carried_forward=1)
"""
import logging
from datetime import datetime
from db import get_db
from schema import stage_rank
from utils import now_iso

log = logging.getLogger("oca-agent")


# ── Matters ───────────────────────────────────────────────────

def get_matter_by_file_number(file_number: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM matters WHERE file_number = ?", (str(file_number),)
        ).fetchone()
        return dict(row) if row else None


def upsert_matter(file_number: str, fields: dict) -> int:
    """
    Insert a new matter or update an existing one.
    Only updates fields when the incoming value is non-empty and (for stages)
    newer/more advanced than what we already have.
    Returns the matter id.
    """
    now = now_iso()
    file_number = str(file_number)
    existing = get_matter_by_file_number(file_number)

    if existing is None:
        # INSERT
        cols = ["file_number", "created_at", "updated_at"]
        vals = [file_number, now, now]
        update_fields = [
            "short_title", "full_title", "file_type", "sponsor", "department",
            "control_body", "current_status", "legislative_notes",
            "first_seen_date", "last_seen_date", "current_stage",
        ]
        for f in update_fields:
            v = fields.get(f)
            if v:
                cols.append(f)
                vals.append(v)

        placeholders = ",".join("?" * len(cols))
        col_names = ",".join(cols)
        with get_db() as conn:
            conn.execute(
                f"INSERT INTO matters ({col_names}) VALUES ({placeholders})", vals
            )
            row = conn.execute(
                "SELECT id FROM matters WHERE file_number=?", (file_number,)
            ).fetchone()
            return row["id"]
    else:
        # UPDATE — only replace if incoming value is better
        matter_id = existing["id"]
        updates = {"updated_at": now}

        # Simple text fields: update if incoming is non-empty and existing is empty
        simple = [
            "short_title", "full_title", "file_type", "sponsor",
            "department", "control_body", "legislative_notes",
        ]
        for f in simple:
            v = fields.get(f)
            if v and not existing.get(f):
                updates[f] = v
            elif v and len(str(v)) > len(str(existing.get(f) or "")):
                # Prefer longer/richer value
                updates[f] = v

        # Status: always update to latest
        if fields.get("current_status"):
            updates["current_status"] = fields["current_status"]

        # Dates
        if fields.get("last_seen_date"):
            updates["last_seen_date"] = fields["last_seen_date"]
        if not existing.get("first_seen_date") and fields.get("first_seen_date"):
            updates["first_seen_date"] = fields["first_seen_date"]

        # Stage: update if new stage is more advanced
        new_stage = fields.get("current_stage", "")
        old_stage = existing.get("current_stage", "")
        if new_stage and stage_rank(new_stage) >= stage_rank(old_stage):
            updates["current_stage"] = new_stage

        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            with get_db() as conn:
                conn.execute(
                    f"UPDATE matters SET {set_clause} WHERE id=?",
                    list(updates.values()) + [matter_id]
                )
        return matter_id


def update_matter_ai_fields(matter_id: int, part1: str, watch_points: str):
    """Store the latest AI summary and watch points on the master matter record."""
    with get_db() as conn:
        conn.execute(
            """UPDATE matters SET
               latest_ai_summary_part1 = ?,
               latest_watch_points = ?,
               updated_at = ?
               WHERE id = ?""",
            (part1, watch_points, now_iso(), matter_id)
        )


# ── Meetings ──────────────────────────────────────────────────

def _normalize_date(d: str) -> str:
    """Normalize any date string to ISO YYYY-MM-DD format."""
    for fmt in ("%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(d.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d.strip()  # already ISO or unknown — return as-is


def _date_variants(iso_date: str) -> list:
    """Return all plausible date format variants for matching legacy rows.

    The database may contain dates as 'YYYY-MM-DD' or 'M/D/YYYY'.
    Given an ISO date string, return both formats for comparison.
    """
    variants = [iso_date]
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        slash = f"{dt.month}/{dt.day}/{dt.year}"  # M/D/YYYY (no zero-padding)
        if slash != iso_date:
            variants.append(slash)
    except ValueError:
        pass
    return variants


def get_or_create_meeting(body_name: str, meeting_date: str, **kwargs) -> int:
    """Find an existing meeting row or create one. Returns meeting id."""
    meeting_date = _normalize_date(meeting_date)
    with get_db() as conn:
        # Check for both ISO and legacy M/D/YYYY formats in the DB
        for dv in _date_variants(meeting_date):
            row = conn.execute(
                "SELECT id FROM meetings WHERE body_name=? AND meeting_date=?",
                (body_name, dv)
            ).fetchone()
            if row:
                updates = []
                params = []
                # Normalize the stored date to ISO if it was in legacy format
                if dv != meeting_date:
                    updates.append("meeting_date=?")
                    params.append(meeting_date)
                # Fix meeting_type if caller provides one and existing is wrong
                new_type = kwargs.get("meeting_type")
                if new_type:
                    cur = conn.execute(
                        "SELECT meeting_type FROM meetings WHERE id=?",
                        (row["id"],)
                    ).fetchone()
                    if cur and cur["meeting_type"] != new_type:
                        updates.append("meeting_type=?")
                        params.append(new_type)
                if updates:
                    params.append(row["id"])
                    conn.execute(
                        f"UPDATE meetings SET {','.join(updates)} WHERE id=?",
                        params
                    )
                return row["id"]

        now = now_iso()
        cols = ["body_name", "meeting_date", "created_at", "updated_at"]
        vals = [body_name, meeting_date, now, now]
        optional = ["meeting_type", "agenda_status", "agenda_version",
                    "final_agenda_url", "agenda_pdf_url", "agenda_page_url",
                    "meeting_family_id"]
        for k in optional:
            if kwargs.get(k):
                cols.append(k)
                vals.append(kwargs[k])

        placeholders = ",".join("?" * len(cols))
        conn.execute(
            f"INSERT INTO meetings ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        return conn.execute(
            "SELECT id FROM meetings WHERE body_name=? AND meeting_date=?",
            (body_name, meeting_date)
        ).fetchone()["id"]


# ── Appearances ───────────────────────────────────────────────

def get_appearance(matter_id: int, meeting_id: int) -> dict | None:
    """Check if an appearance already exists for this matter+meeting pair."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM appearances WHERE matter_id=? AND meeting_id=?",
            (matter_id, meeting_id)
        ).fetchone()
        return dict(row) if row else None


def get_latest_appearance_for_matter(matter_id: int,
                                     before_meeting_id: int | None = None) -> dict | None:
    """Return the most recent *earlier* appearance for a matter.

    If before_meeting_id is given, only consider appearances whose meeting
    date is strictly earlier than the meeting identified by before_meeting_id.
    This prevents carrying notes *backward* from a later stage (e.g. BCC)
    to an earlier one (e.g. committee).
    """
    with get_db() as conn:
        if before_meeting_id is not None:
            # Get the meeting date of the target meeting
            target = conn.execute(
                "SELECT meeting_date FROM meetings WHERE id=?",
                (before_meeting_id,)
            ).fetchone()
            if target:
                row = conn.execute(
                    """SELECT a.* FROM appearances a
                       JOIN meetings m ON m.id = a.meeting_id
                       WHERE a.matter_id = ?
                         AND m.meeting_date < ?
                       ORDER BY m.meeting_date DESC, a.created_at DESC
                       LIMIT 1""",
                    (matter_id, target["meeting_date"])
                ).fetchone()
                return dict(row) if row else None
        # Fallback: no meeting context, return most recent by created_at
        row = conn.execute(
            """SELECT * FROM appearances
               WHERE matter_id=?
               ORDER BY created_at DESC LIMIT 1""",
            (matter_id,)
        ).fetchone()
        return dict(row) if row else None


def get_appearance_by_id(appearance_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM appearances WHERE id=?", (appearance_id,)
        ).fetchone()
        return dict(row) if row else None


def create_or_update_appearance(matter_id: int, meeting_id: int,
                                 file_number: str, fields: dict) -> tuple[int, bool]:
    """
    Create a new appearance or update an existing one for this matter+meeting.
    Implements Case A (new matter) and Case B (existing matter) continuity logic.
    Returns (appearance_id, is_new).
    """
    now = now_iso()
    file_number = str(file_number)

    existing_app = get_appearance(matter_id, meeting_id)
    if existing_app:
        # Already exists — update metadata if richer (don't overwrite AI work)
        app_id = existing_app["id"]
        updates = {"updated_at": now}
        for f in ["appearance_title", "appearance_notes", "committee_item_number",
                  "bcc_item_number", "agenda_stage", "raw_agenda_item_number",
                  "matter_url", "item_pdf_url", "item_pdf_local_path"]:
            v = fields.get(f)
            if v and not existing_app.get(f):
                updates[f] = v
        if updates:
            set_clause = ", ".join(f"{k}=?" for k in updates)
            with get_db() as conn:
                conn.execute(
                    f"UPDATE appearances SET {set_clause} WHERE id=?",
                    list(updates.values()) + [app_id]
                )
        return app_id, False

    # Determine carry-forward — only from chronologically earlier meetings
    prior = get_latest_appearance_for_matter(matter_id, before_meeting_id=meeting_id)
    carried = 1 if prior else 0
    prior_id = prior["id"] if prior else None

    cols = [
        "matter_id", "meeting_id", "file_number",
        "carried_forward_from_prior", "workflow_status",
        "created_at", "updated_at",
    ]
    vals = [matter_id, meeting_id, file_number, carried, "New", now, now]

    if prior_id is not None:
        cols.append("prior_appearance_id")
        vals.append(prior_id)

    # Carry forward useful fields from prior appearance
    carry_fields = [
        "analyst_working_notes",
        "ai_summary_for_appearance",
        "watch_points_for_appearance",
        "finalized_brief",
    ]
    if prior and carried:
        # Build a human-readable label: "Government Operations 3/10/2026, Item 2A"
        prior_meeting_info = None
        with get_db() as conn:
            prior_meeting_info = conn.execute(
                "SELECT body_name, meeting_date FROM meetings WHERE id=?",
                (prior["meeting_id"],)
            ).fetchone()
        prior_item_num = prior.get("committee_item_number") or prior.get("bcc_item_number") or ""
        if prior_meeting_info:
            label_parts = []
            if prior_meeting_info["body_name"]:
                label_parts.append(prior_meeting_info["body_name"])
            if prior_meeting_info["meeting_date"]:
                label_parts.append(prior_meeting_info["meeting_date"])
            if prior_item_num:
                label_parts.append(f"Item {prior_item_num}")
            carry_label = ", ".join(label_parts) if label_parts else f"prior appearance {prior_id}"
        else:
            carry_label = f"prior appearance {prior_id}"

        for cf in carry_fields:
            pv = prior.get(cf)
            if pv:
                cols.append(cf)
                vals.append(f"[Carried from {carry_label}]\n{pv}")

    # ── Sticky researcher / reviewer ──────────────────────────────────
    # When a matter reappears at a later stage (e.g. committee → BCC), keep
    # the original researcher and reviewer with it so ownership doesn't get
    # lost. Only applied if the new appearance wasn't explicitly assigned
    # via fields{} by the caller.
    if prior:
        for sticky in ("assigned_to", "reviewer", "priority"):
            prior_val = (prior.get(sticky) or "").strip() if isinstance(prior.get(sticky), str) else prior.get(sticky)
            if prior_val and sticky not in cols and not fields.get(sticky):
                cols.append(sticky)
                vals.append(prior_val)

    # Add new-appearance-specific fields
    new_fields = [
        "raw_agenda_item_number", "committee_item_number", "bcc_item_number",
        "agenda_stage", "appearance_title", "appearance_notes",
        "requires_research", "priority",
        "matter_url", "item_pdf_url", "item_pdf_local_path",
    ]
    for f in new_fields:
        v = fields.get(f)
        if v is not None and f not in cols:
            cols.append(f)
            vals.append(v)

    placeholders = ",".join("?" * len(cols))
    with get_db() as conn:
        conn.execute(
            f"INSERT INTO appearances ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        row = conn.execute(
            "SELECT id FROM appearances WHERE matter_id=? AND meeting_id=? ORDER BY id DESC LIMIT 1",
            (matter_id, meeting_id)
        ).fetchone()
        return row["id"], True


def update_appearance_ai(appearance_id: int, part1: str, part2: str,
                          watch_points: str, leg_summary: str,
                          input_hash: str | None = None,
                          tokens_in: int | None = None,
                          tokens_out: int | None = None,
                          cached_tokens: int | None = None,
                          ai_risk_level: str = "",
                          ai_risk_reason: str = ""):
    """Save AI analysis results to an appearance row.

    If input_hash is provided, it is stored alongside the token counts so
    subsequent runs can skip re-calling Claude when the same prompt is
    assembled for a different appearance (e.g., the same item carried
    across stages).
    """
    ts = now_iso()
    with get_db() as conn:
        if input_hash is not None:
            conn.execute(
                """UPDATE appearances SET
                   ai_summary_for_appearance   = ?,
                   watch_points_for_appearance = ?,
                   leg_history_summary         = ?,
                   finalized_brief             = ?,
                   analysis_input_hash         = ?,
                   analysis_tokens_in          = ?,
                   analysis_tokens_out         = ?,
                   analysis_cached_tokens      = ?,
                   analysis_at                 = ?,
                   ai_risk_level               = ?,
                   ai_risk_reason              = ?,
                   updated_at                  = ?
                   WHERE id = ?""",
                (part1, watch_points, leg_summary, part2,
                 input_hash, tokens_in or 0, tokens_out or 0, cached_tokens or 0,
                 ts, ai_risk_level or "", ai_risk_reason or "",
                 ts, appearance_id)
            )
        else:
            conn.execute(
                """UPDATE appearances SET
                   ai_summary_for_appearance   = ?,
                   watch_points_for_appearance = ?,
                   leg_history_summary         = ?,
                   finalized_brief             = ?,
                   ai_risk_level               = ?,
                   ai_risk_reason              = ?,
                   updated_at                  = ?
                   WHERE id = ?""",
                (part1, watch_points, leg_summary, part2,
                 ai_risk_level or "", ai_risk_reason or "",
                 ts, appearance_id)
            )


def find_cached_analysis(input_hash: str) -> dict | None:
    """If any appearance has already been analyzed with this exact input
    hash, return the stored AI fields so the pipeline can reuse them and
    skip the Claude API call entirely.

    Returns a dict with ai_summary, part2 (finalized_brief), watch_points,
    leg_history_summary, plus the source appearance_id — or None if no match.
    """
    if not input_hash:
        return None
    with get_db() as conn:
        row = conn.execute(
            """SELECT id, ai_summary_for_appearance, watch_points_for_appearance,
                      leg_history_summary, finalized_brief
               FROM appearances
               WHERE analysis_input_hash = ?
                 AND ai_summary_for_appearance IS NOT NULL
                 AND LENGTH(ai_summary_for_appearance) > 50
               ORDER BY id DESC
               LIMIT 1""",
            (input_hash,)
        ).fetchone()
        if not row:
            return None
        return {
            "source_appearance_id": row["id"],
            "part1":        row["ai_summary_for_appearance"] or "",
            "part2":        row["finalized_brief"]             or "",
            "watch_points": row["watch_points_for_appearance"] or "",
            "leg_summary":  row["leg_history_summary"]         or "",
        }


def get_all_appearances_for_matter(matter_id: int) -> list[dict]:
    """Return all appearances for a matter, oldest first."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, mt.meeting_date, mt.body_name
               FROM appearances a
               JOIN meetings mt ON mt.id = a.meeting_id
               WHERE a.matter_id = ?
               ORDER BY mt.meeting_date ASC, a.created_at ASC""",
            (matter_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_appearances_for_meeting(meeting_id: int) -> list[dict]:
    """Return all appearances for a given meeting."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.*, m.short_title, m.full_title, m.file_number as matter_file_number
               FROM appearances a
               JOIN matters m ON m.id = a.matter_id
               WHERE a.meeting_id = ?
               ORDER BY a.committee_item_number, a.id""",
            (meeting_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_processed_file_numbers_for_meeting(meeting_id: int) -> set:
    """Return set of file numbers already processed for this meeting."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT file_number FROM appearances WHERE meeting_id=?", (meeting_id,)
        ).fetchall()
        return {r["file_number"] for r in rows}


def get_meeting_by_id(meeting_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return dict(row) if row else None


def get_meetings_by_date(meeting_date: str) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM meetings WHERE meeting_date=? ORDER BY body_name",
            (meeting_date,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_recent_meeting_ids_for_date_range(date_str: str,
                                           selected_bodies: list[str] | None = None) -> list[int]:
    """Return meeting IDs matching a date (or nearby dates) and optionally
    filtered by body names. Used to find meetings that were just processed
    so we can auto-trigger transcript backfill."""
    with get_db() as conn:
        if selected_bodies:
            placeholders = ",".join("?" for _ in selected_bodies)
            rows = conn.execute(
                f"""SELECT id FROM meetings
                    WHERE meeting_date >= ? AND body_name IN ({placeholders})
                    ORDER BY meeting_date ASC""",
                (date_str, *selected_bodies)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM meetings WHERE meeting_date >= ? ORDER BY meeting_date ASC",
                (date_str,)
            ).fetchall()
        return [r["id"] for r in rows]
