#!/usr/bin/env python3
"""
OCA Agenda Intelligence Agent v6
=================================
Local-first agenda intelligence, workflow, and institutional memory system
for the Office of the Commission Auditor, Miami-Dade County.

New in v6:
  - SQLite persistence (matters, meetings, appearances)
  - Continuity by file number across multiple meetings/stages
  - Full-text search (FTS5)
  - Workflow tracking (New → Assigned → In Progress → Draft Complete → In Review → Finalized → Archived)
  - Database-driven Excel + Word exports
  - Prior history carry-forward in research briefs
  - CLI subcommands: process, search, history, update, notes, export

Preserves all v5 behavior:
  - Miami-Dade scraper (sub-item capture, dual-approach matter.asp parsing)
  - Claude AI analysis (Part 1 OCA Debrief + Part 2 Research Intelligence)
  - Incremental processing (skip already-processed, append new)
  - Excel yellow highlight for new rows
  - AGENDA_UPDATES.txt
"""

import os, sys, re, time, logging, argparse
from pathlib import Path
from datetime import datetime

# ── Setup path so all v6 modules are importable ──
sys.path.insert(0, str(Path(__file__).parent))

import db as database
from db import init_db
from utils import parse_date_arg, load_api_key, now_iso, safe_filename, extract_watch_points
from scraper import MiamiDadeScraper, extract_pdf_text, IMAGE_ONLY_SENTINEL
from analyzer import AgendaAnalyzer, compute_input_hash, SOP_PROMPT, MODEL as ANALYZER_MODEL
import repository as repo
import workflow as wf
import search as srch
import exporters

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("oca-agent")


# ─────────────────────────────────────────────────────────────
# Core processing loop
# ─────────────────────────────────────────────────────────────

