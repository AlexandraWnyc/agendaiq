"""
analyzer.py — Claude AI analysis for OCA Agenda Intelligence v6

Cost-reduction changes (April 2026):
  1. PDF text is clipped to a token-aware budget BEFORE being sent to Claude.
     If the PDF exceeds the budget, we pick the pages whose text best matches
     the item's title keywords instead of blindly taking the first N chars.
  2. Prompt caching (`cache_control: ephemeral`) is applied to the system
     prompt. On cache hits, Anthropic bills cached input at 10% of normal
     rate. If the system prompt is below the model's cache minimum, the API
     silently ignores the directive — still free to try.
  3. `analyze_item` now returns a 4th element: a usage dict with input /
     output / cached token counts so the caller can log real $ per item.
  4. New helper `compute_input_hash()` lets the caller hash the full prompt
     deterministically and skip the API call entirely when an identical
     analysis already exists in the DB.

Preserves all v5 split logic (PART 1 / PART 2).
"""
import time, logging, hashlib, re
from utils import clean_markdown

log = logging.getLogger("oca-agent")

MODEL = "claude-haiku-4-5-20251001"

# Character budgets (~4 chars ≈ 1 token for English). Lower these to cut cost.
PDF_TEXT_BUDGET_CHARS   = 15000   # ~3,750 tokens. Was 30,000 in scraper cap.
PAGE_TEXT_BUDGET_CHARS  = 4000
PRIOR_CTX_BUDGET_CHARS  = 4000
LEG_HIST_BUDGET_CHARS   = 2000

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

RISK LEVEL: [Classify as exactly one of: HIGH / MEDIUM / LOW / CEREMONIAL]
Apply these criteria:
- HIGH: Sole source or no-bid procurement. Rejected bidders with sole-source fallback. Contracts extended 3+ times without re-bid. Non-compete clauses in public contracts. Eminent domain. Tax or rate increases. Veto overrides. Policy changes affecting countywide services. Complex technology implementations with countywide impact. Items with prior audit findings or IG investigations. Controversial vendor history. Items deferred multiple times.
- MEDIUM: New ordinances or ordinance amendments with policy substance. Interlocal agreements. Change orders on existing contracts. New procurements following standard process. Zoning changes. Items with fiscal impact over $5M that followed proper process. Service changes affecting specific districts.
- LOW: Contract renewals following standard terms. Standard resolutions. Consent agenda items with no policy change. Routine reports. Budget allocations within existing authority. Street namings. Appointment confirmations.
- CEREMONIAL: Proclamations, recognitions, honorary resolutions, commendations, certificates, poster contests.

RISK REASON: [One sentence explaining WHY this risk level. Example: "Sole-source contract extended for the 4th time without competitive rebid since 2019."]

PART 2 - RESEARCH INTELLIGENCE

RESEARCH CONTEXT:
[Findings from web search organized by relevance. Plain text paragraphs. Cite sources inline with parenthetical references like (Source Name, Date). Cover: prior legislation, news, vendor history for procurement items, known controversy, peer jurisdiction context.]

WATCH POINTS:
[2-3 bullets on what Commissioners should watch for. Each starts with "- ".]

