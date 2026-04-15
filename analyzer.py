"""
analyzer.py — Claude AI analysis for OCA Agenda Intelligence v6
Preserves all v5 analysis logic. Minor refactor: standalone module.
"""
import time, logging
from utils import clean_markdown

log = logging.getLogger("oca-agent")

MODEL = "claude-haiku-4-5-20251001"

SOP_PROMPT = """You are a senior research analyst for the Office of the Commission Auditor (OCA) at Miami-Dade County.

OUTPUT FORMAT RULES — MANDATORY:
- Do NOT use markdown. No **, no ##, no *, no _, no ``` anywhere.
- Do NOT add meta-commentary like "Based on the provided documents..." or "I'll analyze..."
- Write in clean professional prose. Use plain text only.
- For emphasis, just use CAPS for section headers.
- For bullet points, start lines with a dash and space: "- "
- Never start your response with a preamble. Go straight into the formatted output.

PART 1 - OCA AGENDA DEBRIEF (Standardized Summary)

ITEM [number] - [Short Title]
Sponsor: [Commissioner or Department name only]
Summary: [One sentence. What this item does.]
District(s): [Affected district(s), or "Countywide"]
Purpose and Background: [2-3 sentences of context.]
Fiscal Impact: [Funding source, dollar amount, countywide vs district. Verify numbers against the PDF.]
Additional Notes: [1-3 short bullets if applicable, each starting with "- "]

WATCH POINTS: [1-2 sentences: what should Commissioners pay attention to on this item?]

PART 2 - RESEARCH INTELLIGENCE

RESEARCH CONTEXT:
[Findings from web search organized by relevance. Plain text paragraphs. Cite sources inline with parenthetical references like (Source Name, Date). Cover: prior legislation, news, vendor history for procurement items, known controversy, peer jurisdiction context.]

WATCH POINTS:
[2-3 bullets on what Commissioners should watch for. Each starts with "- ".]

Be factual, concise, and non-interpretive. Work with available information and note gaps."""


class AgendaAnalyzer:
    def __init__(self, api_key: str):
        import httpx
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key, http_client=httpx.Client(verify=False))

    def _call_api(self, system: str, msg: str, max_tokens: int = 4096) -> str:
        try:
            r = self.client.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=[{"role": "user", "content": msg}],
            )
            return "\n".join(b.text for b in r.content if hasattr(b, "text")).strip()
        except Exception as e:
            if "429" in str(e) or "rate_limit" in str(e):
                log.info("    Rate limited, waiting 60s...")
                time.sleep(60)
                try:
                    r = self.client.messages.create(
                        model=MODEL, max_tokens=max_tokens, system=system,
                        tools=[{"type": "web_search_20250305", "name": "web_search"}],
                        messages=[{"role": "user", "content": msg}],
                    )
                    return "\n".join(b.text for b in r.content if hasattr(b, "text")).strip()
                except Exception as e2:
                    log.error(f"    Retry failed: {e2}")
                    return f"[ERROR: {e2}]"
            log.error(f"    API error: {e}")
            return f"[ERROR: {e}]"

    def analyze_item(self, item_number: str, title: str, pdf_text: str,
                     committee_name: str, page_text: str = "",
                     prior_context: str = "") -> tuple[str, str, str]:
        """
        Full analysis. Returns (part1_text, part2_text, full_text).
        Optionally includes prior context for carried-forward items.
        """
        ctx = f"COMMITTEE: {committee_name}\nITEM NUMBER: {item_number}\nTITLE: {title}\n"

        if prior_context:
            ctx += f"\n--- PRIOR ANALYSIS CONTEXT (for reference, do not simply repeat) ---\n{prior_context[:1500]}\n--- END PRIOR CONTEXT ---\n"

        if page_text:
            ctx += f"\n--- ITEM ROUTING & LEGISLATIVE INFO ---\n{page_text[:4000]}\n--- END ROUTING ---\n"
        if pdf_text:
            ctx += f"\n--- PDF CONTENT ---\n{pdf_text}\n--- END PDF ---\n"
        elif not page_text:
            ctx += "\n[No content available]\n"

        msg = (ctx + "\nProduce both PART 1 (OCA Agenda Debrief) and PART 2 (Research Intelligence) "
               "per your system prompt. Use web search for Part 2. Clearly separate Part 1 and Part 2 "
               "with headers. Remember: NO markdown formatting.")

        log.info(f"    Calling Claude API...")
        full = self._call_api(SOP_PROMPT, msg)
        full = clean_markdown(full)

        # Robust Part 1/Part 2 split (v5 logic preserved)
        part1, part2 = full, ""
        split_markers = [
            "PART 2 -", "PART 2:", "PART 2\n",
            "RESEARCH INTELLIGENCE", "RESEARCH CONTEXT:",
        ]
        for marker in split_markers:
            idx = full.upper().find(marker.upper())
            if idx > 50:
                part1 = full[:idx].strip()
                part2 = full[idx:].strip()
                break

        if len(part1) < 30:
            part1 = full
            part2 = ""

        return part1, part2, full

    def summarize_leg_history(self, leg_history_raw: str, item_title: str) -> str:
        if not leg_history_raw or len(leg_history_raw) < 50:
            return ""
        msg = (f'Summarize this legislative history into 2-3 concise bullet points. '
               f'Include key dates, actions taken, committee votes, deferrals, and forwarding decisions. '
               f'Be factual and brief. Do NOT use any markdown formatting. Start each bullet with "- ".\n\n'
               f'Item: {item_title}\n\n{leg_history_raw[:2000]}\n\nReply with ONLY the bullet points, nothing else.')
        result = self._call_api(
            "You are a concise legislative summarizer. Output only bullet points. No markdown.",
            msg, max_tokens=500
        )
        return clean_markdown(result)