def process_committees(committees: dict, parsed_date: datetime, mode: str,
                        output_dir: Path, analyzer: AgendaAnalyzer,
                        scraper: MiamiDadeScraper,
                        progress_callback=None) -> dict:
    """
    Main v6 processing loop.
    Data flow:
      1. Scrape meeting + agenda data
      2. Parse agenda items + matter metadata
      3. Determine whether each file number is new or known (Case A / Case B)
      4. Persist matter and meeting data
      5. Create or update appearance records (with carry-forward)
      6. Run AI analysis
      7. Save summaries into DB
      8. Generate Excel + Word from DB
      9. Write AGENDA_UPDATES.txt
    """
    from paths import PDF_CACHE_DIR
    pdf_dir = PDF_CACHE_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    total_items = 0
    total_new   = 0
    total_files = 0
    results_summary = []

    # ── Progress helper ───────────────────────────────────────
    # `progress_callback` may be the old 1-arg callable (just a message) or
    # a new signature (message, phase=..., pct=...). We wrap it so both
    # work. `phase` is one of: scanning, analyzing, exporting, done.
    def _emit(message: str, phase: str | None = None, pct: float | None = None):
        if not progress_callback:
            return
        try:
            progress_callback(message, phase=phase, pct=pct)
        except TypeError:
            # Old signature — fall back to message-only
            progress_callback(message)

    _emit("Starting — scanning Legistar for matching agendas…",
          phase="scanning", pct=1)

    # Rough progress model: scanning = 0-15%, analyzing = 15-90%, exporting = 90-100%
    n_committees = max(1, len(committees))

    for ci, (cname, ccode) in enumerate(committees.items()):
        cmte_base_pct = 1 + (ci / n_committees) * 14  # scanning phase 1-15%
        _emit(f"Scanning {cname} for matching agendas ({ci+1}/{len(committees)})…",
              phase="scanning", pct=cmte_base_pct)

        log.info(f"\n  {cname}")
        if mode == "single":
            matching_dates = scraper.get_matching_dates(cname, ccode, exact_date=parsed_date)
        else:
            matching_dates = scraper.get_matching_dates(cname, ccode, from_date=parsed_date)

        if not matching_dates:
            log.info(f"  No matching agendas")
            _emit(f"{cname}: no agenda found for this date",
                  phase="scanning", pct=cmte_base_pct)
            results_summary.append({
                "committee": cname, "status": "No agenda found", "items": 0, "new": 0
            })
            continue

        for adate in matching_dates:
            _emit(f"{cname} ({adate}): reading agenda item list…",
                  phase="scanning", pct=cmte_base_pct + 2)
            items = scraper.get_agenda_items(ccode, cname, adate)
            log.info(f"  {adate}: {len(items)} items on site")

            if not items:
                _emit(f"{cname} ({adate}): 0 items on agenda — skipping",
                      phase="scanning", pct=cmte_base_pct + 3)
                continue

            # Determine stage/meeting_type from body name.
            # BCC agendas are Board of County Commissioners (full board);
            # everything else is a committee.
            _cn_low = (cname or "").lower()
            if ("board of county commissioners" in _cn_low
                    or _cn_low.strip() in ("bcc", "commission")
                    or "county commission" in _cn_low):
                _stage = "bcc"
                _mtype = "bcc"
            else:
                _stage = "committee"
                _mtype = "committee"

            # Get or create the meeting record
            meeting_id = repo.get_or_create_meeting(cname, adate, meeting_type=_mtype)

            # Set agenda_version fingerprint and agenda_status
            try:
                from agenda_monitor import _compute_agenda_fingerprint
                _fp = _compute_agenda_fingerprint(items)
                import db as _fpdb
                with _fpdb.get_db() as _fpc:
                    _fpc.execute(
                        """UPDATE meetings SET agenda_version=?, agenda_status=?,
                           item_count=?, last_scan_at=? WHERE id=?""",
                        (_fp, "Analyzed", len(items),
                         datetime.now().strftime("%Y-%m-%d %H:%M:%S"), meeting_id))
            except Exception:
                pass  # Non-critical — don't block processing

            # Check which file numbers are already processed for this meeting
            processed_ids = repo.get_processed_file_numbers_for_meeting(meeting_id)

            new_items = [
                it for it in items
                if str(it.get("matter_id", "")) not in processed_ids
            ]

            if not new_items:
                log.info(f"  SKIPPING: All {len(items)} items already in DB")
                _emit(f"{cname} ({adate}): all {len(items)} items already analyzed — skipping",
                      phase="scanning", pct=cmte_base_pct + 5)
                results_summary.append({
                    "committee": cname, "date": adate,
                    "status": f"Skipped (all {len(items)} items already processed)",
                    "items": len(items), "new": 0
                })
                continue

            log.info(f"  {len(new_items)} new items to process (out of {len(items)} total)")
            _emit(f"{cname} ({adate}): {len(new_items)} new item(s) to analyze "
                  f"(out of {len(items)} total on agenda)",
                  phase="analyzing", pct=15)

            # Sort so base items (e.g. '3C') are analyzed before their
            # supplements/sub-items ('3C Supplement', '3C1'). This lets us
            # pass the base analysis as within-meeting prior context so
            # Claude focuses on what CHANGES rather than re-summarizing.
            from utils import parse_item_family, sort_items_by_family
            new_items = sort_items_by_family(new_items)

            # Index of results for in-meeting families, keyed by full item
            # number (e.g. '3C'). When '3C1' or '3C SUPPLEMENT' is analyzed,
            # we look up '3C' here and inject it as sibling context.
            sibling_results: dict[str, dict] = {}

            processed_appearances = []
            n_new = max(1, len(new_items))

            for i, item in enumerate(new_items, 1):
                file_num = str(item.get("matter_id", ""))
                if not file_num:
                    continue

                # analyzing phase covers pct 15% → 90%
                item_pct = 15 + ((i - 1) / n_new) * 75
                _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) — fetching details…",
                      phase="analyzing", pct=item_pct)
                log.info(f"    [{i}/{len(new_items)}] File# {file_num}...")

                # Fetch routing info from matter.asp
                item = scraper.get_item_detail(item)
                time.sleep(0.3)

                today = datetime.utcnow().strftime("%Y-%m-%d")

                # ── Upsert matter (Case A or B) ──────────────────
                matter_fields = {
                    "short_title":    item.get("short_title", item.get("title", "")),
                    "full_title":     item.get("full_title", ""),
                    "file_type":      item.get("file_type", ""),
                    "sponsor":        item.get("sponsor", ""),
                    "control_body":   item.get("control", ""),
                    "current_status": item.get("status", ""),
                    "legislative_notes": item.get("notes", ""),
                    "first_seen_date": today,
                    "last_seen_date":  today,
                    "current_stage":   _stage,
                }
                matter_id = repo.upsert_matter(file_num, matter_fields)

                # ── Create appearance (with carry-forward if repeat) ──
                app_fields = {
                    "committee_item_number": item.get("committee_item_number", ""),
                    "bcc_item_number":       item.get("bcc_agenda_item_number", ""),
                    "raw_agenda_item_number": item.get("item_number", ""),
                    "agenda_stage":          _stage,
                    "appearance_title":      item.get("short_title", item.get("title", "")),
                    "appearance_notes":      item.get("notes", ""),
                    "requires_research":     1,
                    "matter_url":            item.get("detail_url", ""),
                    "item_pdf_url":          item.get("pdf_url", ""),
                }
                appearance_id, is_new_appearance = repo.create_or_update_appearance(
                    matter_id, meeting_id, file_num, app_fields
                )

                # ── Case linker (Session 1 — April 2026) ─────────
                # Attach this matter to a Case (lifecycle entity above
                # matter). Idempotent — re-running the scraper is safe.
                # Linker handles: application-number extraction,
                # case find-or-create, role/stage classification, and
                # membership write with confidence-based status.
                try:
                    import cases as _cases
                    # Gather text that's most likely to contain the
                    # application number
                    scan_text = " ".join(filter(None, [
                        item.get("short_title", ""),
                        item.get("full_title", ""),
                        item.get("notes", ""),
                        item.get("page_text", "")[:4000],  # bounded
                    ]))
                    link_result = _cases.link_matter_to_case(
                        matter_id=matter_id,
                        short_title=item.get("short_title", item.get("title", "")),
                        full_title=item.get("full_title", ""),
                        file_type=item.get("file_type", ""),
                        file_number=file_num,
                        committee_item_number=item.get("committee_item_number", ""),
                        agenda_stage=_stage,
                        body_name=cname,
                        extra_text=scan_text,
                    )
                    log.info(
                        f"    Case link: {link_result['application_number']} "
                        f"({link_result['case_type']}) — "
                        f"role={link_result['role_label']}, "
                        f"stage={link_result['stage_category']}, "
                        f"status={link_result['link_status']}"
                    )
                    # Record an event for this appearance
                    _cases.record_case_event(
                        case_id=link_result["case_id"],
                        matter_id=matter_id,
                        appearance_id=appearance_id,
                        event_date=adate,
                        event_type="agenda_appearance",
                        stage_category=link_result["stage_category"],
                        stage_label=link_result["stage_label"],
                        body_name=cname,
                        action=link_result["role_label"],
                        source="ingest",
                    )
                except Exception as _ce:
                    log.warning(f"    Case linker failed (non-fatal): {_ce}")

                # Parse and persist lifecycle events from the leg history text
                try:
                    import lifecycle as _lc
                    raw_hist = item.get("legislative_history_raw", "") or ""
                    if raw_hist:
                        _lc.rebuild_for_matter(matter_id, raw_hist)

                        # ── Auto-create STUB committee appearances from
                        # parsed Legistar history so that BCC items always
                        # show their prior committee date/#. The stubs carry
                        # agenda_stage='committee', a friendly body name, and
                        # workflow_status='Archived' so they don't pollute the
                        # active workload. Researchers can still open them and
                        # add notes; notes carry forward to the BCC appearance
                        # via existing continuity logic. No-op if an appearance
                        # already exists for that (matter, body, date) combo.
                        events = _lc.get_timeline_for_matter(matter_id)
                        for ev in events:
                            ev_date = ev.get("event_date") or ""
                            ev_body = ev.get("body_name") or ""
                            if not ev_date or not ev_body:
                                continue
                            bn_low = ev_body.lower()
                            # Only create stubs for committee-like bodies (skip
                            # BCC lines — we already have the real BCC row).
                            is_cmte = ("committee" in bn_low
                                       and "board of county" not in bn_low)
                            if not is_cmte:
                                continue
                            # Skip if the lifecycle event date matches the
                            # current meeting date — those items are already
                            # in this meeting, no stub needed.
                            from repository import _normalize_date as _nd
                            if ev_date == _nd(adate):
                                continue
                            # Skip if we already have an appearance for this
                            # matter at a committee meeting on/near this date.
                            # Use fuzzy body match (LIKE) since lifecycle names
                            # may differ from Legistar committee names.
                            try:
                                from db import get_db as _gdb
                                from repository import _date_variants
                                _ev_variants = _date_variants(ev_date)
                                # Extract a keyword from the body name for fuzzy match
                                # e.g. "Infrastructure" from "Infrastructure, Operations..."
                                _body_kw = ev_body.split(",")[0].split(" and ")[0].strip()
                                _body_kw = _body_kw.replace("County ", "").strip()
                                with _gdb() as _c:
                                    existing = None
                                    for _dv in _ev_variants:
                                        # Try exact match first
                                        existing = _c.execute(
                                            """SELECT a.id FROM appearances a
                                               JOIN meetings m ON m.id=a.meeting_id
                                               WHERE a.matter_id=? AND m.meeting_date=?
                                                 AND LOWER(m.body_name)=LOWER(?)""",
                                            (matter_id, _dv, ev_body)
                                        ).fetchone()
                                        if existing:
                                            break
                                        # Fuzzy: body name contains the keyword
                                        if _body_kw and len(_body_kw) > 4:
                                            existing = _c.execute(
                                                """SELECT a.id FROM appearances a
                                                   JOIN meetings m ON m.id=a.meeting_id
                                                   WHERE a.matter_id=? AND m.meeting_date=?
                                                     AND LOWER(m.body_name) LIKE ?""",
                                                (matter_id, _dv,
                                                 f"%{_body_kw.lower()}%")
                                            ).fetchone()
                                            if existing:
                                                break
                                if existing:
                                    continue
                            except Exception:
                                pass
                            try:
                                # Before creating a new meeting for the stub,
                                # check if a meeting with a similar body name
                                # already exists on this date (avoids phantom
                                # duplicates from body name mismatches between
                                # lifecycle parser and Legistar committee names).
                                from db import get_db as _gdb2
                                _stub_body_kw = ev_body.split(",")[0].split(" and ")[0].strip()
                                _stub_body_kw = _stub_body_kw.replace("County ", "").strip()
                                _found_mtg = None
                                if _stub_body_kw and len(_stub_body_kw) > 4:
                                    for _sdv in _date_variants(ev_date):
                                        with _gdb2() as _sc:
                                            _found_mtg = _sc.execute(
                                                """SELECT id, body_name FROM meetings
                                                   WHERE meeting_date=?
                                                     AND LOWER(body_name) LIKE ?""",
                                                (_sdv, f"%{_stub_body_kw.lower()}%")
                                            ).fetchone()
                                        if _found_mtg:
                                            break
                                if _found_mtg:
                                    stub_mtg_id = _found_mtg["id"]
                                else:
                                    stub_mtg_id = repo.get_or_create_meeting(
                                        ev_body, ev_date, meeting_type="committee"
                                    )
                                ag_item = ev.get("agenda_item", "") or ""
                                stub_fields = {
                                    "agenda_stage": "committee",
                                    "appearance_title": (item.get("short_title")
                                                          or item.get("title") or ""),
                                    "appearance_notes": (
                                        "[Auto-created stub from Legistar legislative "
                                        "history so cross-stage tracking works. "
                                        f"Agenda Item: {ag_item or 'n/a'}. "
                                        f"Action: {ev.get('action','')}]"
                                    ),
                                    "requires_research": 0,
                                    "workflow_status": "Archived",
                                    "raw_agenda_item_number": ag_item,
                                    "committee_item_number": ag_item,
                                }
                                repo.create_or_update_appearance(
                                    matter_id, stub_mtg_id, file_num, stub_fields
                                )
                            except Exception as _e:
                                log.debug(f"  stub cmte appearance skipped: {_e}")
                except Exception as _e:
                    log.debug(f"  lifecycle rebuild skipped: {_e}")

                # ── Download PDF ──────────────────────────────────
                pdf_text = ""
                item_pdf_local = ""
                if item.get("pdf_path"):
                    item_pdf_local = item["pdf_path"]
                    _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) — reading cached PDF…",
                          phase="analyzing", pct=item_pct + 1)
                    pdf_text = extract_pdf_text(Path(item["pdf_path"]))
                elif item.get("pdf_url"):
                    _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) — downloading PDF…",
                          phase="analyzing", pct=item_pct + 1)
                    pp = scraper.download_pdf(item["pdf_url"], pdf_dir)
                    if pp:
                        item_pdf_local = str(pp)
                        _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) — extracting text from PDF…",
                              phase="analyzing", pct=item_pct + 2)
                        pdf_text = extract_pdf_text(pp)

                # Persist the local PDF path on the appearance if we got one
                if item_pdf_local:
                    try:
                        from db import get_db
                        from utils import now_iso
                        with get_db() as conn:
                            conn.execute(
                                "UPDATE appearances SET item_pdf_local_path=?, updated_at=? WHERE id=?",
                                (item_pdf_local, now_iso(), appearance_id)
                            )
                    except Exception as _e:
                        log.warning(f"  Could not persist item_pdf_local_path: {_e}")

                # ── Build prior context for carried-forward items ──
                prior_context = ""
                prior_pdf_text = ""
                prior_summary = ""
                prior_notes = ""
                prior_meeting_date = ""
                prior_body_name = ""
                if is_new_appearance:
                    current_app = repo.get_appearance_by_id(appearance_id)
                    if current_app and current_app.get("prior_appearance_id"):
                        prior_app = repo.get_appearance_by_id(
                            current_app["prior_appearance_id"]
                        )
                        if prior_app:
                            prior_summary = (
                                prior_app.get("ai_summary_for_appearance", "") or ""
                            )
                            prior_notes = (
                                prior_app.get("analyst_working_notes", "") or ""
                            )
                            # Get meeting info for date attribution
                            prior_meeting = repo.get_meeting_by_id(prior_app.get("meeting_id"))
                            if prior_meeting:
                                prior_meeting_date = prior_meeting.get("meeting_date", "")
                                prior_body_name = prior_meeting.get("body_name", "")
                            # Build rich prior context: AI summary + researcher notes
                            ctx_parts = []
                            if prior_summary:
                                ctx_parts.append(
                                    f"[AI ANALYSIS from {prior_body_name} {prior_meeting_date}]\n"
                                    f"{prior_summary}"
                                )
                            if prior_notes:
                                # Strip any previously carried-forward prefixes
                                # (handles both old "[Carried from prior appearance 118]"
                                #  and new "[Carried from Body Name, 2026-03-10, Item 2A]")
                                clean_notes = re.sub(
                                    r'^\[Carried from [^\]]+\]\s*', '', prior_notes
                                ).strip()
                                ctx_parts.append(
                                    f"[RESEARCHER NOTES from {prior_body_name} {prior_meeting_date}]\n"
                                    f"{clean_notes}"
                                )
                            prior_context = "\n\n".join(ctx_parts)
                            # Try to get the prior committee PDF text for
                            # change detection
                            prior_pdf_path = prior_app.get("item_pdf_local_path", "")
                            if prior_pdf_path and Path(prior_pdf_path).exists():
                                try:
                                    prior_pdf_text = extract_pdf_text(Path(prior_pdf_path))
                                    if prior_pdf_text == IMAGE_ONLY_SENTINEL:
                                        prior_pdf_text = ""
                                except Exception:
                                    prior_pdf_text = ""

                # ── AI Analysis ───────────────────────────────────
                cn = item.get("committee_item_number", "")
                display = f"{cn} (File# {file_num})" if cn else f"File# {file_num}"

                # ── Within-meeting sibling context ────────────────
                # If this item is a supplement, substitute, or sub-item of a
                # base item that was already analyzed in THIS meeting run,
                # append the base's Part 1 to prior_context and tell Claude
                # to focus on what CHANGES rather than re-summarizing.
                base_num, rel_kind = parse_item_family(cn)
                if rel_kind and rel_kind != "base" and base_num:
                    # Look up any already-analyzed base item with this prefix.
                    # Accept exact base ('3C') or any 'base' variant stored.
                    base_hit = sibling_results.get(base_num)
                    if base_hit:
                        rel_label = {
                            "supplement": "a SUPPLEMENT to",
                            "substitute": "a SUBSTITUTE for",
                            "sub":        "a SUB-ITEM of",
                        }.get(rel_kind, "related to")
                        sibling_block = (
                            f"[WITHIN-MEETING BASE ITEM — This item ({cn}) is "
                            f"{rel_label} item {base_hit['num']} on the same "
                            f"agenda. The base item has already been analyzed. "
                            f"Your job for THIS item is to identify what is "
                            f"ADDED, CHANGED, or SUBSTITUTED relative to the "
                            f"base — not to re-summarize the base.]\n\n"
                            f"BASE ITEM {base_hit['num']} — {base_hit['short_title']}\n"
                            f"{base_hit['part1']}"
                        )
                        prior_context = (
                            (prior_context + "\n\n" + sibling_block).strip()
                            if prior_context else sibling_block
                        )
                        log.info(f"    {display} recognized as {rel_kind} of "
                                 f"{base_hit['num']} — injecting sibling context")

                # ── Cross-reference context (Session 4) ──────────
                # Pull summaries of OTHER items in the same Case AND
                # summaries from companion Cases (CDMP ↔ Zoning for
                # same project). Claude uses these to cross-reference
                # in its brief per the CROSS-REFERENCE RULE in SOP.
                try:
                    import cases as _cases_mod
                    # The linker earlier in this loop already wrote a
                    # case_id onto the appearance. Read it back.
                    app_row = repo.get_appearance_by_id(appearance_id) or {}
                    this_case_id = app_row.get("case_id")
                    this_matter_id = matter_id
                    if this_case_id:
                        crossref = _cases_mod.build_crossref_context(
                            case_id=this_case_id,
                            current_matter_id=this_matter_id,
                            max_chars=3500,
                        )
                        if crossref:
                            prior_context = (
                                (prior_context + "\n\n" + crossref).strip()
                                if prior_context else crossref
                            )
                            log.info(f"    {display} — injected cross-reference "
                                     f"context ({len(crossref):,} chars)")
                except Exception as _ce:
                    log.warning(f"    Cross-reference context build failed "
                                f"(non-fatal): {_ce}")

                # Guard: if the PDF is image-only (no extractable text and
                # OCR unavailable), don't ask the LLM — it will just produce
                # a confused "I can't see content" message. Flag the item
                # for manual review and move on.
                if pdf_text == IMAGE_ONLY_SENTINEL or (not pdf_text and not item.get("page_text", "")):
                    log.warning(f"  {display}: no readable PDF text — flagging for manual review")
                    manual_msg = (
                        "⚠ MANUAL REVIEW NEEDED — Source PDF appears to be image-only "
                        "(scanned) or empty. No text could be extracted for AI analysis. "
                        "A researcher will need to open the item PDF directly and draft "
                        "this debrief manually.\n\n"
                        f"Item: {display}\n"
                        f"Title: {item.get('short_title', item.get('title', ''))}\n"
                        f"Legistar: {item.get('matter_url', '')}\n"
                        f"PDF: {item.get('pdf_url', '') or item.get('pdf_path', '')}"
                    )
                    part1 = manual_msg
                    part2 = manual_msg
                    full  = manual_msg
                    analysis_meta = None
                    leg_summary = ""
                    leg_usage = {"input_tokens": 0, "output_tokens": 0,
                                 "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0}
                else:
                    # ── Content-hash cache lookup ────────────────
                    # Compose what we would send, hash it, and see if any
                    # prior appearance already has an analysis for these
                    # exact inputs. If so, reuse it — zero tokens spent.
                    _user_msg, _trim_meta = analyzer.build_user_message(
                        display,
                        item.get("short_title", item.get("title", "")),
                        pdf_text,
                        cname,
                        item.get("page_text", ""),
                        prior_context=prior_context,
                    )
                    input_hash = compute_input_hash(ANALYZER_MODEL, SOP_PROMPT, _user_msg)
                    cached = repo.find_cached_analysis(input_hash)
                    if cached:
                        log.info(f"    HIT cache (from appearance "
                                 f"{cached['source_appearance_id']}) — skipping API call")
                        _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) "
                              "— reused cached analysis (\u2714 saved API call)",
                              phase="analyzing", pct=item_pct + 60)
                        part1 = cached["part1"]
                        part2 = cached["part2"]
                        full  = (part1 + "\n\n" + part2).strip()
                        analysis_meta = {
                            "input_hash": input_hash,
                            "usage": {"input_tokens": 0, "output_tokens": 0,
                                      "cache_read_input_tokens": 0,
                                      "cache_creation_input_tokens": 0},
                            "cache_source": "db",
                            **_trim_meta,
                        }
                        # Reuse leg summary too if the prior item had one
                        leg_summary = cached.get("leg_summary", "") or ""
                        leg_usage = {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0}
                    else:
                        _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) "
                              "— analyzing with Claude (Part 1 + Part 2)…",
                              phase="analyzing", pct=item_pct + 20)
                        part1, part2, full, analysis_meta = analyzer.analyze_item(
                            display,
                            item.get("short_title", item.get("title", "")),
                            pdf_text,
                            cname,
                            item.get("page_text", ""),
                            prior_context=prior_context,
                        )
                        _usage_dbg = (analysis_meta or {}).get("usage") or {}
                        _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) "
                              f"— Claude responded ({_usage_dbg.get('input_tokens', 0):,} in / "
                              f"{_usage_dbg.get('output_tokens', 0):,} out tokens)",
                              phase="analyzing", pct=item_pct + 55)
                        time.sleep(20)

                        # ── Leg history summary ───────────────────
                        leg_summary = ""
                        leg_usage = {"input_tokens": 0, "output_tokens": 0,
                                     "cache_read_input_tokens": 0,
                                     "cache_creation_input_tokens": 0}
                        raw_hist = item.get("legislative_history_raw", "")
                        if raw_hist and len(raw_hist) > 50:
                            _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) "
                                  "— summarizing legislative history…",
                                  phase="analyzing", pct=item_pct + 65)
                            leg_summary, leg_usage = analyzer.summarize_leg_history(
                                raw_hist, item.get("short_title", "")
                            )
                            time.sleep(20)

                # ── Extract watch points ──────────────────────────
                part1_clean, watch = extract_watch_points(part1)

                # ── Record in sibling index for later family members ──
                # Key on the item's own number (not just the base) so that
                # '3C Supplement' can reference '3C', and '3C1' can reference
                # '3C' — both look up 'sibling_results["3C"]'. We overwrite
                # only if this is the first 'base' result for that key, so
                # a later supplement doesn't shadow the original base.
                if cn:
                    item_base, item_kind = parse_item_family(cn)
                    # Store against the base key if this item IS the base.
                    # Also store against its own full number so exact lookups
                    # (e.g. if a sub-sub-item references '3C1' directly) work.
                    short_t = item.get("short_title", item.get("title", ""))
                    record = {
                        "num": cn,
                        "short_title": short_t,
                        "part1": part1_clean[:4000],  # cap to keep prompt size sane
                    }
                    if item_kind == "base" and item_base:
                        sibling_results.setdefault(item_base, record)
                    # Always also index by the full number
                    sibling_results.setdefault(cn, record)

                # ── Change detection (committee → BCC) ───────────
                # If this item appeared at a prior stage (committee),
                # compare the two versions and note what changed.
                change_notes = ""
                change_usage = {"input_tokens": 0, "output_tokens": 0,
                                "cache_read_input_tokens": 0,
                                "cache_creation_input_tokens": 0}
                if prior_context and (prior_pdf_text or prior_summary) and pdf_text:
                    _emit(f"{cname}: item {i}/{len(new_items)} (File# {file_num}) "
                          "— detecting changes from committee version…",
                          phase="analyzing", pct=item_pct + 70)
                    try:
                        change_notes, change_usage = analyzer.detect_changes(
                            item.get("short_title", item.get("title", "")),
                            prior_summary, prior_pdf_text, pdf_text,
                        )
                        if change_notes:
                            log.info(f"    Changes detected: {change_notes[:100]}")
                        time.sleep(20)
                    except Exception as _ce:
                        log.warning(f"    Change detection failed: {_ce}")

                # ── Build carried-forward notes ──────────────────
                # Combine prior notes + change detection into the
                # analyst working notes for this appearance.
                # Build readable label: "Government Operations 2026-03-10"
                prior_label = f"{prior_body_name} {prior_meeting_date}".strip() or "prior committee appearance"
                carried_notes_parts = []
                if prior_notes:
                    # Strip any previously-carried prefixes to avoid nesting
                    clean_prior = re.sub(
                        r'^\[Carried from [^\]]+\]\s*', '', prior_notes
                    ).strip()
                    # Include the prior committee item number for reference
                    prior_item = prior_app.get("committee_item_number") or prior_app.get("bcc_item_number") or ""
                    item_ref = f", Item {prior_item}" if prior_item else ""
                    carried_notes_parts.append(
                        f"[Carried from {prior_label}{item_ref}]\n{clean_prior}"
                    )
                # Always include change detection result — even "no changes"
                if change_notes:
                    if "no substantive changes" in change_notes.lower():
                        carried_notes_parts.append(
                            f"[No changes from {prior_label} to BCC version]\n{change_notes}"
                        )
                    else:
                        carried_notes_parts.append(
                            f"[Changes from {prior_label} to BCC version]\n{change_notes}"
                        )
                if carried_notes_parts:
                    combined_notes = "\n\n".join(carried_notes_parts)
                    try:
                        from db import get_db as _gdb
                        with _gdb() as _c:
                            existing_notes = _c.execute(
                                "SELECT analyst_working_notes FROM appearances WHERE id=?",
                                (appearance_id,)
                            ).fetchone()
                            old = (existing_notes["analyst_working_notes"] or "") if existing_notes else ""
                            if old:
                                combined_notes = old + "\n\n" + combined_notes
                            _c.execute(
                                "UPDATE appearances SET analyst_working_notes=?, updated_at=? WHERE id=?",
                                (combined_notes, now_iso(), appearance_id)
                            )
                        log.info(f"    Carried forward notes + changes for File# {file_num}")
                    except Exception as _ne:
                        log.warning(f"    Notes carry-forward failed: {_ne}")

                # ── Persist AI results (with hash + token counts) ──
                _usage = (analysis_meta or {}).get("usage") or {}
                _tokens_in = ((_usage.get("input_tokens") or 0) +
                              (leg_usage.get("input_tokens") or 0) +
                              (change_usage.get("input_tokens") or 0))
                _tokens_out = ((_usage.get("output_tokens") or 0) +
                               (leg_usage.get("output_tokens") or 0) +
                               (change_usage.get("output_tokens") or 0))
                _tokens_cached = ((_usage.get("cache_read_input_tokens") or 0) +
                                  (leg_usage.get("cache_read_input_tokens") or 0) +
                                  (change_usage.get("cache_read_input_tokens") or 0))
                _hash = (analysis_meta or {}).get("input_hash")

                repo.update_appearance_ai(
                    appearance_id, part1_clean, part2, watch, leg_summary,
                    input_hash=_hash,
                    tokens_in=_tokens_in, tokens_out=_tokens_out,
                    cached_tokens=_tokens_cached,
                    ai_risk_level=(analysis_meta or {}).get("ai_risk_level", ""),
                    ai_risk_reason=(analysis_meta or {}).get("ai_risk_reason", ""),
                )
                repo.update_matter_ai_fields(matter_id, part1_clean, watch)

                # ── Append to token-usage CSV ─────────────────────
                try:
                    _log_token_usage(output_dir, {
                        "timestamp":   now_iso(),
                        "committee":   cname,
                        "meeting_date": adate,
                        "file_number": file_num,
                        "item":        display,
                        "title":       (item.get("short_title") or item.get("title") or "")[:120],
                        "cache_source": (analysis_meta or {}).get("cache_source", "api"),
                        "pdf_trim":    (analysis_meta or {}).get("pdf_trim_strategy", ""),
                        "pdf_chars_in":   (analysis_meta or {}).get("pdf_chars_in", 0),
                        "pdf_chars_sent": (analysis_meta or {}).get("pdf_chars_sent", 0),
                        "tokens_in":   _tokens_in,
                        "tokens_out":  _tokens_out,
                        "tokens_cached": _tokens_cached,
                    })
                except Exception as _e:
                    log.debug(f"  token log write skipped: {_e}")

                processed_appearances.append(appearance_id)
                total_items += 1
                total_new   += 1 if is_new_appearance else 0

            # ── Generate exports ─────────────────────────────────
            if processed_appearances:
                _emit(f"  Generating Excel + Word exports for {cname}...",
                      phase="exporting", pct=92)
                gen_files = exporters.export_for_meeting(meeting_id, output_dir)
                total_files += len(gen_files)
                _log_updates(output_dir, cname, new_items, adate)
                _emit(f"  Created {len(gen_files)} export file(s) for {cname}.",
                      phase="exporting", pct=96)

                results_summary.append({
                    "committee": cname,
                    "date":      adate,
                    "status":    f"Processed {len(processed_appearances)} item(s)",
                    "items":     len(items),
                    "new":       len(processed_appearances),
                    "meeting_id": meeting_id,
                })

    _emit(f"Done — {total_items} item(s) processed, {total_files} export file(s) created.",
          phase="done", pct=100)
    return {
        "total_items_analyzed": total_items,
        "total_new_items":      total_new,
        "total_files":          total_files,
        "details":              results_summary,
    }