Be factual, concise, and non-interpretive. Work with available information and note gaps."""


# ─────────────────────────────────────────────────────────────
# PDF trimming
# ─────────────────────────────────────────────────────────────

_PAGE_RE = re.compile(r"^--- Page \d+.*?---\s*$", re.MULTILINE)


def _split_pages(pdf_text: str) -> list[str]:
    """Split text produced by scraper.extract_pdf_text() back into pages.
    Each returned element still has its '--- Page N ---' header."""
    if not pdf_text:
        return []
    # Find header positions, then slice between them
    positions = [m.start() for m in _PAGE_RE.finditer(pdf_text)]
    if not positions:
        return [pdf_text]
    positions.append(len(pdf_text))
    pages = []
    for i in range(len(positions) - 1):
        chunk = pdf_text[positions[i]:positions[i + 1]].strip()
        if chunk:
            pages.append(chunk)
    return pages


def _keywords_from_title(title: str) -> list[str]:
    """Pull meaningful keywords from an agenda item title for page scoring.
    Drops short/stop words so scoring is actually signal-bearing."""
    stop = {
        "a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "by",
        "with", "at", "as", "is", "be", "per", "into", "from", "that", "this",
        "resolution", "ordinance", "miami", "dade", "county", "regarding",
        "certain", "other", "all", "any", "authorizing", "approving",
    }
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-]{3,}", (title or "").lower())
    return [w for w in words if w not in stop]


def _trim_pdf_to_budget(pdf_text: str, title: str,
                        budget_chars: int = PDF_TEXT_BUDGET_CHARS) -> tuple[str, str]:
    """Clip pdf_text to budget_chars.
    Strategy:
      1. If already under budget, return as-is.
      2. Otherwise, split into pages. Score each page by how many title
         keywords it contains. Keep highest-scoring pages in original order
         until the budget is exhausted. Always include page 1.
      3. Prefix with a note so the model knows trimming happened.
    Returns (trimmed_text, strategy_used) where strategy_used is one of
      'none', 'smart-section', 'fallback-head'.
    """
    if not pdf_text or len(pdf_text) <= budget_chars:
        return pdf_text, "none"

    pages = _split_pages(pdf_text)
    if len(pages) <= 1:
        # One big blob, just truncate head — better than nothing
        return pdf_text[:budget_chars] + "\n[...truncated for cost...]", "fallback-head"

    keywords = _keywords_from_title(title)
    scored = []
    for idx, page in enumerate(pages):
        low = page.lower()
        score = sum(1 for kw in keywords if kw in low)
        # Cover pages, summaries, and fiscal impact sections tend to cluster
        # near the top. Bias page 1 and any page mentioning money/impact.
        if idx == 0:
            score += 2
        if any(tag in low for tag in ("fiscal impact", "dollar", "$", "funding source", "summary")):
            score += 1
        scored.append((idx, score, page))

    # Sort by score desc, stable on original order
    scored.sort(key=lambda t: (-t[1], t[0]))

    kept_indices = set()
    used = 0
    for idx, _score, page in scored:
        if used + len(page) + 2 > budget_chars:
            continue
        kept_indices.add(idx)
        used += len(page) + 2
        if used >= budget_chars:
            break

    # If nothing got in somehow, fall back to head
    if not kept_indices:
        return pdf_text[:budget_chars] + "\n[...truncated for cost...]", "fallback-head"

    # Emit in original page order
    kept = [pages[i] for i in sorted(kept_indices)]
    trimmed = "\n\n".join(kept)
    note = (
        f"[Note: PDF was {len(pdf_text):,} chars; trimmed to {len(trimmed):,} chars "
        f"by keeping {len(kept)} of {len(pages)} pages most relevant to the item title.]\n\n"
    )
    return note + trimmed, "smart-section"


# ─────────────────────────────────────────────────────────────
# Hashing
# ─────────────────────────────────────────────────────────────

def compute_input_hash(model: str, system_prompt: str, user_message: str) -> str:
    """Deterministic SHA-256 of everything that affects the API output.
    Used by the pipeline to skip the API call when we already analyzed
    these exact inputs before."""
    h = hashlib.sha256()
    h.update(model.encode("utf-8"))
    h.update(b"\x1f")
    h.update(system_prompt.encode("utf-8"))
    h.update(b"\x1f")
    h.update(user_message.encode("utf-8"))
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────────────────

def _zero_usage() -> dict:
    return {
        "input_tokens": 0, "output_tokens": 0,
        "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
    }


class AgendaAnalyzer:
    def __init__(self, api_key: str):
        import httpx
        from anthropic import Anthropic
        self.client = Anthropic(api_key=api_key, http_client=httpx.Client(verify=False))

    # ── Low-level API call ────────────────────────────────────
    def _call_api(self, system: str, msg: str, max_tokens: int = 4096,
                  use_web_search: bool = True, cache_system: bool = True
                  ) -> tuple[str, dict]:
        """Returns (text, usage_dict)."""
        # Build kwargs so we can flip cache/tools cleanly
        def _build_kwargs():
            kw = {"model": MODEL, "max_tokens": max_tokens,
                  "messages": [{"role": "user", "content": msg}]}
            if cache_system:
                kw["system"] = [{
                    "type": "text", "text": system,
                    "cache_control": {"type": "ephemeral"},
                }]
            else:
                kw["system"] = system
            if use_web_search:
                kw["tools"] = [{"type": "web_search_20250305", "name": "web_search"}]
            return kw

        def _extract(r):
            text = "\n".join(b.text for b in r.content if hasattr(b, "text")).strip()
            u = getattr(r, "usage", None)
            usage = {
                "input_tokens":               getattr(u, "input_tokens", 0) or 0,
                "output_tokens":              getattr(u, "output_tokens", 0) or 0,
                "cache_read_input_tokens":    getattr(u, "cache_read_input_tokens", 0) or 0,
                "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
            }
            return text, usage

        try:
            r = self.client.messages.create(**_build_kwargs())
            return _extract(r)
        except Exception as e:
            emsg = str(e)
            # If the model rejects prompt caching (too-short minimum), retry
            # without cache_control.
            if cache_system and ("cache_control" in emsg or "cache" in emsg.lower()):
                log.info("    Prompt-cache rejected; retrying without cache_control")
                try:
                    cache_system = False
                    r = self.client.messages.create(**_build_kwargs())
                    return _extract(r)
                except Exception as e2:
                    log.error(f"    API error (no-cache retry): {e2}")
                    return f"[ERROR: {e2}]", _zero_usage()
            if "429" in emsg or "rate_limit" in emsg:
                log.info("    Rate limited, waiting 60s...")
                time.sleep(60)
                try:
                    r = self.client.messages.create(**_build_kwargs())
                    return _extract(r)
                except Exception as e2:
                    log.error(f"    Retry failed: {e2}")
                    return f"[ERROR: {e2}]", _zero_usage()
            log.error(f"    API error: {e}")
            return f"[ERROR: {e}]", _zero_usage()

    # ── Public: build the user message (also used for hashing) ──
    def build_user_message(self, item_number: str, title: str, pdf_text: str,
                           committee_name: str, page_text: str = "",
                           prior_context: str = "",
                           pdf_budget_chars: int = PDF_TEXT_BUDGET_CHARS
                           ) -> tuple[str, dict]:
        """Compose the exact user message that will be sent to Claude.
        Returned alongside a meta dict describing trimming for logging."""
        trimmed_pdf, strategy = _trim_pdf_to_budget(pdf_text or "", title,
                                                     pdf_budget_chars)
        ctx  = f"COMMITTEE: {committee_name}\nITEM NUMBER: {item_number}\nTITLE: {title}\n"
        if prior_context:
            ctx += (f"\n--- PRIOR STAGE CONTEXT (AI analysis + researcher notes from committee) ---\n"
                    f"IMPORTANT: Review ALL prior notes below. Your analysis should build on this "
                    f"prior work — do not simply repeat it. If the researcher flagged specific "
                    f"concerns, address them. If anything has changed from the committee version, "
                    f"call it out explicitly.\n"
                    f"{prior_context[:PRIOR_CTX_BUDGET_CHARS]}\n"
                    f"--- END PRIOR CONTEXT ---\n")
        if page_text:
            ctx += (f"\n--- ITEM ROUTING & LEGISLATIVE INFO ---\n"
                    f"{page_text[:PAGE_TEXT_BUDGET_CHARS]}\n"
                    f"--- END ROUTING ---\n")
        if trimmed_pdf:
            ctx += f"\n--- PDF CONTENT ---\n{trimmed_pdf}\n--- END PDF ---\n"
        elif not page_text:
            ctx += "\n[No content available]\n"

        msg = (ctx + "\nProduce both PART 1 (OCA Agenda Debrief) and PART 2 (Research Intelligence) "
               "per your system prompt. Use web search for Part 2. Clearly separate Part 1 and Part 2 "
               "with headers. Remember: NO markdown formatting.")

        meta = {
            "pdf_trim_strategy": strategy,
            "pdf_chars_in":      len(pdf_text or ""),
            "pdf_chars_sent":    len(trimmed_pdf or ""),
            "prompt_chars":      len(msg),
        }
        return msg, meta

    # ── Public: full analysis ────────────────────────────────
    def analyze_item(self, item_number: str, title: str, pdf_text: str,
                     committee_name: str, page_text: str = "",
                     prior_context: str = "",
                     pdf_budget_chars: int = PDF_TEXT_BUDGET_CHARS,
                     use_web_search: bool = True
                     ) -> tuple[str, str, str, dict]:
        """
        Full analysis. Returns (part1_text, part2_text, full_text, meta).
        meta contains: input_hash, usage{in,out,cached,created}, pdf_trim_strategy,
        pdf_chars_in, pdf_chars_sent, prompt_chars, model.
        """
        msg, trim_meta = self.build_user_message(
            item_number, title, pdf_text, committee_name,
            page_text, prior_context, pdf_budget_chars,
        )
        input_hash = compute_input_hash(MODEL, SOP_PROMPT, msg)

        log.info(f"    Calling Claude API... (pdf {trim_meta['pdf_chars_in']:,}→"
                 f"{trim_meta['pdf_chars_sent']:,} chars, {trim_meta['pdf_trim_strategy']})")
        full, usage = self._call_api(SOP_PROMPT, msg, use_web_search=use_web_search)
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

        # Extract AI risk classification from output and strip from display text
        ai_risk_level = ""
        ai_risk_reason = ""
        for line in full.split("\n"):
            line_upper = line.strip().upper()
            if line_upper.startswith("RISK LEVEL:"):
                val = line.split(":", 1)[1].strip().upper()
                for lvl in ["HIGH", "MEDIUM", "LOW", "CEREMONIAL"]:
                    if lvl in val:
                        ai_risk_level = lvl
                        break
            elif line_upper.startswith("RISK REASON:"):
                ai_risk_reason = line.split(":", 1)[1].strip()

        # Remove RISK LEVEL and RISK REASON lines from part1 (metadata, not display)
        part1_lines = part1.split("\n")
        part1 = "\n".join(
            l for l in part1_lines
            if not l.strip().upper().startswith("RISK LEVEL:")
            and not l.strip().upper().startswith("RISK REASON:")
        ).strip()

        meta = {
            "input_hash":         input_hash,
            "model":              MODEL,
            "usage":              usage,
            "ai_risk_level":      ai_risk_level,
            "ai_risk_reason":     ai_risk_reason,
            **trim_meta,
        }
        return part1, part2, full, meta

    # ── Public: short leg-history summary ────────────────────
    def summarize_leg_history(self, leg_history_raw: str, item_title: str
                              ) -> tuple[str, dict]:
        """Returns (summary, usage). Empty string + zero usage if no history."""
        if not leg_history_raw or len(leg_history_raw) < 50:
            return "", _zero_usage()
        msg = (f'Summarize this legislative history into 2-3 concise bullet points. '
               f'Include key dates, actions taken, committee votes, deferrals, and forwarding decisions. '
               f'Be factual and brief. Do NOT use any markdown formatting. Start each bullet with "- ".\n\n'
               f'Item: {item_title}\n\n{leg_history_raw[:LEG_HIST_BUDGET_CHARS]}\n\nReply with ONLY the bullet points, nothing else.')
        text, usage = self._call_api(
            "You are a concise legislative summarizer. Output only bullet points. No markdown.",
            msg, max_tokens=500, use_web_search=False, cache_system=False,
        )
        return clean_markdown(text), usage

    # ── Public: committee→BCC change detection ──────────────
    def detect_changes(self, item_title: str,
                       committee_summary: str, committee_pdf_text: str,
                       bcc_pdf_text: str) -> tuple[str, dict]:
        """Compare a committee version of an item to its BCC version and
        describe what changed. Returns (change_notes, usage).
        Empty string + zero usage if inputs are insufficient."""
        if not bcc_pdf_text or (not committee_summary and not committee_pdf_text):
            return "", _zero_usage()
        prior = committee_summary or ""
        if committee_pdf_text:
            prior += f"\n\n--- Committee PDF excerpt ---\n{committee_pdf_text[:5000]}"
        msg = (
            f"Compare the committee version and BCC version of this legislative item "
            f"and identify what has changed. Focus on substantive changes: amended language, "
            f"new conditions, dollar amounts, dates, sponsors, scope changes, added/removed "
            f"sections. Ignore formatting or cosmetic differences.\n\n"
            f"Item: {item_title}\n\n"
            f"--- COMMITTEE VERSION ---\n{prior[:8000]}\n--- END ---\n\n"
            f"--- BCC VERSION ---\n{bcc_pdf_text[:8000]}\n--- END ---\n\n"
            f"If there are meaningful changes, list each one starting with '- CHANGED: '. "
            f"If the item appears identical or is a supplement/new item with no prior, "
            f"reply with 'No substantive changes detected.' "
            f"Do NOT use any markdown formatting."
        )
        text, usage = self._call_api(
            "You are a legislative analyst comparing two versions of the same item. "
            "Be precise and factual. No markdown.",
            msg, max_tokens=600, use_web_search=False, cache_system=False,
        )
        return clean_markdown(text), usage

    # ─────────────────────────────────────────────────────────
    # Synthesized Debrief
    # ─────────────────────────────────────────────────────────

    SYNTHESIS_PROMPT = """You are preparing a debrief for the Commission Auditor at Miami-Dade County. He will use this to brief Commissioners in under 30 seconds per item. Every fact you write MUST come directly from the source materials provided. If you cannot find it in the sources, do not state it.

