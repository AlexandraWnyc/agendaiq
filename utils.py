"""
utils.py — Shared utilities for OCA Agenda Intelligence v6
"""
import os, re, logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("oca-agent")


# ── Text cleaning ─────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    """Strip markdown formatting that Claude may insert despite instructions.

    Covers: bold/italic (incl. nested, incl. no-space-after-asterisk),
    ATX headers (with or without trailing space — '##Summary' and '## Summary'),
    code fences and inline code, horizontal rules ('---', '***', '___'),
    markdown tables (pipe rows), blockquote markers ('> '), unicode bullets
    normalized to '- ', and common preamble phrases. Idempotent — safe to
    call multiple times.
    """
    if not text:
        return ""

    # 1. Bold / italic — triple → double → single, greedy outer to inner
    text = re.sub(r'\*\*\*([^\n*]+?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*([^\n*]+?)\*\*', r'\1', text)
    # Single-asterisk italics: allow no word-boundary on either side
    # (handles '*Sponsor* and' AND 'Sponsor*'). Done twice to catch overlaps.
    for _ in range(2):
        text = re.sub(r'(?<!\*)\*([^\n*]+?)\*(?!\*)', r'\1', text)
    # Stray asterisks that survived (unmatched pairs)
    text = re.sub(r'(?<!\\)\*+', '', text)

    # Underscore emphasis
    text = re.sub(r'__([^\n_]+?)__', r'\1', text)
    text = re.sub(r'(?<!_)_([^\n_]+?)_(?!_)', r'\1', text)

    # 2. ATX headers — with OR without trailing space ('##Summary' counts)
    text = re.sub(r'^\s{0,3}#{1,6}\s*', '', text, flags=re.MULTILINE)

    # 3. Code fences and inline code
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`\n]+)`', r'\1', text)
    # Orphan backticks
    text = text.replace('`', '')

    # 4. Horizontal rules on their own line
    text = re.sub(r'^\s*(?:[-*_]\s*){3,}\s*$', '', text, flags=re.MULTILINE)

    # 5. Blockquote markers
    text = re.sub(r'^\s{0,3}>\s?', '', text, flags=re.MULTILINE)

    # 6. Markdown tables — strip separator rows '|---|---|', keep data rows
    #    but drop the pipes so content survives as plain text. We replace
    #    separator rows with a blank line so adjacent data rows don't
    #    collide into one line when the separator is removed.
    text = re.sub(r'^\s*\|?\s*(?::?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$',
                  '', text, flags=re.MULTILINE)
    # Convert '| cell | cell |' to 'cell | cell' (keep content, lose borders)
    def _detable(m):
        row = m.group(0).strip().strip('|')
        cells = [c.strip() for c in row.split('|')]
        return ' — '.join(c for c in cells if c)
    text = re.sub(r'^\s*\|.+\|\s*$', _detable, text, flags=re.MULTILINE)

    # 7. Bullets — normalize unicode bullets and '* ' to '- '
    text = re.sub(r'^\s*[\u2022\u2023\u25E6\u2043\u25AA\u25AB]\s*', '- ',
                  text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\*\s+', '- ', text, flags=re.MULTILINE)

    # 8. Preamble phrases Claude sometimes opens with despite instructions
    preamble_patterns = [
        r'^(?:Based on|Looking at|I\'ll|Let me|Here\'s my|After reviewing).*?\n',
        r'^(?:I have|I can see|The provided|According to the).*?\n',
        r'^(?:Here is|Below is|The following is).*?\n',
    ]
    for pat in preamble_patterns:
        text = re.sub(pat, '', text, count=1, flags=re.IGNORECASE)

    # 9. Collapse runs of 3+ blank lines (may result from stripping)
    text = re.sub(r'\n{3,}', '\n\n', text)

    return text.strip()


def safe_filename(name: str) -> str:
    s = re.sub(r"[^\w\- ]", "", name).strip().replace(" ", "_")
    return re.sub(r"_+", "_", s)


def parse_date_arg(s: str) -> datetime:
    for fmt in ["%m/%d/%Y", "%m-%d-%Y", "%Y-%m-%d"]:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    raise ValueError(f"Invalid date: {s}. Use M/D/YYYY or YYYY-MM-DD.")


def load_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    env_path = Path(__file__).parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.strip().startswith("ANTHROPIC_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("No API key. Create .env with ANTHROPIC_API_KEY=sk-ant-...")


def now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def extract_watch_points(part1: str) -> tuple[str, str]:
    """Split Part 1 text into (summary_without_watch_points, watch_points)."""
    wp_idx = part1.upper().find("WATCH POINTS:")
    if wp_idx >= 0:
        watch = part1[wp_idx + len("WATCH POINTS:"):].strip()
        end_idx = watch.find("\n\n")
        if end_idx > 0:
            watch = watch[:end_idx].strip()
        summary = part1[:wp_idx].strip()
        return summary, watch
    return part1, ""


def parse_item_family(committee_item_number: str) -> tuple[str, str]:
    """Given a committee item number like '3C', '3C1', '3C Supplement',
    '3C SUB', return (base, relationship_kind) where:
      - base is the head item the sub-items/supplements attach to
        (e.g. '3C' for all of the above)
      - relationship_kind is one of:
          'base'        — this IS the base item (no modifier)
          'sub'         — sub-item like '3C1'
          'supplement'  — marked SUPPLEMENT
          'substitute'  — marked SUBSTITUTE / SUB
          ''            — could not parse

    Returns ('', '') if the input doesn't look like a Miami-Dade item code.
    Examples:
      '3C'            -> ('3C', 'base')
      '3C1'           -> ('3C', 'sub')
      '3C SUPPLEMENT' -> ('3C', 'supplement')
      '1G'            -> ('1G', 'base')
      '1G1'           -> ('1G', 'sub')
      ''              -> ('', '')
      'abc'           -> ('', '')
    """
    if not committee_item_number:
        return "", ""
    s = committee_item_number.strip().upper()

    # Modifier suffixes first
    mod_kind = ""
    for kw, kind in [("SUPPLEMENT", "supplement"),
                     ("SUBSTITUTE", "substitute"),
                     (" SUB", "substitute")]:
        if s.endswith(kw) or kw.strip() in s:
            mod_kind = kind
            s = re.sub(r'\s+(?:SUPPLEMENT|SUBSTITUTE|SUB)\s*$', '', s).strip()
            break

    # After stripping modifier, expect digits+letter+optional digits
    m = re.match(r'^(\d+[A-Z])(\d*)$', s)
    if not m:
        return "", ""
    base_letter = m.group(1)      # e.g. '3C'
    trailing = m.group(2)          # e.g. '1' for '3C1', '' for '3C'

    if mod_kind:
        # 'X SUPPLEMENT' or 'XN SUPPLEMENT' — the base is X (section+letter)
        return base_letter, mod_kind
    if trailing:
        return base_letter, "sub"
    return base_letter, "base"


def sort_items_by_family(items: list) -> list:
    """Stable-sort items so that bases come before their sub-items and
    supplements. Items with unparseable numbers are sorted to the end.
    Input: list of dicts with 'committee_item_number'.
    Output: new list (input not mutated)."""
    def key(it):
        num = it.get("committee_item_number", "")
        base, kind = parse_item_family(num)
        if not base:
            return (2, num)  # unparseable → end
        rank = {"base": 0, "sub": 1, "supplement": 1, "substitute": 1}.get(kind, 1)
        # Natural sort on base: split '1G' → (1, 'G')
        m = re.match(r'^(\d+)([A-Z])$', base)
        base_key = (int(m.group(1)), m.group(2)) if m else (999, base)
        return (0, base_key, rank, num)
    return sorted(items, key=key)