def _log_updates(output_dir: Path, committee_name: str, items: list, date: str):
    log_path = output_dir / "AGENDA_UPDATES.txt"
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\n")
        f.write(f"[{ts}] {committee_name} - {date}\n")
        f.write(f"Items processed: {len(items)}\n")
        for item in items:
            cn  = item.get("committee_item_number", "")
            mid = item.get("matter_id", "")
            ttl = item.get("short_title", item.get("title", ""))[:80]
            f.write(f"  {cn} (File# {mid}): {ttl}\n")
        f.write(f"{'='*60}\n")


# Per-item token-usage log. Each row records what went to Claude (or was
# served from our DB cache). Open this file in Excel to audit cost drivers:
#   - cache_source='db' means we paid $0 for that item
#   - tokens_in * $1/M + tokens_out * $5/M = approximate Haiku spend
import csv as _csv
_TOKEN_LOG_FIELDS = [
    "timestamp", "committee", "meeting_date", "file_number", "item", "title",
    "cache_source", "pdf_trim", "pdf_chars_in", "pdf_chars_sent",
    "tokens_in", "tokens_out", "tokens_cached",
]

def _log_token_usage(output_dir: Path, row: dict):
    """Append one per-item row to token_log.csv under the output dir."""
    path = output_dir / "token_log.csv"
    write_header = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=_TOKEN_LOG_FIELDS, extrasaction="ignore")
        if write_header:
            w.writeheader()
        w.writerow(row)