YOUR JOB:
1. ACCURACY FIRST. Only state facts that are explicitly in the source documents. If a number, name, date, or claim appears in your output, it must be traceable to the sources. Do not infer, assume, or extrapolate.
2. COMPLETENESS. Include every important detail from the sources — dollar amounts, where the money comes from, who pays, what the item changes, what happened before, what is projected. The Commission Auditor cannot be caught off guard.
3. PLAIN LANGUAGE. If the source uses a technical term (e.g., "lineup," "BRT," "RFP"), explain what it means in parentheses on first use. The Commissioners are not transit experts or procurement specialists.
4. NEUTRAL TONE. You are a briefer, not a critic. State what the materials say. If information is missing from the materials, say "not addressed in materials" — never "undocumented," "unsupported," "failed to provide," or any accusatory language.
5. NO REPETITION. Each fact appears exactly ONCE in the entire output. Each section adds only NEW information.

OUTPUT FORMAT — MANDATORY:
- Plain text only. No markdown (no **, ##, *, _). Use "•" for bullets. CAPS for headers.

FORMAT:

• ITEM [number] – [Short Title]

• Sponsor: [Commissioner or Department name]

• 1-sentence summary: [1-2 sentences in plain English. What changes if this passes? Be specific — routes, amounts, dates.]

• District(s): [Affected district(s), or "Countywide"]

• Purpose and Background:
  • [What triggered this — mandate, contract requirement, prior action. Explain technical terms in parentheses on first use.]
  • [What specifically changes — services added, removed, modified. Dates.]
  • [History — what happened before, prior cycles, past savings or costs from similar actions]
  • [Committee review, public input, or legislative history if any]
  • [3-5 bullets. Each bullet 1-2 sentences with specific detail from the source documents.]

• Fiscal Impact:
  • [How much does this cost and what is the money for? Be specific.]
  • [Where does the money come from? What fund, budget line, or offset?]
  • [Any projected savings, past savings, or future cost implications mentioned in the materials]
  • [2-4 bullets. Every dollar amount must come from the source documents.]

• Additional Notes:
  • [ONLY info not already stated above. Implementation details, equity concerns, committee discussion.]
  • [1-2 bullets MAX. If nothing new: "None beyond above."]

• WATCH POINTS: [2-3 specific questions or areas to monitor for the Commission Auditor. Frame gaps as questions to ask, not accusations. Neutral tone.]

---WATCH_POINTS---
• [Max 3 bullets. Key things to monitor or ask about. Neutral, specific.]

RULES:
- If you are not 100% certain a fact is in the source materials, DO NOT include it.
- Explain jargon. "Lineup" = scheduled service adjustment cycle. "BRT" = Bus Rapid Transit. Etc.
- Target 300-400 words. Must fit on one page.
- No filler sentences. Every sentence carries a fact from the sources."""

    def synthesize_debrief(self, sources: dict) -> tuple:
        """Synthesize all research into a single comprehensive debrief.

        Args:
            sources: dict with keys like 'ai_summary', 'watch_points',
                     'analyst_notes', 'reviewer_notes', 'transcript_analysis',
                     'chat_insights', 'legislative_history', 'pdf_text',
                     'item_title', 'file_number', 'body_name', 'meeting_date'

        Returns:
            (debrief_text, watch_points_text, usage_dict)
        """
        # Build context message from all available sources
        parts = []
        parts.append(f"ITEM: {sources.get('item_title', 'Unknown')}")
        parts.append(f"FILE: {sources.get('file_number', 'N/A')}")
        parts.append(f"BODY: {sources.get('body_name', 'N/A')}")
        parts.append(f"DATE: {sources.get('meeting_date', 'N/A')}")
        parts.append("")

        budgets = {
            'ai_summary': 6000,
            'analyst_notes': 4000,
            'reviewer_notes': 4000,
            'watch_points': 2000,
            'transcript_analysis': 5000,
            'chat_insights': 4000,
            'legislative_history': 3000,
            'pdf_text': 6000,
        }

        labels = {
            'ai_summary': 'AI ANALYSIS (from initial agenda scan)',
            'analyst_notes': 'ANALYST WORKING NOTES',
            'reviewer_notes': 'REVIEWER NOTES',
            'watch_points': 'CURRENT WATCH POINTS',
            'transcript_analysis': 'COMMITTEE TRANSCRIPT ANALYSIS',
            'chat_insights': 'CHAT-BASED RESEARCH INSIGHTS',
            'legislative_history': 'LEGISLATIVE HISTORY',
            'pdf_text': 'SOURCE PDF TEXT',
        }

        for key, label in labels.items():
            val = sources.get(key, '') or ''
            val = val.strip()
            if val:
                budget = budgets.get(key, 4000)
                if len(val) > budget:
                    val = val[:budget] + "\n... [truncated]"
                parts.append(f"--- {label} ---")
                parts.append(val)
                parts.append(f"--- END {label} ---")
                parts.append("")

        msg = "\n".join(parts)

        text, usage = self._call_api(
            self.SYNTHESIS_PROMPT,
            msg, max_tokens=1200, use_web_search=False, cache_system=True,
        )

        text = clean_markdown(text)

        # Extract watch points from separator
        watch_points = ""
        if "---WATCH_POINTS---" in text:
            idx = text.index("---WATCH_POINTS---")
            watch_points = text[idx + len("---WATCH_POINTS---"):].strip()
            text = text[:idx].strip()

        return text, watch_points, usage
