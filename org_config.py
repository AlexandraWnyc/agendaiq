"""
org_config.py — DB-backed per-organization configuration for AgendaIQ

Each organization's jurisdiction-specific settings are stored as JSON in
the organizations.settings column. This module provides:
  - A typed config schema with defaults
  - get_org_config(org_id) to load config for any org
  - save_org_config(org_id, config) to persist changes
  - get_current_config() shortcut using Flask g.org_id
  - MIAMI_DADE_DEFAULTS: seed config for the default org

All scraper, analyzer, transcript, and lifecycle modules read from this
instead of hardcoded constants. Adding a new jurisdiction = inserting a
new org row with the right settings JSON.
"""

import json
import logging
from db import get_db

log = logging.getLogger("oca-agent")


# ══════════════════════════════════════════════════════════════════
# CONFIG SCHEMA — every key that can vary by jurisdiction
# ══════════════════════════════════════════════════════════════════

MIAMI_DADE_DEFAULTS = {
    # ── Branding ──
    "jurisdiction_name": "Miami-Dade County",
    "org_display_name": "Office of the Commission Auditor",
    "org_short_name": "OCA",
    "body_name": "Board of County Commissioners",
    "body_short_name": "BCC",

    # ── Legistar / Agenda Source ──
    "legistar_base_url": "https://www.miamidade.gov/govaction/",
    "agenda_source_type": "legistar",  # legistar | granicus | civicclerk | custom

    # ── Committees ──
    # Maps display name → short code (used by scraper for form submission)
    "committees": {
        "Appropriations Committee": "APC",
        "Aviation and Seaport Committee": "AASC",
        "BCC - Comprehensive Development Master Plan & Zoning": "CDMZ",
        "Board of County Commissioners": "CC",
        "Government Efficiency & Transparency Ad Hoc Cmte": "GETC",
        "Housing Committee": "HOUS",
        "Infrastructure, Innovation & Technology Committee": "IITC",
        "Intergovernmental and Economic Impact Committee": "IEIC",
        "Joint Appropriations & Gov Eff & Transparency Cmte": "JAGE",
        "Policy Council": "PC",
        "Recreation, Tourism, and Resiliency Committee": "RTRC",
        "Safety and Health Committee": "SHC",
        "Transportation Cmte": "TRNS",
    },

    # ── Transcript Sources ──
    "youtube_channel_id": "UCjwHcRTA0ZuOdsXxEBKnq0A",
    "youtube_channel_url": "https://www.youtube.com/@miami-dadebcc6863",
    "granicus_urls": [
        "https://miamidade.granicus.com/ViewPublisher.php?view_id=3",  # BCC
        "https://miamidade.granicus.com/ViewPublisher.php?view_id=4",  # Committees
    ],

    # ── Committee Aliases (for YouTube title matching) ──
    "committee_aliases": {
        "Board of County Commissioners": [
            "Board of County Commissioners",
            "BCC Regular", "BCC Meeting", "County Commission",
        ],
        "Comprehensive Development Master Plan & Zoning": [
            "Master Plan & Zoning", "Master Plan and Zoning",
            "Zoning", "CDMP", "Comprehensive Development",
        ],
        "Government Operations": [
            "Government Operations", "Gov Operations", "Gov Ops",
        ],
        "Infrastructure, Innovation, and Technology": [
            "Infrastructure", "Innovation and Technology",
            "Infrastructure, Innovation",
        ],
        "Housing and Community Development": [
            "Housing", "Community Development",
            "Housing and Community", "Housing Committee",
        ],
        "Public Safety and Rehabilitation": [
            "Public Safety", "Rehabilitation",
        ],
        "Intergovernmental and Economic Impact": [
            "Intergovernmental", "Economic Impact",
            "Intergovernmental and Economic",
        ],
        "Recreation, Tourism, and Resiliency": [
            "Recreation", "Tourism", "Resiliency",
            "Recreation, Tourism",
        ],
        "Transportation and Finance": [
            "Transportation", "Finance",
            "Transportation and Finance",
        ],
        "Aviation and Seaport": [
            "Aviation", "Seaport",
            "Aviation and Seaport",
        ],
        "Health Care and County Operations": [
            "Health Care", "County Operations",
            "Health Care and County",
        ],
        "Parks and Culture": [
            "Parks", "Culture", "Parks and Culture",
        ],
        "Trade and Tourism": [
            "Trade", "Trade and Tourism",
        ],
    },

    # ── Legislative History Parsing ──
    # Body names that appear in Legistar legislative history tables
    "body_hints": [
        "Board of County Commissioners",
        "County Commission",
        "Committee of the Whole",
        "Unincorporated Municipal Service Area",
        "Infrastructure, Operations and Innovations",
        "Community Health and Economic Resilience",
        "Housing, Recreation and Culture",
        "Public Safety and Rehabilitation",
        "Transportation, Mobility and Planning",
        "Chairperson's Policy Council",
        "Mayor",
        "Clerk of the Board",
        "Office of the Commission Auditor",
        "County Attorney",
    ],
    # Fragments used to detect body names in legislative history text
    "body_fragments": [
        "Board of County Commissioners",
        "County Commission",
        "Committee of the Whole",
        "Committee",
        "Commission",
        "Council",
        "Mayor",
        "Clerk of the Board",
        "Office of",
        "County Attorney",
    ],

    # ── AI Analysis Context ──
    # Injected into the system prompt so the AI knows the jurisdiction
    "ai_jurisdiction_context": (
        "You are analyzing agenda items for the Miami-Dade County "
        "Board of County Commissioners (BCC) and its committees. "
        "The Office of the Commission Auditor (OCA) prepares briefings "
        "for County Commissioners."
    ),
}