# ─────────────────────────────────────────────────────────────
# CLI command handlers
# ─────────────────────────────────────────────────────────────

def cmd_process(args):
    init_db()
    try:
        api_key = load_api_key()
    except RuntimeError as e:
        print(f"ERROR: {e}"); sys.exit(1)

    analyzer = AgendaAnalyzer(api_key)
    scraper  = MiamiDadeScraper()

    if args.date:
        try:
            parsed_date = parse_date_arg(args.date)
        except ValueError as e:
            print(f"ERROR: {e}"); sys.exit(1)
        mode, label = "single", args.date
    else:
        try:
            parsed_date = parse_date_arg(args.from_date)
        except ValueError as e:
            print(f"ERROR: {e}"); sys.exit(1)
        mode, label = "range", f"{args.from_date} onward"

    all_committees = scraper.get_committees()
    if args.committee:
        committees = {k: v for k, v in all_committees.items()
                      if args.committee.lower() in k.lower()}
        if not committees:
            print(f"No committee matching '{args.committee}'"); sys.exit(1)
    else:
        committees = all_committees

    output_dir = Path(args.output_dir)

    print(f"\n{'='*60}")
    print(f"  OCA AGENDA INTELLIGENCE AGENT v6")
    print(f"  Mode:       {'Single date' if mode == 'single' else 'From date onward'}")
    print(f"  Date(s):    {label}")
    print(f"  Committees: {len(committees)}")
    print(f"  Output:     {output_dir}/")
    print(f"  Database:   {database.DB_PATH}")
    print(f"{'='*60}\n")

    results = process_committees(committees, parsed_date, mode,
                                  output_dir, analyzer, scraper)

    print(f"\n{'='*60}")
    print(f"  COMPLETE")
    print(f"  Items analyzed:  {results['total_items_analyzed']}")
    print(f"  New items:       {results['total_new_items']}")
    print(f"  Files generated: {results['total_files']} (in {output_dir}/)")
    print(f"{'='*60}\n")


