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

# Character budgets (~4 chars ≈ 1 token for English).
# April 2026 tuning: raised PDF_TEXT_BUDGET_CHARS from 15k to 35k and
# PAGE_TEXT_BUDGET_CHARS from 4k to 8k after accuracy audits showed Claude
# was reporting 'not in materials' for facts that lived in fiscal-impact
# attachments and legislative history sections we had clipped out.
# Net cost per item on Haiku stays inside the ~$0.05–$0.10 target band.
PDF_TEXT_BUDGET_CHARS   = 35000   # ~8,750 tokens
PAGE_TEXT_BUDGET_CHARS  = 8000
PRIOR_CTX_BUDGET_CHARS  = 6000
LEG_HIST_BUDGET_CHARS   = 3000

# Pages containing any of these phrases are ALWAYS kept during trimming,
# regardless of keyword score. These are the sections that most often hold
# the facts researchers get burned on when they're missing.
ALWAYS_KEEP_PATTERNS = [
    "fiscal impact",
    "funding source",
    "economic impact",
    "delegation of board authority",
    "recommendation",
    "background",
    "track record",
    "attachment",
    "exhibit",
    "resolution no.",
    "ordinance no.",
    "whereas,",
    "now, therefore",
    "be it resolved",
    "be it ordained",
    "legislative text",
    "legislative history",
    "sponsor:",
    "sponsored by",
]

