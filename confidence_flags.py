"""
confidence_flags.py — Compute analysis confidence/completeness flags for agenda items.

Each flag has: level ("red"|"yellow"), code (unique key), label, detail, color (hex for UI).
Used by meeting_service.py (Meeting Prep grid) and app_v6.py (Workflow/My Items API).
"""

from datetime import date, datetime


def compute_confidence_flags(a: dict) -> tuple:
    """Compute confidence flags for an appearance dict.

    Returns (confidence_level, flags_list) where confidence_level is
    'green', 'yellow', or 'red' and flags_list is a list of flag dicts.

    Flag colors:
        #dc2626 (red)    — No PDF
        #7c3aed (purple) — No AI Analysis
        #ea580c (orange) — AI Incomplete
        #d97706 (amber)  — No Transcript
        #0891b2 (cyan)   — Stale Analysis
    """
    flags = []
    has_pdf = bool(a.get("item_pdf_url") or a.get("item_pdf_local_path"))
    has_ai  = bool((a.get("ai_summary_for_appearance") or "").strip())
    has_transcript = bool(
        (a.get("transcript_analysis") or "").strip() or
        "[Meeting Discussion" in (a.get("analyst_working_notes") or "")
    )
    analysis_at = a.get("analysis_at") or ""
    meeting_date = a.get("meeting_date") or ""
    is_future = meeting_date > date.today().isoformat() if meeting_date else False

    # Red flags (critical — researcher MUST look deeper)
    if not has_pdf:
        flags.append({"level": "red", "code": "no_pdf", "color": "#dc2626",
            "label": "No PDF", "detail": "No item PDF from Legistar — AI analysis was based on title only"})
    if not has_ai:
        flags.append({"level": "red", "code": "no_ai", "color": "#7c3aed",
            "label": "No AI Analysis", "detail": "AI analysis has not been run or returned no results"})
    elif has_ai and len((a.get("ai_summary_for_appearance") or "").strip()) < 200:
        flags.append({"level": "red", "code": "ai_short", "color": "#ea580c",
            "label": "AI Incomplete", "detail": "AI analysis is unusually short — likely based on insufficient source material"})

    # Yellow flags (warnings — researcher should be aware)
    # Only flag missing transcript if the meeting date has already passed
    if not has_transcript and not is_future:
        flags.append({"level": "yellow", "code": "no_transcript", "color": "#d97706",
            "label": "No Transcript", "detail": "No meeting transcript — discussion summary not available"})
    if analysis_at:
        try:
            d1 = datetime.strptime(analysis_at[:10], "%Y-%m-%d").date()
            age_days = (date.today() - d1).days
            if age_days > 7:
                flags.append({"level": "yellow", "code": "stale", "color": "#0891b2",
                    "label": f"Stale ({age_days}d)", "detail": f"AI analysis is {age_days} days old — agenda may have changed"})
        except Exception:
            pass

    levels = [f["level"] for f in flags]
    confidence = "red" if "red" in levels else ("yellow" if "yellow" in levels else "green")
    return confidence, flags