def cmd_search(args):
    init_db()
    if args.file:
        result = srch.search_by_file_number(args.file)
        if not result:
            print(f"File number {args.file} not found.")
            return
        _print_matter(result)
    elif args.keyword:
        results = srch.search_by_keyword(args.keyword, limit=args.limit)
        if not results:
            print(f"No results for '{args.keyword}'")
            return
        print(f"\n{len(results)} result(s) for '{args.keyword}':\n")
        for r in results:
            print(f"  File# {r.get('file_number','?'):12}  "
                  f"{r.get('meeting_date','?'):12}  "
                  f"{r.get('body_name','?'):35}  "
                  f"{(r.get('short_title') or r.get('appearance_title',''))[:60]}")
    elif args.sponsor:
        results = srch.search_by_sponsor(args.sponsor, limit=args.limit)
        if not results:
            print(f"No results for sponsor '{args.sponsor}'")
            return
        print(f"\n{len(results)} result(s) for sponsor '{args.sponsor}':\n")
        for r in results:
            print(f"  File# {r.get('file_number','?'):12}  "
                  f"{r.get('meeting_date','?'):12}  "
                  f"{r.get('short_title','')[:60]}")
    else:
        print("Specify --file, --keyword, or --sponsor")


def cmd_history(args):
    init_db()
    result = srch.get_history(args.file)
    if not result:
        print(f"File number {args.file} not found.")
        return
    _print_matter(result, verbose=True)


