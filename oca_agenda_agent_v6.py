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
from analyzer import AgendaAnalyzer
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

    for ci, (cname, ccode) in enumerate(committees.items()):
        if progress_callback:
            progress_callback(f"Processing {cname}... ({ci+1}/{len(committees)})")

        log.info(f"\n  {cname}")
        if mode == "single":
            matching_dates = scraper.get_matching_dates(cname, ccode, exact_date=parsed_date)
        else:
            matching_dates = scraper.get_matching_dates(cname, ccode, from_date=parsed_date)

        if not matching_dates:
            log.info(f"  No matching agendas")
            results_summary.append({
                "committee": cname, "status": "No agenda found", "items": 0, "new": 0
            })
            continue

        for adate in matching_dates:
            items = scraper.get_agenda_items(ccode, cname, adate)
            log.info(f"  {adate}: {len(items)} items on site")

            if not items:
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

            # Check which file numbers are already processed for this meeting
            processed_ids = repo.get_processed_file_numbers_for_meeting(meeting_id)

            new_items = [
                it for it in items
                if str(it.get("matter_id", "")) not in processed_ids
            ]

            if not new_items:
                log.info(f"  SKIPPING: All {len(items)} items already in DB")
                results_summary.append({
                    "committee": cname, "date": adate,
                    "status": f"Skipped (all {len(items)} items already processed)",
                    "items": len(items), "new": 0
                })
                continue

            log.info(f"  {len(new_items)} new items to process (out of {len(items)} total)")

            processed_appearances = []

            for i, item in enumerate(new_items, 1):
                file_num = str(item.get("matter_id", ""))
                if not file_num:
                    continue

                if progress_callback:
                    progress_callback(
                        f"{cname}: item {i}/{len(new_items)} (File# {file_num})"
                    )
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
                            # Skip if we already have an appearance at that meeting
                            try:
                                from db import get_db as _gdb
                                with _gdb() as _c:
                                    existing = _c.execute(
                                        """SELECT a.id FROM appearances a
                                           JOIN meetings m ON m.id=a.meeting_id
                                           WHERE a.matter_id=? AND m.meeting_date=?
                                             AND LOWER(m.body_name)=LOWER(?)""",
                                        (matter_id, ev_date, ev_body)
                                    ).fetchone()
                                if existing:
                                    continue
                            except Exception:
                                pass
                            try:
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
                    pdf_text = extract_pdf_text(Path(item["pdf_path"]))
                elif item.get("pdf_url"):
                    pp = scraper.download_pdf(item["pdf_url"], pdf_dir)
                    if pp:
                        item_pdf_local = str(pp)
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
                if is_new_appearance:
                    current_app = repo.get_appearance_by_id(appearance_id)
                    if current_app and current_app.get("prior_appearance_id"):
                        prior_app = repo.get_appearance_by_id(
                            current_app["prior_appearance_id"]
                        )
                        if prior_app:
                            prior_context = (
                                prior_app.get("ai_summary_for_appearance", "") or ""
                            )

                # ── AI Analysis ───────────────────────────────────
                cn = item.get("committee_item_number", "")
                display = f"{cn} (File# {file_num})" if cn else f"File# {file_num}"

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
                else:
                    part1, part2, full = analyzer.analyze_item(
                        display,
                        item.get("short_title", item.get("title", "")),
                        pdf_text,
                        cname,
                        item.get("page_text", ""),
                        prior_context=prior_context,
                    )
                    time.sleep(20)

                # ── Leg history summary ───────────────────────────
                leg_summary = ""
                raw_hist = item.get("legislative_history_raw", "")
                if raw_hist and len(raw_hist) > 50:
                    leg_summary = analyzer.summarize_leg_history(
                        raw_hist, item.get("short_title", "")
                    )
                    time.sleep(20)

                # ── Extract watch points ──────────────────────────
                part1_clean, watch = extract_watch_points(part1)

                # ── Persist AI results ────────────────────────────
                repo.update_appearance_ai(
                    appearance_id, part1_clean, part2, watch, leg_summary
                )
                repo.update_matter_ai_fields(matter_id, part1_clean, watch)

                processed_appearances.append(appearance_id)
                total_items += 1
                total_new   += 1 if is_new_appearance else 0

            # ── Generate exports ─────────────────────────────────
            if processed_appearances:
                gen_files = exporters.export_for_meeting(meeting_id, output_dir)
                total_files += len(gen_files)
                _log_updates(output_dir, cname, new_items, adate)

                results_summary.append({
                    "committee": cname,
                    "date":      adate,
                    "status":    f"Processed {len(processed_appearances)} item(s)",
                    "items":     len(items),
                    "new":       len(processed_appearances),
                    "meeting_id": meeting_id,
                })

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

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.db:
        database.set_db_path(args.db)

    dispatch = {
        "process": cmd_process,
        "search":  cmd_search,
        "history": cmd_history,
        "update":  cmd_update,
        "notes":   cmd_notes,
        "export":  cmd_export,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
