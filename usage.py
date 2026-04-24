"""
usage.py — API usage tracking and cost controls for AgendaIQ

Tracks every Anthropic API call per organization:
  - Token counts (input, output, cached)
  - Estimated cost based on Haiku pricing
  - Daily and monthly aggregation
  - Per-org rate limits with hard caps

Pricing (Claude Haiku as of April 2026):
  Input:  $0.25 / 1M tokens
  Output: $1.25 / 1M tokens
  Cached: $0.025 / 1M tokens (90% discount on input)
"""

import logging
import json
from datetime import datetime, timedelta
from db import get_db
from utils import now_iso

log = logging.getLogger("oca-agent")

# Haiku pricing per million tokens
PRICE_INPUT_PER_M = 0.25
PRICE_OUTPUT_PER_M = 1.25
PRICE_CACHED_PER_M = 0.025


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


def record_usage(org_id: int, call_type: str,
                 tokens_in: int = 0, tokens_out: int = 0,
                 cached_tokens: int = 0, model: str = "claude-haiku-4-5-20251001",
                 appearance_id: int = None, meeting_id: int = None):
    """Record a single API call's usage.

    call_type: 'analyze', 'synthesize', 'chat', 'delta', 'segment', 'final'
    """
    cost = estimate_cost(tokens_in, tokens_out, cached_tokens)
    now = now_iso()
    today = now[:10]  # YYYY-MM-DD

    try:
        with get_db() as conn:
            conn.execute(
                """INSERT INTO api_usage
                   (org_id, call_type, model, tokens_in, tokens_out,
                    cached_tokens, cost_estimate, appearance_id, meeting_id,
                    call_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (org_id, call_type, model, tokens_in, tokens_out,
                 cached_tokens, cost, appearance_id, meeting_id,
                 today, now)
            )
    except Exception as e:
        log.warning(f"Failed to record API usage: {e}")


def estimate_cost(tokens_in: int, tokens_out: int, cached_tokens: int = 0) -> float:
    """Estimate cost in USD for a single API call."""
    # Cached tokens are billed at 10% of input price
    effective_input = max(0, tokens_in - cached_tokens)
    cost = (effective_input * PRICE_INPUT_PER_M / 1_000_000
            + cached_tokens * PRICE_CACHED_PER_M / 1_000_000
            + tokens_out * PRICE_OUTPUT_PER_M / 1_000_000)
    return round(cost, 6)


def get_usage_summary(org_id: int, days: int = 30) -> dict:
    """Get usage summary for an org over the last N days."""
    oid = _resolve_org_id(org_id)
    cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    today = datetime.utcnow().strftime("%Y-%m-%d")
    month_start = today[:7] + "-01"  # First day of current month

    with get_db() as conn:
        # Total this month
        month_row = conn.execute(
            """SELECT COALESCE(SUM(tokens_in), 0) as total_in,
                      COALESCE(SUM(tokens_out), 0) as total_out,
                      COALESCE(SUM(cached_tokens), 0) as total_cached,
                      COALESCE(SUM(cost_estimate), 0) as total_cost,
                      COUNT(*) as total_calls
               FROM api_usage
               WHERE org_id = ? AND call_date >= ?""",
            (oid, month_start)
        ).fetchone()

        # Today
        today_row = conn.execute(
            """SELECT COALESCE(SUM(tokens_in), 0) as total_in,
                      COALESCE(SUM(tokens_out), 0) as total_out,
                      COALESCE(SUM(cost_estimate), 0) as total_cost,
                      COUNT(*) as total_calls
               FROM api_usage
               WHERE org_id = ? AND call_date = ?""",
            (oid, today)
        ).fetchone()

        # By call type this month
        by_type = conn.execute(
            """SELECT call_type,
                      COUNT(*) as calls,
                      COALESCE(SUM(tokens_in), 0) as tokens_in,
                      COALESCE(SUM(tokens_out), 0) as tokens_out,
                      COALESCE(SUM(cost_estimate), 0) as cost
               FROM api_usage
               WHERE org_id = ? AND call_date >= ?
               GROUP BY call_type
               ORDER BY cost DESC""",
            (oid, month_start)
        ).fetchall()

        # Daily breakdown (last N days)
        daily = conn.execute(
            """SELECT call_date,
                      COUNT(*) as calls,
                      COALESCE(SUM(tokens_in + tokens_out), 0) as total_tokens,
                      COALESCE(SUM(cost_estimate), 0) as cost
               FROM api_usage
               WHERE org_id = ? AND call_date >= ?
               GROUP BY call_date
               ORDER BY call_date""",
            (oid, cutoff)
        ).fetchall()

    return {
        "month": {
            "tokens_in": month_row["total_in"],
            "tokens_out": month_row["total_out"],
            "tokens_cached": month_row["total_cached"],
            "cost": round(month_row["total_cost"], 4),
            "calls": month_row["total_calls"],
        },
        "today": {
            "tokens_in": today_row["total_in"],
            "tokens_out": today_row["total_out"],
            "cost": round(today_row["total_cost"], 4),
            "calls": today_row["total_calls"],
        },
        "by_type": [dict(r) for r in by_type],
        "daily": [dict(r) for r in daily],
    }


# ── Rate Limiting ─────────────────────────────────────────────

def check_limits(org_id: int) -> dict:
    """Check if an org has exceeded its usage limits.

    Returns {"allowed": True/False, "reason": str, "usage": {...}}

    Limits are stored in org config as:
      monthly_token_limit: int (0 = unlimited)
      daily_request_limit: int (0 = unlimited)
      monthly_cost_limit: float (0 = unlimited)
    """
    oid = _resolve_org_id(org_id)

    # Load limits from org config
    from org_config import get_org_config
    cfg = get_org_config(oid)
    monthly_token_limit = cfg.get("monthly_token_limit", 0)
    daily_request_limit = cfg.get("daily_request_limit", 0)
    monthly_cost_limit = cfg.get("monthly_cost_limit", 0)

    # No limits set = unlimited
    if not monthly_token_limit and not daily_request_limit and not monthly_cost_limit:
        return {"allowed": True, "reason": "no limits configured", "usage": {}}

    today = datetime.utcnow().strftime("%Y-%m-%d")
    month_start = today[:7] + "-01"

    with get_db() as conn:
        # Monthly tokens
        month_tokens = conn.execute(
            """SELECT COALESCE(SUM(tokens_in + tokens_out), 0) as total
               FROM api_usage WHERE org_id = ? AND call_date >= ?""",
            (oid, month_start)
        ).fetchone()["total"]

        # Monthly cost
        month_cost = conn.execute(
            """SELECT COALESCE(SUM(cost_estimate), 0) as total
               FROM api_usage WHERE org_id = ? AND call_date >= ?""",
            (oid, month_start)
        ).fetchone()["total"]

        # Daily requests
        today_calls = conn.execute(
            """SELECT COUNT(*) as total
               FROM api_usage WHERE org_id = ? AND call_date = ?""",
            (oid, today)
        ).fetchone()["total"]

    usage = {
        "month_tokens": month_tokens,
        "month_cost": round(month_cost, 4),
        "today_calls": today_calls,
        "monthly_token_limit": monthly_token_limit,
        "daily_request_limit": daily_request_limit,
        "monthly_cost_limit": monthly_cost_limit,
    }

    if monthly_token_limit and month_tokens >= monthly_token_limit:
        return {
            "allowed": False,
            "reason": f"Monthly token limit reached ({month_tokens:,} / {monthly_token_limit:,})",
            "usage": usage,
        }

    if monthly_cost_limit and month_cost >= monthly_cost_limit:
        return {
            "allowed": False,
            "reason": f"Monthly cost limit reached (${month_cost:.2f} / ${monthly_cost_limit:.2f})",
            "usage": usage,
        }

    if daily_request_limit and today_calls >= daily_request_limit:
        return {
            "allowed": False,
            "reason": f"Daily request limit reached ({today_calls} / {daily_request_limit})",
            "usage": usage,
        }

    return {"allowed": True, "reason": "within limits", "usage": usage}