# Bare-minimum defaults for a new org (everything else uses schema defaults)
_EMPTY_DEFAULTS = {
    "jurisdiction_name": "",
    "org_display_name": "",
    "org_short_name": "",
    "body_name": "",
    "body_short_name": "",
    "legistar_base_url": "",
    "agenda_source_type": "legistar",
    "committees": {},
    "youtube_channel_id": "",
    "youtube_channel_url": "",
    "granicus_urls": [],
    "committee_aliases": {},
    "body_hints": [],
    "body_fragments": [],
    "ai_jurisdiction_context": "",
}


# ══════════════════════════════════════════════════════════════════
# READ / WRITE
# ══════════════════════════════════════════════════════════════════

_config_cache: dict[int, dict] = {}


def get_org_config(org_id: int) -> dict:
    """Load config for an organization. Merges DB settings over defaults.
    Cached per org_id for the lifetime of the process (cleared on save)."""
    if org_id in _config_cache:
        return _config_cache[org_id]

    # Start with empty defaults
    config = dict(_EMPTY_DEFAULTS)

    # For org_id=1, use Miami-Dade defaults as the base
    if org_id == 1:
        config = dict(MIAMI_DADE_DEFAULTS)

    # Overlay anything stored in the DB
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT settings FROM organizations WHERE id = ?", (org_id,)
            ).fetchone()
            if row and row["settings"]:
                db_settings = json.loads(row["settings"])
                if isinstance(db_settings, dict):
                    config.update(db_settings)
    except Exception as e:
        log.warning(f"Could not load org config for org_id={org_id}: {e}")

    _config_cache[org_id] = config
    return config


def save_org_config(org_id: int, config: dict):
    """Persist config to the organizations.settings column."""
    from utils import now_iso
    settings_json = json.dumps(config, ensure_ascii=False)
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE organizations SET settings = ?, updated_at = ? WHERE id = ?",
                (settings_json, now_iso(), org_id)
            )
        # Clear cache
        _config_cache.pop(org_id, None)
        log.info(f"Saved org config for org_id={org_id}")
    except Exception as e:
        log.error(f"Failed to save org config for org_id={org_id}: {e}")
        raise


def get_current_config() -> dict:
    """Get config for the current request's org. Falls back to org_id=1."""
    try:
        from flask import g
        if hasattr(g, 'org_id') and g.org_id is not None:
            return get_org_config(g.org_id)
    except (ImportError, RuntimeError):
        pass
    return get_org_config(1)


def clear_config_cache(org_id: int = None):
    """Clear cached config. Call after updating org settings."""
    if org_id:
        _config_cache.pop(org_id, None)
    else:
        _config_cache.clear()


# ══════════════════════════════════════════════════════════════════
# CONVENIENCE ACCESSORS
# ══════════════════════════════════════════════════════════════════

def get_legistar_base_url(org_id: int = None) -> str:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("legistar_base_url", "")


def get_committees(org_id: int = None) -> dict:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("committees", {})


def get_committee_aliases(org_id: int = None) -> dict:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("committee_aliases", {})


def get_granicus_urls(org_id: int = None) -> list:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("granicus_urls", [])


def get_youtube_channel(org_id: int = None) -> tuple[str, str]:
    """Returns (channel_id, channel_url)."""
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("youtube_channel_id", ""), cfg.get("youtube_channel_url", "")


def get_body_hints(org_id: int = None) -> list:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("body_hints", [])


def get_body_fragments(org_id: int = None) -> list:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("body_fragments", [])


def get_ai_context(org_id: int = None) -> str:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("ai_jurisdiction_context", "")


def get_jurisdiction_name(org_id: int = None) -> str:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("jurisdiction_name", "")


def get_org_display_name(org_id: int = None) -> str:
    cfg = get_org_config(org_id) if org_id else get_current_config()
    return cfg.get("org_display_name", "")