def _print_matter(matter: dict, verbose: bool = False):
    print(f"\n{'='*60}")
    print(f"  File #:   {matter.get('file_number','')}")
    print(f"  Title:    {matter.get('short_title','')}")
    if matter.get('full_title'):
        print(f"  Full:     {matter['full_title'][:80]}")
    print(f"  Type:     {matter.get('file_type','')}   "
          f"Sponsor: {matter.get('sponsor','')}")
    print(f"  Status:   {matter.get('current_status','')}   "
          f"Stage: {matter.get('current_stage','')}")
    print(f"  First seen: {matter.get('first_seen_date','')}   "
          f"Last seen: {matter.get('last_seen_date','')}")

    apps = matter.get("appearances", [])
    if apps:
        print(f"\n  APPEARANCES ({len(apps)}):")
        print(f"  {'#':>4}  {'Date':12}  {'Body':35}  {'Stage':12}  {'Status':14}  {'CF?':4}")
        print(f"  {'-'*4}  {'-'*12}  {'-'*35}  {'-'*12}  {'-'*14}  {'-'*4}")
        for a in apps:
            cf = "Yes" if a.get("carried_forward_from_prior") else "No"
            print(f"  {a.get('id',0):>4}  "
                  f"{a.get('meeting_date',''):12}  "
                  f"{a.get('body_name',''):35}  "
                  f"{a.get('agenda_stage',''):12}  "
                  f"{a.get('workflow_status',''):14}  "
                  f"{cf:4}")
            if verbose and a.get("analyst_working_notes"):
                notes = a["analyst_working_notes"][:120].replace("\n", " ")
                print(f"        Notes: {notes}")
    print(f"{'='*60}\n")


