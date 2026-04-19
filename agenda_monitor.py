"""
agenda_monitor.py — Agenda Change Detection for OCA Agenda Intelligence v6

Periodically checks Legistar for:
  1. New agendas that haven't been scraped yet
  2. Changes to already-analyzed agendas (new/modified items)

When changes are detected:
  - Creates notification records
  - Optionally auto-processes new items (AI analysis + synthesis)
  - Updates agenda_version / agenda_status on meetings
"""

import logging
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import db as _db

log = logging.getLogger("oca-agent")

# ── Configuration ─────────────────────────────────────────────
CHECK_INTERVAL_MINUTES = 45       # Background check frequency
LOOKBACK_DAYS          = 60       # How far back to scan for agendas
LOOKAHEAD_DAYS         = 90       # How far ahead to scan

# Lock to prevent concurrent scans
_scan_lock = threading.Lock()
_last_scan = {"time": None, "result": None, "running": False, "error": None}


# ── Notifications DB helper ────────────────────────────────────

def _create_notification(conn, ntype: str, title: str, body: str,
                         meeting_id: int | None = None,
                         appearance_id: int | None = None,
                         metadata: dict | None = None):
    """Insert a notification row."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO notifications
           (type, title, body, meeting_id, appearance_id, metadata, is_read, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
        (ntype, title, body, meeting_id, appearance_id,
         json.dumps(metadata) if metadata else None, now)
    )


# ── Agenda fingerprinting ──────────────────────────────────────

def _compute_agenda_fingerprint(items: list) -> str:
    """Create a hash of all matter IDs + titles on an agenda.
    If the fingerprint changes, something was added or modified."""
    parts = []
    for it in sorted(items, key=lambda x: x.get("matter_id", "")):
        parts.append(f"{it.get('matter_id','')}|{it.get('title','')}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


# ── Core scanning functions ────────────────────────────────────

def check_for_new_agendas(scraper=None) -> dict:
    """Scan Legistar for agendas NOT in our DB.

    Returns: {
        "new_meetings": [{"body_name", "date", "item_count", "agendas": [...]}],
        "checked_committees": int,
        "checked_dates": int,
    }
    """
    from scraper import MiamiDadeScraper, COMMITTEES
    if scraper is None:
        scraper = MiamiDadeScraper()

    cutoff_past = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).date()
    cutoff_future = (datetime.utcnow() + timedelta(days=LOOKAHEAD_DAYS)).date()

    new_meetings = []
    checked_committees = 0
    checked_dates = 0

    for cname, ccode in COMMITTEES.items():
        checked_committees += 1
        try:
            available_dates = scraper.get_agenda_dates(cname, ccode)
        except Exception as e:
            log.warning(f"Monitor: failed to check {cname}: {e}")
            continue

        for d in available_dates:
            try:
                dp = datetime.strptime(d.strip(), "%m/%d/%Y").date()
            except ValueError:
                continue
            if dp < cutoff_past or dp > cutoff_future:
                continue
            checked_dates += 1

            iso_date = dp.strftime("%Y-%m-%d")
            slash_date = d.strip()

            # Check if this meeting already exists in DB
            with _db.get_db() as conn:
                existing = conn.execute(
                    """SELECT id, agenda_version FROM meetings
                       WHERE body_name=? AND (meeting_date=? OR meeting_date=?)""",
                    (cname, iso_date, slash_date)
                ).fetchone()

            if not existing:
                # New meeting — get item count
                try:
                    items = scraper.get_agenda_items(ccode, cname, d)
                    item_count = len(items) if items else 0
                except Exception:
                    item_count = 0

                new_meetings.append({
                    "body_name": cname,
                    "date": iso_date,
                    "date_slash": slash_date,
                    "committee_code": ccode,
                    "item_count": item_count,
                })

    return {
        "new_meetings": new_meetings,
        "checked_committees": checked_committees,
        "checked_dates": checked_dates,
    }


def check_for_agenda_changes(scraper=None) -> dict:
    """Re-scrape all saved meetings and detect added/modified items.

    Only checks meetings in the future or within the last LOOKBACK_DAYS.

    Returns: {
        "changed_meetings": [{
            "meeting_id", "body_name", "meeting_date",
            "new_items": [...], "removed_items": [...],
            "old_version", "new_version"
        }],
        "unchanged_count": int,
    }
    """
    from scraper import MiamiDadeScraper, COMMITTEES
    if scraper is None:
        scraper = MiamiDadeScraper()

    cutoff = (datetime.utcnow() - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")

    with _db.get_db() as conn:
        meetings = conn.execute(
            """SELECT id, body_name, meeting_date, agenda_version
               FROM meetings WHERE meeting_date >= ?
               ORDER BY meeting_date DESC""",
            (cutoff,)
        ).fetchall()

    changed_meetings = []
    unchanged_count = 0

    # Build reverse lookup: committee name -> code
    name_to_code = {v: v for v in COMMITTEES.values()}
    name_to_code.update(COMMITTEES)

    for mtg in meetings:
        body = mtg["body_name"]
        mdate = mtg["meeting_date"]
        mid = mtg["id"]
        old_version = mtg["agenda_version"] or ""

        # Find committee code
        ccode = name_to_code.get(body)
        if not ccode:
            # Try fuzzy match
            for cn, cc in COMMITTEES.items():
                kw = body.split(",")[0].replace("County ", "").strip().lower()
                if kw in cn.lower():
                    ccode = cc
                    break
        if not ccode:
            continue

        # Convert date to slash format for scraper
        try:
            dt = datetime.strptime(mdate, "%Y-%m-%d")
            slash_date = f"{dt.month}/{dt.day}/{dt.year}"
        except ValueError:
            slash_date = mdate

        try:
            # Pre-fetch dates so the form cache is populated
            scraper.get_agenda_dates(body, ccode)
            items = scraper.get_agenda_items(ccode, body, slash_date)
        except Exception as e:
            log.warning(f"Monitor: failed to re-scrape {body} {mdate}: {e}")
            continue

        if not items:
            continue

        new_fingerprint = _compute_agenda_fingerprint(items)

        if new_fingerprint == old_version:
            unchanged_count += 1
            continue

        # Something changed — figure out what
        with _db.get_db() as conn:
            existing_items = conn.execute(
                """SELECT a.file_number, m.short_title
                   FROM appearances a
                   JOIN matters m ON m.id = a.matter_id
                   WHERE a.meeting_id = ?""",
                (mid,)
            ).fetchall()

        existing_files = {r["file_number"] for r in existing_items}
        scraped_files = set()
        new_items = []

        for it in items:
            fn = it.get("matter_id", "") or it.get("file_number", "")
            scraped_files.add(fn)
            if fn not in existing_files:
                new_items.append({
                    "file_number": fn,
                    "title": it.get("title", ""),
                    "item_number": it.get("committee_item_number", "")
                                   or it.get("item_number", ""),
                })

        removed_items = [
            {"file_number": fn}
            for fn in existing_files - scraped_files
        ] if existing_files else []

        if new_items or removed_items or new_fingerprint != old_version:
            changed_meetings.append({
                "meeting_id": mid,
                "body_name": body,
                "meeting_date": mdate,
                "new_items": new_items,
                "removed_items": removed_items,
                "old_version": old_version,
                "new_version": new_fingerprint,
                "total_items_now": len(items),
            })

    return {
        "changed_meetings": changed_meetings,
        "unchanged_count": unchanged_count,
    }


def update_agenda_versions(changed: list):
    """After detecting changes, update agenda_version and agenda_status."""
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    with _db.get_db() as conn:
        for ch in changed:
            new_items = ch.get("new_items", [])
            removed = ch.get("removed_items", [])

            if ch.get("old_version"):
                status = "Updated"
            else:
                status = "Initial"

            if new_items:
                status += f" (+{len(new_items)} items)"
            if removed:
                status += f" (-{len(removed)} items)"

            conn.execute(
                """UPDATE meetings SET
                    agenda_version = ?,
                    agenda_status = ?,
                    updated_at = ?
                   WHERE id = ?""",
                (ch["new_version"], status, now, ch["meeting_id"])
            )


# ── Full scan orchestrator ─────────────────────────────────────

def run_full_scan(auto_process: bool = True) -> dict:
    """Run a complete scan: check new agendas + check changes.
    Creates notifications and optionally auto-processes.

    Returns summary dict.
    """
    from scraper import MiamiDadeScraper

    if not _scan_lock.acquire(blocking=False):
        return {"error": "Scan already in progress"}

    _last_scan["running"] = True
    _last_scan["error"] = None

    try:
        scraper = MiamiDadeScraper()
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

        # Phase 1: New agendas
        log.info("Monitor: checking for new agendas…")
        new_result = check_for_new_agendas(scraper)

        # Phase 2: Changes to existing agendas
        log.info("Monitor: checking for agenda changes…")
        change_result = check_for_agenda_changes(scraper)

        # Create notifications for new meetings
        with _db.get_db() as conn:
            for nm in new_result["new_meetings"]:
                _create_notification(
                    conn, "new_agenda",
                    f"New agenda: {nm['body_name']}",
                    f"{nm['body_name']} posted their {nm['date']} agenda"
                    f" with {nm['item_count']} items.",
                    metadata=nm,
                )

            # Create notifications for changed meetings
            for ch in change_result["changed_meetings"]:
                new_ct = len(ch.get("new_items", []))
                rem_ct = len(ch.get("removed_items", []))
                parts = []
                if new_ct:
                    parts.append(f"{new_ct} new item{'s' if new_ct != 1 else ''} added")
                if rem_ct:
                    parts.append(f"{rem_ct} item{'s' if rem_ct != 1 else ''} removed")
                if not parts:
                    parts.append("agenda content changed")

                _create_notification(
                    conn, "agenda_changed",
                    f"Agenda updated: {ch['body_name']} ({ch['meeting_date']})",
                    f"{', '.join(parts)} on the {ch['body_name']} "
                    f"{ch['meeting_date']} meeting since last analysis.",
                    meeting_id=ch["meeting_id"],
                    metadata=ch,
                )

        # Update agenda versions
        if change_result["changed_meetings"]:
            update_agenda_versions(change_result["changed_meetings"])

        # Phase 3: Auto-process if enabled
        auto_results = {"analyzed": 0, "synthesized": 0}
        if auto_process and change_result["changed_meetings"]:
            auto_results = _auto_process_changes(change_result["changed_meetings"])

        summary = {
            "scan_time": now,
            "new_agendas": len(new_result["new_meetings"]),
            "changed_agendas": len(change_result["changed_meetings"]),
            "unchanged_agendas": change_result["unchanged_count"],
            "committees_checked": new_result["checked_committees"],
            "dates_checked": new_result["checked_dates"],
            "auto_analyzed": auto_results.get("analyzed", 0),
            "auto_synthesized": auto_results.get("synthesized", 0),
            "new_meetings_detail": new_result["new_meetings"],
            "changed_meetings_detail": change_result["changed_meetings"],
        }

        _last_scan["time"] = now
        _last_scan["result"] = summary
        _last_scan["running"] = False
        log.info(f"Monitor: scan complete — {summary['new_agendas']} new, "
                 f"{summary['changed_agendas']} changed")
        return summary

    except Exception as e:
        _last_scan["running"] = False
        _last_scan["error"] = str(e)
        log.error(f"Monitor: scan failed — {e}")
        return {"error": str(e)}
    finally:
        _scan_lock.release()


def _auto_process_changes(changed_meetings: list) -> dict:
    """Auto-analyze new items and re-synthesize changed meetings."""
    from analyzer import AgendaAnalyzer
    from repository import get_appearance_by_id, get_all_appearances_for_matter
    from repository import get_meeting_by_id, get_matter_by_file_number

    analyzed = 0
    synthesized = 0
    from utils import load_api_key
    analyzer = AgendaAnalyzer(load_api_key())

    for ch in changed_meetings:
        mid = ch["meeting_id"]
        new_items = ch.get("new_items", [])

        if not new_items:
            continue

        # Find appearances for the new items that need analysis
        with _db.get_db() as conn:
            for ni in new_items:
                fn = ni.get("file_number", "")
                if not fn:
                    continue
                app_row = conn.execute(
                    """SELECT a.id FROM appearances a
                       WHERE a.meeting_id=? AND a.file_number=?
                       AND (a.ai_summary_for_appearance IS NULL
                            OR a.ai_summary_for_appearance='')""",
                    (mid, fn)
                ).fetchone()
                if app_row:
                    # Queue for analysis — we'll do it inline for simplicity
                    try:
                        _auto_analyze_appearance(app_row["id"], analyzer)
                        analyzed += 1
                    except Exception as e:
                        log.warning(f"Monitor: auto-analyze failed for {fn}: {e}")

        # Re-synthesize all appearances in this meeting
        with _db.get_db() as conn:
            apps = conn.execute(
                "SELECT id FROM appearances WHERE meeting_id=?",
                (mid,)
            ).fetchall()

        for app in apps:
            try:
                _auto_synthesize_appearance(app["id"], analyzer)
                synthesized += 1
            except Exception as e:
                log.warning(f"Monitor: auto-synthesize failed for app {app['id']}: {e}")

        # Notify about auto-processing
        with _db.get_db() as conn:
            _create_notification(
                conn, "auto_processed",
                f"Auto-processed: {ch['body_name']} ({ch['meeting_date']})",
                f"Analyzed {analyzed} new items and re-synthesized "
                f"{len(apps)} items for updated meeting.",
                meeting_id=mid,
            )

    return {"analyzed": analyzed, "synthesized": synthesized}


def _auto_analyze_appearance(app_id: int, analyzer):
    """Run AI analysis on a single appearance (lightweight version)."""
    from repository import get_appearance_by_id, get_matter_by_file_number
    from repository import get_meeting_by_id
    from utils import now_iso

    a = get_appearance_by_id(app_id)
    if not a:
        return

    matter = get_matter_by_file_number(a["file_number"]) or {}
    meeting = get_meeting_by_id(a["meeting_id"]) or {}

    title = a.get("appearance_title") or matter.get("short_title") or ""
    item_num = (a.get("committee_item_number")
                or a.get("bcc_item_number")
                or a.get("raw_agenda_item_number") or "")
    committee = meeting.get("body_name") or ""

    # Get PDF text
    pdf_text = ""
    pdf_path = a.get("item_pdf_local_path") or ""
    if pdf_path and Path(pdf_path).exists():
        try:
            import fitz
            doc = fitz.open(pdf_path)
            pdf_text = "\n".join(page.get_text() for page in doc)
            doc.close()
        except Exception:
            pass

    part1, part2, full, meta = analyzer.analyze_item(
        item_number=item_num, title=title,
        pdf_text=pdf_text, committee_name=committee, prior_context="",
    )

    now = now_iso()
    with _db.get_db() as conn:
        conn.execute("""UPDATE appearances SET
            ai_summary_for_appearance=?,
            analysis_at=?, updated_at=? WHERE id=?""",
            (part1, now, now, app_id))

    log.info(f"Monitor: auto-analyzed appearance {app_id}")


def _auto_synthesize_appearance(app_id: int, analyzer):
    """Run synthesis on an appearance (reuses the synthesize_debrief method)."""
    from repository import (get_appearance_by_id, get_matter_by_file_number,
                            get_meeting_by_id, get_all_appearances_for_matter)
    from utils import now_iso

    a = get_appearance_by_id(app_id)
    if not a:
        return

    matter = get_matter_by_file_number(a["file_number"]) or {}
    meeting = get_meeting_by_id(a["meeting_id"]) or {}

    sources = {
        'item_title': a.get("appearance_title") or matter.get("short_title") or "",
        'file_number': a.get("file_number") or "",
        'body_name': meeting.get("body_name") or "",
        'meeting_date': meeting.get("meeting_date") or "",
        'ai_summary': a.get("ai_summary_for_appearance") or "",
        'watch_points': a.get("watch_points_for_appearance") or "",
        'analyst_notes': a.get("analyst_working_notes") or "",
        'reviewer_notes': a.get("reviewer_notes") or "",
        'transcript_analysis': a.get("transcript_analysis") or "",
        'pdf_text': "",
        'chat_insights': "",
        'legislative_history': a.get("leg_history_summary") or "",
    }

    # Only synthesize if there's enough source material
    has_content = any(sources.get(k) for k in
                      ['ai_summary', 'analyst_notes', 'transcript_analysis'])
    if not has_content:
        return

    debrief, watch_points, usage = analyzer.synthesize_debrief(sources)

    now = now_iso()
    with _db.get_db() as conn:
        conn.execute("""UPDATE appearances SET
            ai_summary_for_appearance=?,
            watch_points_for_appearance=?,
            updated_at=? WHERE id=?""",
            (debrief, watch_points, now, app_id))

    log.info(f"Monitor: auto-synthesized appearance {app_id}")


# ── Background scheduler ───────────────────────────────────────

_bg_thread = None
_bg_stop = threading.Event()


def start_background_monitor(interval_minutes: int = CHECK_INTERVAL_MINUTES):
    """Start the background monitoring thread."""
    global _bg_thread

    if _bg_thread and _bg_thread.is_alive():
        log.info("Monitor: background thread already running")
        return

    _bg_stop.clear()

    def _loop():
        log.info(f"Monitor: background thread started "
                 f"(interval={interval_minutes} min)")
        # Wait a bit on startup before first scan
        if _bg_stop.wait(timeout=120):
            return

        while not _bg_stop.is_set():
            try:
                run_full_scan(auto_process=True)
            except Exception as e:
                log.error(f"Monitor: background scan error — {e}")

            # Wait for next interval (or until stopped)
            if _bg_stop.wait(timeout=interval_minutes * 60):
                break

        log.info("Monitor: background thread stopped")

    _bg_thread = threading.Thread(target=_loop, daemon=True, name="agenda-monitor")
    _bg_thread.start()


def stop_background_monitor():
    """Signal the background thread to stop."""
    _bg_stop.set()
    log.info("Monitor: stop signal sent")


def get_scan_status() -> dict:
    """Return the current scan status."""
    return {
        "running": _last_scan["running"],
        "last_scan_time": _last_scan["time"],
        "last_result": _last_scan["result"],
        "error": _last_scan["error"],
        "background_active": _bg_thread is not None and _bg_thread.is_alive(),
        "interval_minutes": CHECK_INTERVAL_MINUTES,
    }
