"""
lifecycle.py — Matter lifecycle timeline for OCA Agenda Intelligence v6.

Every Legistar matter.asp page has a "Legislative History" block that lists
each action taken on the item: when it was introduced, which committees
reviewed it, what their recommendation was, and ultimately the BCC action.

This module:
  - parses that raw text into structured events
  - persists them to matter_timeline (dedup'd by matter_id+date+body+action)
  - exposes a helper to rebuild a timeline for a matter given its raw text
  - exposes a backfill that loops every matter in the DB, re-hits matter.asp
    through the scraper, updates matter_url / item_pdf_url / downloaded PDF
    on every appearance of that matter, and rebuilds the timeline.

A "full lifecycle" is therefore: union of our scraped appearances (what
we've analyzed) + parsed Legistar history events (everything the county
has done to the item, including pre-AgendaIQ activity).
"""
import logging
import re
from datetime import datetime
from pathlib import Path
from db import get_db
from utils import now_iso

log = logging.getLogger("oca-agent")

# Recognised committee/board tokens that typically appear in a history line.
_BODY_HINTS = [
    "Board of County Commissioners",
    "County Commission",
    "Committee of the Whole",
    "Unincorporated Municipal Service Area",
    "Infrastructure, Operations and Innovations",
    "Community Health and Economic Resilience",
    "Housing, Recreation and Culture",
    "Public Safety and Rehabilitation",
    "Transportation, Mobility and Planning",
    "Chairperson's Policy Council",
    "Mayor",
    "Clerk of the Board",
    "Office of the Commission Auditor",
    "County Attorney",
]

_ACTION_HINTS = [
    "Adopted", "Adopted on first reading", "Adopted with Change",
    "Forwarded with a favorable recommendation", "Forwarded without recommendation",
    "Forwarded to BCC with a favorable recommendation",
    "Deferred", "Carried over", "Tabled", "No action taken",
    "Introduced", "Received", "Referred", "Withdrawn",
    "Amended", "Substituted", "Rescinded", "Moved to",
    "Set on public hearing", "Public Hearing", "Passed",
    "Approved", "Approved with Conditions", "Vetoed",
]

_DATE_RE = re.compile(r"(\d{1,2}/\d{1,2}/\d{2,4})")

# Miami-Dade Legistar legislative history is rendered as a table:
#   Acting Body | Date | Agenda Item | Action | Sent To | Due Date | ...
# When pulled as plain text, cells from the same row are concatenated with
# no separators, e.g.:
#   "Intergovernmental and Economic Impact Committee3/11/20263CForwarded to BCC with a favorable recommendation"
# We use the date tokens as row boundaries: the text immediately before each
# date is the body, and the text immediately after the date starts with an
# optional agenda item code (like "3C", "10A2", "4A1a") followed by the
# action verb.
# Miami-Dade agenda item codes: 1-2 digits, one uppercase letter, optional
# digits, optional single lowercase letter (e.g. "3C", "10A2", "4A1a").
# Tight match so we don't swallow the first letter of the action verb.
_AGENDA_ITEM_RE = re.compile(r"^([0-9]{1,2}[A-Z](?:[0-9]+)?(?:[a-z])?)(?=[A-Z]|\s|$|[-–·•|:,])")

_ROLE_TOKENS = [
    "Assigned", "Additions", "Forwarded to", "Forwarded with",
    "Forwarded without", "Forwarded to BCC",
    "REPORT:", "Sponsor", "pending", "Attachments",
]

# Common Miami-Dade body/committee stems. Anything containing "Committee",
# "Commission", "Board", "Mayor", "Clerk", "Office of", "County Attorney",
# "Policy Council" is treated as a body even when not in this list.
_KNOWN_BODY_FRAGMENTS = [
    "Board of County Commissioners",
    "County Commission",
    "Committee of the Whole",
    "Committee",               # catches "Intergovernmental and Economic Impact Committee" etc.
    "Commission",
    "Council",                 # "Chairperson's Policy Council"
    "Mayor",
    "Clerk of the Board",
    "Office of",               # "Office of the Commission Auditor", "Office of Agenda Coordination"
    "County Attorney",
    "Palmer",                  # Arnold Palmer etc. (person assignees sometimes appear as body)
]