def cmd_update(args):
    init_db()
    aid = args.appearance
    try:
        if args.status:
            wf.set_workflow_status(aid, args.status)
            print(f"Appearance {aid} status → {args.status}")
        if args.assigned_to:
            wf.assign_appearance(aid, args.assigned_to)
            print(f"Appearance {aid} assigned to → {args.assigned_to}")
        if args.reviewer:
            wf.set_reviewer(aid, args.reviewer)
            print(f"Appearance {aid} reviewer → {args.reviewer}")
        if args.due_date:
            wf.set_due_date(aid, args.due_date)
            print(f"Appearance {aid} due date → {args.due_date}")
    except ValueError as e:
        print(f"ERROR: {e}"); sys.exit(1)


def cmd_notes(args):
    init_db()
    aid = args.appearance
    try:
        if args.append_working:
            wf.append_working_notes(aid, args.append_working)
            print(f"Appearance {aid}: working notes appended.")
        if args.append_reviewer:
            wf.append_reviewer_notes(aid, args.append_reviewer)
            print(f"Appearance {aid}: reviewer notes appended.")
        if args.set_final_file:
            wf.set_finalized_brief(aid, args.set_final_file)
            print(f"Appearance {aid}: finalized brief set.")
    except ValueError as e:
        print(f"ERROR: {e}"); sys.exit(1)


def cmd_export(args):
    init_db()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    files = []

    if args.meeting_id:
        files = exporters.export_for_meeting(args.meeting_id, output_dir)
    elif args.date:
        try:
            dt = parse_date_arg(args.date)
        except ValueError as e:
            print(f"ERROR: {e}"); sys.exit(1)
        date_str = dt.strftime("%m/%d/%Y")
        files = exporters.export_for_date(date_str, output_dir)
    elif args.file:
        files = exporters.export_for_file_number(args.file, output_dir)
    else:
        print("Specify --meeting-id, --date, or --file")
        sys.exit(1)

    if files:
        print(f"\nExported {len(files)} file(s) to {output_dir}/:")
        for f in files:
            print(f"  {f.name}")
    else:
        print("No files generated — no matching data found.")


