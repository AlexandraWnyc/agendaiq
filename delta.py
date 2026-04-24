"""
delta.py — Cross-appearance change detection for AgendaIQ

When an item (matter) appears on multiple meeting agendas over time,
conditions often change: covenant terms updated, density limits modified,
conditions added/removed. This module detects and surfaces those changes.

Strategy:
  1. For each matter with 2+ appearances, compare the AI summaries
     chronologically (oldest → newest)
  2. Use Claude to produce a structured diff: what specifically changed
  3. Store diffs on the appearance row (delta_from_prior column)
  4. Surface in the UI as a "Changes" card

The diff is AI-powered because the changes are semantic, not textual:
"covenant limiting development to 33 units" → "covenant updated to 45 units"
is a meaningful change that text-diff would miss entirely.
"""

import logging
import json
from db import get_db
from utils import now_iso

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


def get_appearance_history(matter_id: int, org_id=None) -> list[dict]:
    """Get all appearances for a matter, chronologically, with their
    AI summaries and meeting context."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        rows = conn.execute(
            """SELECT a.id, a.ai_summary_for_appearance, a.watch_points_for_appearance,
                      a.appearance_title, a.agenda_stage, a.file_number,
                      a.committee_item_number, a.bcc_item_number,
                      a.created_at, a.analysis_at,
                      m.meeting_date, m.body_name, m.meeting_type
               FROM appearances a
               JOIN meetings m ON m.id = a.meeting_id
               WHERE a.matter_id = ? AND a.org_id = ?
               ORDER BY m.meeting_date ASC, a.created_at ASC""",
            (matter_id, oid)
        ).fetchall()
    return [dict(r) for r in rows]


def compute_delta_prompt(prev_summary: str, curr_summary: str,
                          prev_context: str, curr_context: str) -> str:
    """Build the prompt for Claude to detect changes between two appearances."""
    return f"""Compare these two briefings of the SAME agenda item from different meeting dates.
Identify what specifically CHANGED between the earlier and later versions.

EARLIER BRIEFING ({prev_context}):
{prev_summary[:3000]}

LATER BRIEFING ({curr_context}):
{curr_summary[:3000]}

Respond with a JSON object:
{{
  "has_changes": true/false,
  "change_summary": "One sentence describing the overall change",
  "changes": [
    {{
      "category": "covenant|density|conditions|fiscal|status|sponsor|scope|timeline|other",
      "what_changed": "specific description of what changed",
      "previous": "what it was before",
      "current": "what it is now",
      "significance": "high|medium|low"
    }}
  ]
}}

If there are no substantive changes (just rewording or minor differences), set has_changes to false and changes to [].
Focus on MATERIAL changes: terms, numbers, conditions, status, scope. Ignore stylistic differences."""


def detect_deltas_for_matter(matter_id: int, org_id=None) -> list[dict]:
    """Compare consecutive appearances of a matter and detect changes.
    Returns a list of delta objects, one per appearance pair."""
    oid = _resolve_org_id(org_id)
    history = get_appearance_history(matter_id, org_id=oid)

    # Need at least 2 appearances with AI summaries to compare
    analyzed = [h for h in history if h.get("ai_summary_for_appearance")]
    if len(analyzed) < 2:
        return []

    deltas = []
    for i in range(1, len(analyzed)):
        prev = analyzed[i - 1]
        curr = analyzed[i]

        prev_ctx = f"{prev['body_name']} {prev['meeting_date']}"
        curr_ctx = f"{curr['body_name']} {curr['meeting_date']}"

        prompt = compute_delta_prompt(
            prev["ai_summary_for_appearance"],
            curr["ai_summary_for_appearance"],
            prev_ctx, curr_ctx
        )

        # Call Claude to detect changes
        try:
            import anthropic
            import os
            import usage

            # Rate limit check
            limit_check = usage.check_limits(oid)
            if not limit_check["allowed"]:
                log.warning(f"Rate limit reached: {limit_check['reason']}")
                deltas.append({
                    "has_changes": None,
                    "error": limit_check["reason"],
                    "from_appearance_id": prev["id"],
                    "to_appearance_id": curr["id"],
                    "from_date": prev["meeting_date"],
                    "to_date": curr["meeting_date"],
                })
                continue

            client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.content[0].text.strip()

            # Record usage
            usage.record_usage(org_id=oid, call_type='delta',
                tokens_in=getattr(response.usage, 'input_tokens', 0),
                tokens_out=getattr(response.usage, 'output_tokens', 0))

            # Parse JSON from response
            json_start = result_text.find("{")
            json_end = result_text.rfind("}") + 1
            if json_start >= 0 and json_end > json_start:
                delta = json.loads(result_text[json_start:json_end])
            else:
                delta = {"has_changes": False, "changes": [], "change_summary": ""}

            delta["from_appearance_id"] = prev["id"]
            delta["to_appearance_id"] = curr["id"]
            delta["from_date"] = prev["meeting_date"]
            delta["to_date"] = curr["meeting_date"]
            delta["from_body"] = prev["body_name"]
            delta["to_body"] = curr["body_name"]
            deltas.append(delta)

        except Exception as e:
            log.warning(f"Delta detection failed for matter {matter_id} "
                       f"({prev['meeting_date']} → {curr['meeting_date']}): {e}")
            deltas.append({
                "has_changes": None,
                "error": str(e),
                "from_appearance_id": prev["id"],
                "to_appearance_id": curr["id"],
                "from_date": prev["meeting_date"],
                "to_date": curr["meeting_date"],
            })

    return deltas


def get_cached_deltas(appearance_id: int, org_id=None) -> dict | None:
    """Check if we already computed deltas for this appearance."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        row = conn.execute(
            "SELECT delta_from_prior FROM appearances "
            "WHERE id = ? AND org_id = ? AND delta_from_prior IS NOT NULL",
            (appearance_id, oid)
        ).fetchone()
    if row and row["delta_from_prior"]:
        try:
            return json.loads(row["delta_from_prior"])
        except Exception:
            pass
    return None


def save_delta(appearance_id: int, delta: dict, org_id=None):
    """Cache a computed delta on the appearance row."""
    oid = _resolve_org_id(org_id)
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET delta_from_prior = ? "
            "WHERE id = ? AND org_id = ?",
            (json.dumps(delta, ensure_ascii=False), appearance_id, oid)
        )