def _parse_date(s: str) -> str | None:
    """Return YYYY-MM-DD or None."""
    if not s:
        return None
    m = _DATE_RE.search(s)
    if not m:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(m.group(1), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _clean_body(text: str) -> str:
    """Trim leading/trailing junk and collapse spaces."""
    t = re.sub(r"\s+", " ", (text or "")).strip(" \t\n-–·•|:,")
    # If the segment ends with an ACTION_HINT (because the row before had no
    # separator), trim it off.
    for hint in ("REPORT:", "REPORT :"):
        idx = t.find(hint)
        if idx >= 0:
            t = t[:idx].strip(" \t-–·•|:,")
    return t


def _looks_like_body(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    if len(t) < 3 or len(t) > 160:
        return False
    # If it doesn't contain at least one of our body fragments, skip.
    low = t.lower()
    return any(frag.lower() in low for frag in _KNOWN_BODY_FRAGMENTS)


def parse_legislative_history(raw: str) -> list[dict]:
    """
    Parse Miami-Dade Legistar legislative-history text into structured events.
    Handles both line-separated and concatenated (table-flattened) layouts.

    Each event has:
      event_date  — YYYY-MM-DD
      body_name   — e.g. "Intergovernmental and Economic Impact Committee"
      agenda_item — e.g. "3C" or "" if none in that row
      action      — e.g. "Forwarded to BCC with a favorable recommendation"
      result      — "" (reserved for vote count parsing later)
      raw_line    — original slice of text surrounding the event (for debugging)
    """
    if not raw:
        return []

    # Drop the header line and any "Legislative Text..." tail that might have
    # been included by mistake.
    text = raw
    for marker in ("Legislative Text", "\nNotes ", "Indexes\n"):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    text = text.replace("\r", " ")

    # Collapse very short run-in whitespace but preserve multi-space runs as
    # they sometimes survive from table cell gaps.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)

    # Find every date token with its position in the text.
    date_matches = list(_DATE_RE.finditer(text))
    if not date_matches:
        return []

    events = []
    for i, dm in enumerate(date_matches):
        iso = _parse_date(dm.group(1))
        if not iso:
            continue

        # Body segment: text between the previous date (or start) and this date.
        start = date_matches[i - 1].end() if i > 0 else 0
        body_segment = text[start:dm.start()]

        # If the previous date's tail (agenda item + action) bled into the
        # current row's body, split on the last newline / "REPORT:" marker.
        # When the segment ends with \n (common in line-separated layouts),
        # rsplit gives an empty last element — fall back to the prior line.
        if "\n" in body_segment:
            parts = body_segment.rstrip("\n").rsplit("\n", 1)
            body_segment = parts[-1] if parts else body_segment
        body = _clean_body(body_segment)

        # Accept any segment whose ending contains a body fragment. Some rows
        # have e.g. "County Attorney Office of Agenda Coordination" concatenated
        # across columns — keep the last matching fragment.
        if not _looks_like_body(body):
            # Try to recover by searching for the LAST body fragment within
            # body_segment and using that as the body.
            low = body_segment.lower()
            best = -1
            best_frag = ""
            for frag in _KNOWN_BODY_FRAGMENTS:
                j = low.rfind(frag.lower())
                if j > best:
                    best, best_frag = j, frag
            if best < 0:
                continue
            # Pull surrounding context (20 chars before through end of frag)
            body = _clean_body(body_segment[max(0, best - 60):best + len(best_frag)])
            if not _looks_like_body(body):
                continue

        # Tail after the date: agenda item + action + notes, up to next date.
        tail_end = date_matches[i + 1].start() if i + 1 < len(date_matches) else len(text)
        tail = text[dm.end():tail_end]
        tail = tail.strip(" \t\n-–·•|:,")

        # Agenda item: short alphanumeric token at the start of tail.
        agenda_item = ""
        m = _AGENDA_ITEM_RE.match(tail)
        if m:
            agenda_item = m.group(1)
            tail = tail[m.end():].lstrip(" \t\n-–·•|:,")

        # Action: prefer an ACTION_HINT match at the start; otherwise first
        # ~120 chars until a newline or REPORT:
        action = ""
        low_tail = tail.lower()
        for hint in _ACTION_HINTS:
            if low_tail.startswith(hint.lower()):
                action = hint
                # Keep any additional detail that follows (e.g. "5 - 0").
                extra = tail[len(hint):].split("\n", 1)[0].strip(" -–·•|:,")
                if extra and len(extra) < 80:
                    action = f"{hint} {extra}".strip()
                break
        if not action:
            # Split at REPORT: or a newline
            cut = len(tail)
            for sep in ("REPORT:", "\nAttach", "\n"):
                j = tail.find(sep)
                if 0 <= j < cut:
                    cut = j
            action = tail[:cut].strip(" -–·•|:,")[:160]

        events.append({
            "event_date":  iso,
            "body_name":   body,
            "agenda_item": agenda_item,
            "action":      action,
            "result":      "",
            "raw_line":    (body_segment + dm.group(0) + tail[:120])[-500:],
        })

    # Dedup by (date, body, agenda_item, action)
    seen = set()
    uniq = []
    for e in events:
        k = (e["event_date"], e["body_name"], e["agenda_item"], e["action"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(e)

    # Oldest first
    uniq.sort(key=lambda e: e["event_date"])
    return uniq


def save_timeline(matter_id: int, events: list[dict]) -> int:
    """Upsert timeline events for a matter. Returns count inserted."""
    if not events:
        return 0
    now = now_iso()
    inserted = 0
    with get_db() as conn:
        for ev in events:
            try:
                conn.execute(
                    """INSERT OR IGNORE INTO matter_timeline
                       (matter_id, event_date, body_name, agenda_item, action, result,
                        source, raw_line, sort_key, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (matter_id, ev.get("event_date"), ev.get("body_name", ""),
                     ev.get("agenda_item", ""),
                     ev.get("action", ""), ev.get("result", ""),
                     ev.get("source", "legistar"), ev.get("raw_line", ""),
                     ev.get("event_date") or "", now),
                )
                if conn.total_changes:
                    inserted += 1
            except Exception as e:
                log.debug(f"  timeline insert skipped: {e}")
    return inserted


def get_timeline_for_matter(matter_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM matter_timeline
               WHERE matter_id=? ORDER BY event_date ASC, id ASC""",
            (matter_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def rebuild_for_matter(matter_id: int, raw_history: str) -> int:
    """Parse raw history and REPLACE events for this matter. Returns inserted count.

    Wipes existing matter_timeline rows first so stale / partial parses from
    older parser versions get cleanly overwritten.
    """
    events = parse_legislative_history(raw_history or "")
    with get_db() as conn:
        conn.execute("DELETE FROM matter_timeline WHERE matter_id=?", (matter_id,))
    n = save_timeline(matter_id, events)

    # Cache the raw and refresh timestamp on the matter
    with get_db() as conn:
        conn.execute(
            """UPDATE matters SET legislative_history_raw=?,
               lifecycle_refreshed_at=?, updated_at=? WHERE id=?""",
            (raw_history or "", now_iso(), now_iso(), matter_id),
        )
    return n


# ── Backfill ──────────────────────────────────────────────────

def backfill_urls_and_lifecycle(pdf_dir: Path,
                                 only_missing_urls: bool = False,
                                 progress_callback=None) -> dict:
    """
    For every matter in the DB:
      - Re-hit matter.asp via the scraper
      - Update each appearance's matter_url / item_pdf_url / local PDF path
      - Parse the leg history and rebuild its matter_timeline

    When only_missing_urls=True, skip matters whose appearances already have
    matter_url populated (useful for a quick first pass).

    Returns summary dict.
    """
    from scraper import MiamiDadeScraper

    sc = MiamiDadeScraper()
    # Warm up the session by hitting the Legistar landing page first.
    # During normal analysis the scraper visits the committee list and
    # agenda pages before get_item_detail — those requests establish
    # session cookies that matter.asp may depend on.
    try:
        sc.session.get("https://www.miamidade.gov/govaction/", timeout=15)
        log.info("  Legistar session warmed up")
    except Exception as e:
        log.warning(f"  Session warm-up failed (continuing anyway): {e}")

    Path(pdf_dir).mkdir(parents=True, exist_ok=True)

    summary = {"matters": 0, "urls_filled": 0, "pdfs_downloaded": 0,
               "timeline_events": 0, "errors": 0}

    with get_db() as conn:
        matters = conn.execute("SELECT id, file_number FROM matters").fetchall()

    total = len(matters)
    for i, m in enumerate(matters, 1):
        mid, fn = m["id"], m["file_number"]
        if progress_callback:
            progress_callback(f"[{i}/{total}] backfilling File# {fn}")

        # Decide whether we need to hit the network for this matter.
        # Skip only when it has BOTH urls populated AND at least one
        # timeline event — otherwise we still need to pull legistar to
        # parse the legislative history.
        # NOTE: no skip conditions here anymore. The banner-triggered backfill
        # always re-hits Legistar and re-parses. Cheap enough for <200 matters
        # and guarantees stale parses get overwritten. The only_missing_urls
        # flag is preserved for API compatibility but no longer short-circuits.
        _ = only_missing_urls  # intentionally unused

        try:
            stub = {"matter_id": fn}
            detail = sc.get_item_detail(stub)
            detail_url = detail.get("detail_url") or ""
            pdf_url    = detail.get("pdf_url") or ""
            raw_hist   = detail.get("legislative_history_raw") or ""
            log.info(f"  backfill File# {fn}: detail_url={bool(detail_url)}, "
                     f"pdf_url={bool(pdf_url)}, raw_hist_len={len(raw_hist)}")
            if raw_hist:
                log.info(f"  raw_hist preview: {raw_hist[:200]}")
            else:
                # Log what we got from the page to diagnose why history is empty
                page_text = detail.get("page_text") or detail.get("routing_info") or ""
                log.info(f"  NO leg history. page_text_len={len(page_text)}, "
                         f"keys={list(detail.keys())[:10]}")

            # Fallback: if get_item_detail somehow didn't set detail_url,
            # reconstruct a direct matter.asp URL.
            if not detail_url:
                yr = datetime.now().year
                detail_url = (f"http://www.miamidade.gov/govaction/matter.asp"
                              f"?matter={fn}&file=true&fileAnalysis=false&yearFolder=Y{yr}")

            # Download the PDF once per matter if we got a url
            local_pdf = ""
            if pdf_url:
                pp = sc.download_pdf(pdf_url, pdf_dir)
                if pp:
                    local_pdf = str(pp)
                    summary["pdfs_downloaded"] += 1

            # Write URLs onto every appearance of this matter
            with get_db() as conn:
                apps = conn.execute(
                    "SELECT id, matter_url, item_pdf_url, item_pdf_local_path "
                    "FROM appearances WHERE matter_id=?",
                    (mid,),
                ).fetchall()
                for a in apps:
                    updates, params = [], []
                    # Always overwrite matter_url — old values may be
                    # bridge URLs (searchforpdf.asp) instead of the final
                    # matter.asp link.
                    if detail_url and "matter.asp" in detail_url:
                        old_url = a["matter_url"] or ""
                        if old_url != detail_url:
                            updates.append("matter_url=?"); params.append(detail_url)
                            summary["urls_filled"] += 1
                    elif detail_url and not (a["matter_url"] or ""):
                        updates.append("matter_url=?"); params.append(detail_url)
                        summary["urls_filled"] += 1
                    if pdf_url and not (a["item_pdf_url"] or ""):
                        updates.append("item_pdf_url=?"); params.append(pdf_url)
                    if local_pdf and not (a["item_pdf_local_path"] or ""):
                        updates.append("item_pdf_local_path=?"); params.append(local_pdf)
                    if updates:
                        updates.append("updated_at=?"); params.append(now_iso())
                        params.append(a["id"])
                        conn.execute(
                            f"UPDATE appearances SET {','.join(updates)} WHERE id=?",
                            params,
                        )

            # Rebuild lifecycle timeline
            inserted = rebuild_for_matter(mid, raw_hist)
            summary["timeline_events"] += inserted

            # Auto-create stub committee appearances for every committee
            # event in the history that we don't already have stored.
            try:
                import repository as _repo
                events = get_timeline_for_matter(mid)
                with get_db() as conn:
                    mr = conn.execute(
                        "SELECT short_title FROM matters WHERE id=?", (mid,)
                    ).fetchone()
                stub_title = (mr["short_title"] if mr else "") or f"File# {fn}"
                for ev in events:
                    ev_date = ev.get("event_date") or ""
                    ev_body = ev.get("body_name") or ""
                    if not ev_date or not ev_body:
                        continue
                    bn_low = ev_body.lower()
                    if "committee" not in bn_low or "board of county" in bn_low:
                        continue
                    with get_db() as conn:
                        existing = conn.execute(
                            """SELECT a.id FROM appearances a
                               JOIN meetings m ON m.id=a.meeting_id
                               WHERE a.matter_id=? AND m.meeting_date=?
                                 AND LOWER(m.body_name)=LOWER(?)""",
                            (mid, ev_date, ev_body)
                        ).fetchone()
                    if existing:
                        continue
                    stub_mtg_id = _repo.get_or_create_meeting(
                        ev_body, ev_date, meeting_type="committee"
                    )
                    ag_item = ev.get("agenda_item", "") or ""
                    _repo.create_or_update_appearance(
                        mid, stub_mtg_id, fn, {
                            "agenda_stage": "committee",
                            "appearance_title": stub_title,
                            "committee_item_number": ag_item,
                            "raw_agenda_item_number": ag_item,
                            "appearance_notes": (
                                "[Auto-created stub from Legistar legislative "
                                f"history. Agenda Item: {ag_item or 'n/a'}. "
                                f"Action: {ev.get('action','')}]"
                            ),
                            "requires_research": 0,
                            "workflow_status": "Archived",
                        }
                    )
                    summary["stub_appearances"] = summary.get("stub_appearances", 0) + 1
            except Exception as _e:
                log.debug(f"  stub backfill skipped for matter {mid}: {_e}")

            summary["matters"] += 1
            # Small delay to avoid rate-limiting from the county site
            import time; time.sleep(0.4)
        except Exception as e:
            log.error(f"  backfill failed for File# {fn}: {e}")
            summary["errors"] += 1

    return summary