SOP_PROMPT = """You are a senior research analyst for the Office of the Commission Auditor (OCA) at Miami-Dade County.

OUTPUT FORMAT RULES — MANDATORY:
- Plain text ONLY. No markdown of any kind. That means: no **, no __, no single or double asterisks for emphasis, no ## or # headers, no ``` code fences, no single backticks, no > blockquotes, no | table pipes, no --- horizontal rules.
- If you find yourself reaching for any of the above to emphasize or structure something, STOP and rewrite in plain prose.
- For emphasis, use CAPS for section headers only.
- For bullet points, start lines with "- " (hyphen and space).
- No preamble. No meta-commentary like "Based on the provided documents..." or "I'll analyze...". Go straight into the formatted output.

ACCURACY RULES — READ CAREFULLY:

This output is used in commissioner briefings. The most damaging failure mode is saying "not provided" or "not in the materials" when the fact IS in the materials — that undermines the Commission Auditor's credibility. The second most damaging failure is stating something the materials don't actually support. Balance both.

Two-tier rule for every fact:
1. STATE IT when the fact appears in the source PDF or routing info, even if the wording in your output paraphrases. Exact quotes are not required. If the PDF says "the total estimated cost is $211 million from General Fund," you can write "costs $211M from the General Fund" without hedging.
2. FLAG THE GAP when a fact is genuinely absent. Do NOT write "not provided" or "not addressed in materials" — write instead: "Not located in [the section you searched, e.g. fiscal impact memo / resolution text / legislative history] — recommend checking [where it might live, e.g. the full item PDF, Legistar file, or department memo]."

Never fabricate. Never infer dollar amounts, dates, vendors, or districts that are not in the source. But do NOT refuse to state facts that ARE in the source just because the wording isn't a perfect quote match.

SUPPLEMENT / SUB-ITEM RULE:
If the user message indicates this item is a Supplement, Substitute, or sub-item (e.g. "3C Supplement" relative to a base "3C", or "3C1" relative to "3C"), and prior-context for the base item is provided: focus your analysis on WHAT IS NEW, ADDED, CHANGED, or SUBSTITUTED. Do NOT re-summarize the base item. Structure Part 1 as: what the base item does (ONE sentence, referenced), then the specific changes introduced by this supplement/sub-item.

CROSS-REFERENCE RULE:
If prior-context contains a block labeled [SAME-CASE ITEM …] or [COMPANION CASE …], it means OTHER agenda items are part of the same Case or a related companion Case (e.g. a CDMP amendment and its concurrent zoning application for the same project). When these blocks are present:
- Open the Summary with ONE sentence that references the relationship, e.g. "This ordinance takes final action on the CDMP amendment whose transmittal resolution was Item 3(B)(1) on 03/04/2026" OR "This zoning application is the companion to the CDMP amendment in Item 3(B) on today's agenda."
- Do NOT re-summarize the referenced item's content. Keep each item's brief standalone-readable by focusing on what THIS item does.
- In Watch Points, include a dependency statement when items are legally linked, e.g. "If Item 3(B) is denied, this zoning application becomes moot — staff recommendation is contingent on CDMP approval." Only add this if the dependency is in the source materials; do not speculate.
- Use the exact item numbers (e.g. "Item 3(B)(1)", "Item 8C4") from the cross-reference blocks when available. If only file numbers or application numbers are available, use those.
- Cross-references to COMPANION cases use the word "companion" or "concurrent" (matches county terminology). Cross-references to SAME-CASE items use "this amendment" / "the underlying ordinance" / similar connecting language.

PART 1 - OCA AGENDA DEBRIEF (Standardized Summary)

ITEM [number] - [Short Title]
Sponsor: [Commissioner or Department name only]
Summary: [One sentence. What this item does. If supplement/sub-item: one sentence on what IT changes, not what the base does.]
District(s): [Affected district(s), or "Countywide"]
Purpose and Background: [2-3 sentences of context.]
Fiscal Impact: [Funding source, dollar amount, countywide vs district. If the PDF gives a number, state it. If it genuinely doesn't, use the gap-flag format above.]
Additional Notes: [1-3 short bullets if applicable, each starting with "- "]

WATCH POINTS: [ONLY for HIGH or MEDIUM risk items. 1-2 sentences on what Commissioners should pay attention to. For LOW or CEREMONIAL items, write "None — routine item." Do NOT manufacture watch points for standard contract renewals, routine reports, ceremonial items, consent agenda items with no policy change, or other low-risk routine business.]

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
[ONLY for HIGH or MEDIUM risk items. 2-3 bullets on genuine concerns Commissioners should watch for. Each starts with "- ". For LOW or CEREMONIAL items, write "None — routine item." Do NOT invent watch points for items that are straightforward. Never write "no anticipated controversy" or similar editorializing — if there's nothing to watch, say "None."]

EDITORIAL RULES:
- NEVER write "no anticipated controversy" — that is an editorial judgment the OCA does not make.
- NEVER characterize consent agenda placement as evidence that an item is uncontroversial.
- If an item is on the consent agenda, simply note it factually. The consent agenda is a procedural tool, not a risk indicator.

Be factual, concise, and non-interpretive. When something is genuinely missing, flag the gap per the rule above. Never fabricate, never over-hedge."""


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
    """Clip pdf_text to budget_chars using section-aware keep rules.

    Strategy (revised April 2026):
      0. If already under budget, return as-is.
      1. Split into pages. Classify each:
         - ALWAYS-KEEP: page matches any phrase in ALWAYS_KEEP_PATTERNS
           (fiscal impact, funding source, resolution body, attachments,
           legislative history, etc). These pages contain the facts
           researchers get burned on when missing — we never drop them
           even if the title-keyword scorer gives them a 0.
         - SCORED: every other page gets scored by title-keyword matches,
           with small bonuses for page 1 and money-ish phrases.
      2. Always-keep pages are reserved first, up to 80% of the budget.
         (Cap ensures a 50-page attachment-heavy PDF can't starve out the
         cover memo.)
      3. Remaining budget fills with highest-scored remaining pages.
      4. Emit kept pages in their original order so context flows.
      5. Prefix with a note listing what was dropped so the model knows
         what's absent vs what it should have found.
    Returns (trimmed_text, strategy_used).
    """
    if not pdf_text or len(pdf_text) <= budget_chars:
        return pdf_text, "none"

    pages = _split_pages(pdf_text)
    if len(pages) <= 1:
        return pdf_text[:budget_chars] + "\n[...truncated for cost...]", "fallback-head"

    keywords = _keywords_from_title(title)

    # Classify every page
    entries = []  # list of dicts: idx, text, score, always_keep
    for idx, page in enumerate(pages):
        low = page.lower()
        always = any(pat in low for pat in ALWAYS_KEEP_PATTERNS)
        score = sum(1 for kw in keywords if kw in low)
        if idx == 0:
            score += 2
        if any(tag in low for tag in ("fiscal impact", "dollar", "$",
                                       "funding source", "summary")):
            score += 1
        entries.append({
            "idx": idx, "text": page, "score": score, "always": always,
        })

    kept = set()
    used = 0
    reserve_cap = int(budget_chars * 0.8)

    # Phase 1: reserve always-keep pages in original order up to 80% cap
    for e in entries:
        if not e["always"]:
            continue
        size = len(e["text"]) + 2
        if used + size > reserve_cap:
            # If even one always-keep page alone would overflow, truncate it
            if not kept:
                truncated = e["text"][:reserve_cap]
                e["text"] = truncated + "\n[...page truncated...]"
                size = len(e["text"]) + 2
            else:
                continue
        kept.add(e["idx"])
        used += size

    # Phase 2: fill remaining budget with highest-scored non-always pages
    remaining = [e for e in entries if e["idx"] not in kept]
    remaining.sort(key=lambda e: (-e["score"], e["idx"]))
    for e in remaining:
        size = len(e["text"]) + 2
        if used + size > budget_chars:
            continue
        kept.add(e["idx"])
        used += size

    if not kept:
        return pdf_text[:budget_chars] + "\n[...truncated for cost...]", "fallback-head"

    # Emit in original order
    kept_sorted = sorted(kept)
    kept_pages = [next(e["text"] for e in entries if e["idx"] == i)
                  for i in kept_sorted]
    dropped_idx = [e["idx"] + 1 for e in entries if e["idx"] not in kept]

    note_parts = [
        f"[Note: full PDF was {len(pdf_text):,} chars ({len(pages)} pages). "
        f"Kept {len(kept_pages)} pages ({used:,} chars) by prioritizing fiscal "
        f"impact, funding, resolution/ordinance text, and attachment pages."
    ]
    if dropped_idx:
        preview = dropped_idx[:10]
        more = "" if len(dropped_idx) <= 10 else f" and {len(dropped_idx)-10} more"
        note_parts.append(
            f" Dropped pages: {preview}{more}. If a specific detail isn't "
            "visible here, flag it as 'not located in provided excerpt — "
            "recommend checking the full PDF' rather than claiming it's absent."
        )
    note_parts.append("]\n\n")
    note = "".join(note_parts)
    return note + "\n\n".join(kept_pages), "section-aware"


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

        # Robust Part 1/Part 2 split (v5 logic preserved, v6 hardened).
        # Previous version required idx>50, which silently failed when
        # Claude opened with a short or empty preamble and placed the
        # PART 2 marker near position 0. Now we accept any marker position
        # but validate that the pre-split text actually looks like Part 1
        # (has an ITEM/Sponsor/Summary marker).
        part1, part2 = full, ""
        split_markers = [
            "PART 2 -", "PART 2:", "PART 2 —", "PART 2 –", "PART 2\n",
            "RESEARCH INTELLIGENCE", "RESEARCH CONTEXT:",
        ]
        full_upper = full.upper()
        best_split = -1
        for marker in split_markers:
            idx = full_upper.find(marker.upper())
            if idx >= 0 and (best_split == -1 or idx < best_split):
                best_split = idx
        if best_split >= 0:
            candidate_p1 = full[:best_split].strip()
            candidate_p2 = full[best_split:].strip()
            # Sanity check: Part 1 should have at least one Part-1 marker.
            # If it doesn't, the whole output is probably Part 1 and Claude
            # skipped Part 2 entirely, OR Claude labelled the whole thing
            # Part 2 — in either case, keep everything as part1.
            p1_markers = ("ITEM ", "SPONSOR:", "SUMMARY:", "FISCAL IMPACT",
                          "PURPOSE AND BACKGROUND", "RISK LEVEL:")
            if any(m in candidate_p1.upper() for m in p1_markers) or len(candidate_p1) > 200:
                part1 = candidate_p1
                part2 = candidate_p2
            # else leave part1=full, part2=""

        # Final guard: part1 should never be empty if full is non-empty
        if not part1.strip() and full.strip():
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

• WATCH POINTS: [ONLY if HIGH or MEDIUM risk. 2-3 specific questions or areas to monitor for the Commission Auditor. Frame gaps as questions to ask, not accusations. Neutral tone. For LOW or CEREMONIAL items, write "None — routine item." NEVER write "no anticipated controversy" or editorialize about consent agenda placement.]

---WATCH_POINTS---
• [ONLY for HIGH/MEDIUM risk items. Max 3 bullets. Key things to monitor or ask about. Neutral, specific. For LOW/CEREMONIAL: "None."]

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
