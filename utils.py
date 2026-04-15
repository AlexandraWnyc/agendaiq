"""
utils.py — Shared utilities for OCA Agenda Intelligence v6
"""
import os, re, logging
from pathlib import Path
from datetime import datetime

log = logging.getLogger("oca-agent")


# ── Text cleaning ─────────────────────────────────────────────

def clean_markdown(text: str) -> str:
    """Strip markdown formatting that Claude may insert despite instructions."""
    if not text:
        return ""
    text = re.sub(r'\*\*\*(.+?)\*\*\*', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'(?<!\w)\*(.+?)\*(?!\w)', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text, flags=re.S)
    text = re.sub(r'^#{1,4}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'`([^`]+)`', r'\1', text)
    text = re.sub(r'^\s*[\u2022\u2023\u25E6\u2043\u25AA\u25AB]\s*', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\*\s+', '- ', text, flags=re.MULTILINE)
    preamble_patterns = [
        r'^(?:Based on|Looking at|I\'ll|Let me|Here\'s my|After reviewing).*?\n',
        r'^(?:I have|I can see|The provided|According to the).*?\n',
    ]
    for pat in preamble_patterns:
        text = re.sub(pat, '', text, count=1)
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