def cmd_transcript(args):
    init_db()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    import transcript as tx

    def _print_progress(msg, phase="transcript", pct=0):
        bar = f"[{'█' * (pct // 5)}{'░' * (20 - pct // 5)}] {pct}%"
        print(f"  {bar}  {msg}")

    print(f"\n🎙 Transcript Backfill — meeting ID {args.meeting_id}")
    print("=" * 50)

    result = tx.backfill_transcript(
        args.meeting_id,
        output_dir=output_dir,
        video_url=args.video_url,
        emit=_print_progress,
    )

    if result["status"] == "ok":
        print(f"\n✓ SUCCESS")
        print(f"  Video:        {result.get('video_title', 'N/A')}")
        print(f"  URL:          {result.get('video_url', 'N/A')}")
        print(f"  Transcript:   {result.get('transcript_length', 0):,} characters")
        print(f"  Segmented:    {result.get('items_segmented', 0)} items")
        print(f"  Updated:      {result.get('items_updated', 0)} appearances")
        if result.get("transcript_file"):
            print(f"  Raw saved to: {result['transcript_file']}")
    else:
        print(f"\n✗ FAILED: {result.get('message', 'Unknown error')}")
        if result.get("candidates"):
            print("\n  Possible matches (use --video-url to override):")
            for c in result["candidates"][:5]:
                score = c.get("match_score", 0)
                print(f"    [{score:.0%}] {c.get('title', 'N/A')}")
                print(f"          {c.get('url', '')}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="OCA Agenda Intelligence Agent v6",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python oca_agenda_agent_v6.py process --date 4/15/2026
  python oca_agenda_agent_v6.py process --from-date 4/1/2026
  python oca_agenda_agent_v6.py search --file 251862
  python oca_agenda_agent_v6.py search --keyword "Fisher Island" --limit 20
  python oca_agenda_agent_v6.py search --sponsor "Diaz"
  python oca_agenda_agent_v6.py history --file 251862
  python oca_agenda_agent_v6.py update --appearance 123 --status "In Progress"
  python oca_agenda_agent_v6.py update --appearance 123 --assigned-to "Alex"
  python oca_agenda_agent_v6.py notes  --appearance 123 --append-working "Check amendment"
  python oca_agenda_agent_v6.py export --date 4/15/2026
  python oca_agenda_agent_v6.py export --file 251862
  python oca_agenda_agent_v6.py transcript --meeting-id 42
  python oca_agenda_agent_v6.py transcript --meeting-id 42 --video-url "https://youtube.com/watch?v=XYZ"
        """
    )

    parser.add_argument("--db", default=None, help="Path to SQLite database (default: oca_agenda.db)")
    parser.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", required=True)

    # ── process ──
    p_proc = sub.add_parser("process", help="Scrape and analyze agendas")
    dg = p_proc.add_mutually_exclusive_group(required=True)
    dg.add_argument("--date",      help="Single date (M/D/YYYY)")
    dg.add_argument("--from-date", help="All agendas from this date onward")
    p_proc.add_argument("--committee",  default=None, help="Filter to one committee")
    p_proc.add_argument("--output-dir", default="output", help="Output directory")

    # ── search ──
    p_srch = sub.add_parser("search", help="Search matters")
    p_srch.add_argument("--file",    default=None, help="Exact file number")
    p_srch.add_argument("--keyword", default=None, help="Full-text keyword search")
    p_srch.add_argument("--sponsor", default=None, help="Search by sponsor name")
    p_srch.add_argument("--limit",   type=int, default=20)

    # ── history ──
    p_hist = sub.add_parser("history", help="Show full history for a file number")
    p_hist.add_argument("--file", required=True, help="File number")

    # ── update ──
    p_upd = sub.add_parser("update", help="Update workflow fields on an appearance")
    p_upd.add_argument("--appearance", type=int, required=True, help="Appearance ID")
    p_upd.add_argument("--status",      default=None, help="Workflow status")
    p_upd.add_argument("--assigned-to", default=None, dest="assigned_to")
    p_upd.add_argument("--reviewer",    default=None)
    p_upd.add_argument("--due-date",    default=None, dest="due_date")

    # ── notes ──
    p_notes = sub.add_parser("notes", help="Append or set notes on an appearance")
    p_notes.add_argument("--appearance",      type=int, required=True)
    p_notes.add_argument("--append-working",  default=None, dest="append_working")
    p_notes.add_argument("--append-reviewer", default=None, dest="append_reviewer")
    p_notes.add_argument("--set-final-file",  default=None, dest="set_final_file",
                          help="Path to .txt file or inline text for finalized brief")

    # ── export ──
    p_exp = sub.add_parser("export", help="Export Excel + Word from database")
    p_exp.add_argument("--meeting-id", type=int, default=None, dest="meeting_id")
    p_exp.add_argument("--date",   default=None, help="M/D/YYYY")
    p_exp.add_argument("--file",   default=None, help="File number")
    p_exp.add_argument("--output-dir", default="output", dest="output_dir")

    # ── transcript ──
    p_tx = sub.add_parser("transcript",
        help="Backfill meeting transcript from YouTube recording")
    p_tx.add_argument("--meeting-id", type=int, required=True, dest="meeting_id",
                       help="Database meeting ID")
    p_tx.add_argument("--video-url", default=None, dest="video_url",
                       help="YouTube video URL (skip auto-search)")
    p_tx.add_argument("--output-dir", default="output", dest="output_dir")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.db:
        database.set_db_path(args.db)

    dispatch = {
        "process":    cmd_process,
        "search":     cmd_search,
        "history":    cmd_history,
        "update":     cmd_update,
        "notes":      cmd_notes,
        "export":     cmd_export,
        "transcript": cmd_transcript,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
