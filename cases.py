"""
cases.py — Case layer for OCA Agenda Intelligence v6

A Case is the lifecycle entity above a matter. One Case can span multiple
agenda items (matters) across multiple meetings. Example: a CDMP
application CDMP20250013 is one Case whose members include:
    3C  (ordinance    — Final Action)
    3C1 (resolution   — Transmittal)
    3C Supplement     — Supporting Document

This module provides:
  - Application-number extraction (CDMP, contract, zoning, generic)
  - Case-type classification
  - Role classification (what role does this matter play in its case)
  - Stage classification (where is the case in its lifecycle)
  - The linker that auto-links matters to cases during ingest
  - Repository-style query helpers for loading full cases

Design notes:
  - Every matter MUST be linkable to a case. If no application number can
    be extracted, a synthetic case is created (one-matter case) so UI
    code never has to special-case "no case".
  - Auto-linking uses a confidence score. Above threshold: confirmed.
    Below threshold: 'candidate' — held for researcher review.
  - Role and stage use structured category + free-text label. The category
    enables cross-case-type aggregation (e.g. "all pending decisions")
    while the label preserves domain-specific wording for humans.
"""
import re
import hashlib
import logging
from datetime import datetime
from typing import Optional

from db import get_db
from utils import now_iso
from schema import (
    CASE_TYPES, ROLE_CATEGORIES, STAGE_CATEGORIES,
    LINK_STATUSES, LINK_METHODS, CASE_LINK_AUTO_CONFIRM_THRESHOLD,
)

log = logging.getLogger("oca-agent")


# ══════════════════════════════════════════════════════════════════
# APPLICATION-NUMBER EXTRACTION
# ══════════════════════════════════════════════════════════════════
# Each pattern is (case_type, regex, confidence). Confidence reflects
# how sure we are about the PATTERN, not the specific match — a match
# against a CDMP regex is very high confidence that this is a CDMP case.
#
# Patterns marked LOW are my best guesses from general Miami-Dade naming
# conventions. Flag them if they match too liberally or miss real numbers.

# CDMP = Comprehensive Development Master Plan. Application numbers
# follow a CDMP + year + sequence pattern. Miami-Dade's published
# examples use the form CDMP20250013. We also accept the hyphenated
# human-typing form 'CDMP 2025-0013' and normalize on extraction.
_PAT_CDMP = re.compile(
    r'\bCDMP\s*-?\s*(\d{4}\s*-?\s*\d{2,6})\b', re.IGNORECASE
)

# Contracts — several conventions appear on Miami-Dade agendas. These
# patterns are MEDIUM confidence; flag false positives when you see them.
# RFP / RFQ / ITB / CPA solicitation numbers often look like:
#   RFP-00123, RFP No. 00123, Solicitation #RFP-01234
#   Contract No. 1234-5/6, Agreement 2026-001
_PAT_CONTRACT_SOLICITATION = re.compile(
    r'\b(?:RFP|RFQ|ITB|CPA|ITN|IFB)\s*(?:No\.?|#|-)?\s*([A-Z0-9][A-Z0-9\-]{3,20})\b',
    re.IGNORECASE,
)
# Contract numbers often appear as "Contract No. X" or "Agreement No. X"
# where X is digits with optional hyphens. BEST GUESS — verify.
_PAT_CONTRACT_NUMBER = re.compile(
    r'\b(?:Contract|Agreement)\s+(?:No\.?|Number|#)\s*([A-Z0-9][A-Z0-9\-/]{2,20})\b',
    re.IGNORECASE,
)

# Zoning applications. Miami-Dade uses two live formats:
#   long form : Z2025000130  (Z + 4-digit year + 6-digit sequence)
#              also appears as 'Z20250000130' in some exports.
#   short form: Z25-130      (Z + 2-digit year + 1-4 digit sequence,
#                            typically on staff-report headers)
# We extract both and normalize to the long form on write so they
# collapse to one Case. The short-form inference uses the convention
# that 2-digit years are 2000+YY.
_PAT_ZONING_LONG = re.compile(
    r'\bZ\s*-?\s*(\d{4})\s*-?\s*(\d{3,7})\b',
    re.IGNORECASE,
)
_PAT_ZONING_SHORT = re.compile(
    r'\bZ\s*-?\s*(\d{2})\s*-\s*(\d{1,4})\b',
    re.IGNORECASE,
)

# Ordinance numbers — when an item references an existing ordinance by
# number, that ordinance IS the case anchor for subsequent amendments.
# Format: "Ordinance No. 25-XX" or "Ord. 2025-045".
_PAT_ORDINANCE_NUM = re.compile(
    r'\bOrdinance\s+(?:No\.?\s+)?(\d{2,4}-\d{1,4})\b',
    re.IGNORECASE,
)

# Resolution numbers — similar.
_PAT_RESOLUTION_NUM = re.compile(
    r'\bResolution\s+(?:No\.?\s+)?R?-?(\d{2,4}-\d{1,4})\b',
    re.IGNORECASE,
)


# List of (name, pattern, case_type, confidence). Order matters — first
# match wins, so put the most specific patterns first. The zoning long
# form is listed before the short form so that 'Z2025000130' matches
# whole-token and is never misread as Z25 + 000130.
_EXTRACTION_PATTERNS = [
    ("cdmp",          _PAT_CDMP,                  "cdmp",       0.98),
    ("zoning_long",   _PAT_ZONING_LONG,           "zoning",     0.90),
    ("zoning_short",  _PAT_ZONING_SHORT,          "zoning",     0.80),
    ("rfp",           _PAT_CONTRACT_SOLICITATION, "contract",   0.80),  # LOW-MED — verify
    ("contract",      _PAT_CONTRACT_NUMBER,       "contract",   0.75),  # LOW — verify
    ("ordinance",     _PAT_ORDINANCE_NUM,         "ordinance",  0.70),  # LOW — ambiguous
    ("resolution",    _PAT_RESOLUTION_NUM,        "resolution", 0.60),  # LOW — ambiguous
]


def extract_application_numbers(text: str) -> list[dict]:
    """Scan free text for all recognized application number patterns.

    Returns a list of dicts, each:
      {
        "number": "CDMP20250013",   # normalized canonical form
        "case_type": "cdmp",
        "pattern_name": "cdmp",
        "confidence": 0.98,
        "raw_match": "CDMP 2025-0013",  # as found in source
        "match_start": 123,         # char offset in input text
        "match_end":   141,
      }

    match_start/end are used by the companion detector to look at the
    surrounding context window for relation-signal phrases like
    'concurrent' or 'subject to approval of'.

    Empty list if nothing matches. Order preserved by pattern priority.
    Duplicates (same normalized number) are collapsed to the first
    (highest-confidence) occurrence; match positions of later hits are
    merged in as additional_positions so context-window scans can use
    multiple mention sites.
    """
    if not text:
        return []
    out = []
    by_norm: dict[str, dict] = {}
    for pat_name, pat, ctype, conf in _EXTRACTION_PATTERNS:
        for m in pat.finditer(text):
            raw = m.group(0)
            # For multi-group patterns (zoning long/short), rebuild the
            # underlying digits. For single-group patterns, group(1) is
            # the payload.
            if pat_name == "zoning_long":
                year, seq = m.group(1), m.group(2)
                norm = _normalize_zoning(year, seq)
            elif pat_name == "zoning_short":
                yy, seq = m.group(1), m.group(2)
                # Infer full year: 2-digit YY → 2000+YY (works for the
                # foreseeable decades; if this code lives past 2099
                # someone will need to revisit the century roll).
                year = f"20{yy}"
                norm = _normalize_zoning(year, seq)
            else:
                num = m.group(1) if m.groups() else raw
                norm = _normalize_app_number(num, ctype)

            entry = {
                "number":      norm,
                "case_type":   ctype,
                "pattern_name": pat_name,
                "confidence":  conf,
                "raw_match":   raw.strip(),
                "match_start": m.start(),
                "match_end":   m.end(),
            }
            if norm in by_norm:
                # Keep first occurrence (highest-confidence pattern ran
                # first), but record the additional position — extras
                # are useful signal for companion detection since one
                # passing mention is weaker than multiple mentions.
                by_norm[norm].setdefault("additional_positions", []).append(
                    (m.start(), m.end())
                )
                continue
            by_norm[norm] = entry
            out.append(entry)
    return out


def _normalize_zoning(year: str, seq: str) -> str:
    """Given year + sequence digits from a zoning number, produce the
    canonical long form. Convention: year is 4 digits, sequence is
    zero-padded to 6 digits. So Z25-130 → Z2025000130,
    Z2025000130 → Z2025000130, Z2025-0130 → Z2025000130."""
    year = year.zfill(4)
    seq  = seq.lstrip("0") or "0"
    seq  = seq.zfill(6)
    return f"Z{year}{seq}"


def _normalize_app_number(raw: str, case_type: str) -> str:
    """Normalize an extracted app number for use as a stable key.
    Different case types normalize differently: CDMP strips all spaces
    and hyphens; zoning keeps hyphens because they're semantic."""
    if not raw:
        return ""
    s = raw.strip().upper()
    if case_type == "cdmp":
        # CDMP 2025-0013 → CDMP20250013
        digits = re.sub(r'[^0-9]', '', s)
        return f"CDMP{digits}"
    if case_type == "zoning":
        # Normalize internal spaces away but keep hyphens
        return re.sub(r'\s+', '', s)
    if case_type == "contract":
        return re.sub(r'\s+', '', s)
    # Default
    return re.sub(r'\s+', '', s)


def synthesize_application_number(file_number: str, short_title: str = "") -> str:
    """Create a stable synthetic application number for a matter that has
    no extractable real one. Shape: SYNTH-<file_number>. Deterministic so
    re-running the linker doesn't create duplicates."""
    if file_number:
        return f"SYNTH-{file_number}"
    # Pathological: no file_number — hash title
    h = hashlib.md5((short_title or "unknown").encode()).hexdigest()[:10]
    return f"SYNTH-H-{h.upper()}"


# ══════════════════════════════════════════════════════════════════
# CASE-TYPE CLASSIFIER
# ══════════════════════════════════════════════════════════════════

def classify_case_type(
    short_title: str = "",
    full_title: str = "",
    file_type: str = "",
    extracted_numbers: list[dict] | None = None,
) -> tuple[str, float]:
    """Decide what kind of case this matter belongs to. Returns
    (case_type, confidence ∈ [0,1]).

    Strategy (revised April 2026 after companion-bug):
      1. Run keyword rules on the matter's OWN short_title + full_title
         + file_type. This identifies what the matter IS.
      2. If an extracted application number agrees with the keyword
         result, confirm at higher confidence.
      3. If there's no keyword hit but there are extractions, fall back
         to the highest-confidence extraction.
      4. If there's nothing usable, return 'other' at low confidence.

    Why: a zoning Resolution whose staff report *references* a CDMP
    amendment will have many CDMP numbers in its PDF text. The old
    logic picked the top-confidence extraction and misrouted the
    matter into the CDMP case. By checking the matter's own title
    first, we correctly identify the matter's type even when its
    attached documents discuss other cases.
    """
    extracted_numbers = extracted_numbers or []
    text = f"{short_title} {full_title} {file_type}".lower()

    # Keyword rules — these describe what a matter IS (from its own
    # title/file_type), not what it mentions.
    keyword_rules = [
        # (case_type, confidence, keywords_any_of)
        ("zoning",      0.80, ["rezoning", "zoning change", "district boundary",
                                "zoning hearing", "special exception", "variance",
                                "zoning application"]),
        ("cdmp",        0.80, ["cdmp", "comprehensive development master plan"]),
        ("contract",    0.70, ["award contract", "contract award", "procurement",
                                "solicitation", "purchase order", "rfp", "rfq",
                                "change order", "contract amendment",
                                "professional services agreement"]),
        ("appointment", 0.85, ["appointment", "appointee", "reappoint",
                                "board member", "nomination"]),
        ("ceremonial",  0.90, ["proclamation", "recognize", "commendation",
                                "honorary", "resolution recognizing",
                                "resolution commending"]),
        ("report",      0.75, ["report to the board", "informational report",
                                "quarterly report", "annual report"]),
    ]

    keyword_hit = None
    for ctype, conf, kws in keyword_rules:
        if any(kw in text for kw in kws):
            keyword_hit = (ctype, conf)
            break

    if keyword_hit:
        ctype, conf = keyword_hit
        # Boost confidence if an extracted number of the matching type
        # corroborates the keyword hit.
        corroborated = any(e["case_type"] == ctype for e in extracted_numbers)
        if corroborated:
            conf = min(0.98, conf + 0.12)
        return ctype, conf

    # No keyword hit — fall back on extractions
    if extracted_numbers:
        top = max(extracted_numbers, key=lambda e: e["confidence"])
        return top["case_type"], top["confidence"] * 0.85  # slight discount

    # Fall back on file_type alone
    ft = file_type.lower()
    if "ordinance" in ft:
        return "ordinance", 0.50
    if "resolution" in ft:
        return "resolution", 0.45

    return "other", 0.30


# ══════════════════════════════════════════════════════════════════
# ROLE CLASSIFIER — what role does THIS matter play in its case?
# ══════════════════════════════════════════════════════════════════

def classify_role(
    case_type: str,
    short_title: str = "",
    full_title: str = "",
    file_type: str = "",
    committee_item_number: str = "",
    agenda_stage: str = "",
) -> tuple[str, str, float]:
    """Return (role_category, role_label, confidence).

    role_category is one of ROLE_CATEGORIES (generic, cross-type).
    role_label is human-readable and case-type-aware ("Final Ordinance",
    "Transmittal Resolution", "Staff Analysis").
    """
    text = f"{short_title} {full_title}".lower()
    ft   = (file_type or "").lower()
    stage_low = (agenda_stage or "").lower()

    # Supplement / substitute items are always supporting role regardless
    # of case type — the sibling logic already flagged them.
    item_num_upper = (committee_item_number or "").upper()
    if ("SUPPLEMENT" in item_num_upper or "SUBSTITUTE" in item_num_upper
            or "supplement" in stage_low):
        return "supporting", "Supplement / Staff Analysis", 0.95

    # CDMP-specific rules
    if case_type == "cdmp":
        if "ordinance" in ft:
            # CDMP ordinance = final action (adoption)
            if any(kw in text for kw in ["adopt", "final action", "amendment"]):
                return "decision", "Final Ordinance (Adoption)", 0.90
            return "decision", "CDMP Ordinance", 0.75
        if "resolution" in ft:
            # CDMP resolution typically transmits the amendment to the state
            if any(kw in text for kw in ["transmit", "transmittal", "send to",
                                          "department of economic opportunity",
                                          "state land planning"]):
                return "transmittal", "Transmittal Resolution", 0.90
            return "review", "CDMP Resolution", 0.65
        return "review", "CDMP Item", 0.50

    # Contract-specific rules
    if case_type == "contract":
        if any(kw in text for kw in ["award", "award contract"]):
            return "decision", "Contract Award", 0.90
        if any(kw in text for kw in ["amend", "amendment", "modify", "modification"]):
            return "decision", "Contract Amendment", 0.85
        if any(kw in text for kw in ["extend", "extension", "renew", "renewal"]):
            return "decision", "Contract Extension", 0.85
        if any(kw in text for kw in ["change order", "task order"]):
            return "decision", "Change Order", 0.85
        if any(kw in text for kw in ["solicit", "rfp", "rfq", "itb", "issue"]):
            return "initiation", "Solicitation", 0.80
        if any(kw in text for kw in ["terminate", "terminat"]):
            return "decision", "Contract Termination", 0.90
        return "review", "Contract Item", 0.50

    # Zoning
    if case_type == "zoning":
        if any(kw in text for kw in ["hearing", "public hearing"]):
            return "review", "Zoning Hearing", 0.80
        if any(kw in text for kw in ["final", "adopt", "grant", "deny"]):
            return "decision", "Zoning Decision", 0.80
        return "review", "Zoning Review", 0.60

    # Appointments / ceremonial are always single-action
    if case_type == "appointment":
        return "decision", "Appointment Action", 0.85
    if case_type == "ceremonial":
        return "decision", "Ceremonial Action", 0.95

    # Generic ordinance / resolution
    if "ordinance" in ft:
        if any(kw in text for kw in ["first reading", "introduce"]):
            return "initiation", "Ordinance — First Reading", 0.80
        if any(kw in text for kw in ["second reading", "adopt"]):
            return "decision", "Ordinance — Adoption", 0.85
        return "decision", "Ordinance", 0.60

    if "resolution" in ft:
        return "decision", "Resolution", 0.55

    if "report" in ft or case_type == "report":
        return "supporting", "Report", 0.80

    return "unknown", "", 0.30


# ══════════════════════════════════════════════════════════════════
# STAGE CLASSIFIER — where in the lifecycle IS this case right now?
# ══════════════════════════════════════════════════════════════════

def classify_stage(
    case_type: str,
    role_category: str,
    agenda_stage: str = "",
    body_name: str = "",
    role_label: str = "",
) -> tuple[str, str, float]:
    """Return (stage_category, stage_label, confidence). Stage reflects
    the CURRENT observation: where the case is based on what it's doing
    on THIS agenda."""
    body = (body_name or "").lower()
    stage_low = (agenda_stage or "").lower()

    # Role tells us a lot. If this matter is the decision, stage depends
    # on whether it's BCC-final or committee-review.
    if role_category == "decision":
        if "bcc" in body or "board of county" in body or "bcc" in stage_low:
            return "decision_pending", f"BCC — Pending Decision", 0.85
        if "committee" in body:
            return "analysis", f"Committee Review (Decision Stage)", 0.80
        return "decision_pending", "Pending Decision", 0.70

    if role_category == "transmittal":
        return "transmittal", "Pending Transmittal Action", 0.85

    if role_category == "initiation":
        return "intake", "Introduction / Intake", 0.80

    if role_category == "review":
        if "committee" in body:
            return "analysis", "Committee Review", 0.80
        return "analysis", "Under Review", 0.70

    if role_category == "supporting":
        # Supporting doc by itself doesn't advance a stage, but signals
        # the case is currently under analysis.
        return "analysis", "Supporting Document on File", 0.75

    return "unknown", "", 0.30


# ══════════════════════════════════════════════════════════════════
# LINKER — runs during ingest to attach matters to cases
# ══════════════════════════════════════════════════════════════════

def link_matter_to_case(
    matter_id: int,
    short_title: str = "",
    full_title: str = "",
    file_type: str = "",
    file_number: str = "",
    committee_item_number: str = "",
    agenda_stage: str = "",
    body_name: str = "",
    extra_text: str = "",
) -> dict:
    """Run the full linker pipeline for a single matter.

    Steps:
      1. Scan title + full_title + extra_text for application numbers.
      2. Classify case type, role, stage.
      3. Find-or-create the case.
      4. Create or update the membership with confidence + status.
      5. Write a derived case_event recording this observation.
      6. Denormalize case_id back onto all appearances of this matter.

    Returns a dict with everything decided, for logging / UI:
      {
        "case_id":   int,
        "application_number": str,
        "is_synthetic": bool,
        "case_type": str,
        "role_category": str, "role_label": str,
        "stage_category": str, "stage_label": str,
        "link_status":    "confirmed" | "candidate",
        "link_confidence": float,
        "link_method":    str,
        "extracted_numbers": list,
      }
    """
    search_text = " ".join(filter(None, [
        short_title, full_title, extra_text,
    ]))
    extracted = extract_application_numbers(search_text)

    case_type, ct_conf = classify_case_type(
        short_title, full_title, file_type, extracted
    )
    role_cat, role_lbl, role_conf = classify_role(
        case_type, short_title, full_title, file_type,
        committee_item_number, agenda_stage,
    )
    stage_cat, stage_lbl, stage_conf = classify_stage(
        case_type, role_cat, agenda_stage, body_name, role_lbl,
    )

    # Decide the case identity
    if extracted:
        # Prefer an extraction whose case_type matches the matter's own
        # classified case_type. A zoning Resolution that mentions a CDMP
        # application should anchor on its OWN zoning number, not on
        # the CDMP number it happens to reference. Before this rule we
        # anchored on pure confidence, which misrouted companion matters.
        matched = [e for e in extracted if e["case_type"] == case_type]
        if matched:
            top = max(matched, key=lambda e: e["confidence"])
            anchor_method = "type_matched"
        else:
            # No extraction matches the matter's type — fall back to
            # highest confidence, but this is a weaker situation and
            # worth flagging in logs.
            top = max(extracted, key=lambda e: e["confidence"])
            anchor_method = "type_mismatched_fallback"
            log.info(f"    Anchor fallback: matter classified as "
                     f"{case_type} but top extraction is {top['case_type']} "
                     f"({top['number']})")
        app_number = top["number"]
        is_synthetic = False
        link_method = "application_number"
        link_confidence = top["confidence"]
        link_evidence = f"[{anchor_method}] {top['raw_match']}"
    else:
        app_number = synthesize_application_number(file_number, short_title)
        is_synthetic = True
        link_method = "legacy_merge"  # single-matter synth case
        link_confidence = 1.0  # unambiguous — it's a case-of-one
        link_evidence = "no application number found"

    # Find-or-create case
    case_id = _find_or_create_case(
        app_number=app_number,
        case_type=case_type,
        case_type_confidence=ct_conf,
        display_label=short_title[:200],
        is_synthetic=is_synthetic,
    )

    # Link status decision
    if link_confidence >= CASE_LINK_AUTO_CONFIRM_THRESHOLD or is_synthetic:
        link_status = "confirmed"
    else:
        link_status = "candidate"

    # Create / update membership
    _upsert_membership(
        case_id=case_id, matter_id=matter_id,
        role_category=role_cat, role_label=role_lbl,
        role_confidence=role_conf,
        link_status=link_status,
        link_confidence=link_confidence,
        link_method=link_method,
        link_evidence=link_evidence,
    )

    # Update the case's current stage (most recent observation wins)
    _update_case_current_stage(
        case_id=case_id,
        stage_category=stage_cat,
        stage_label=stage_lbl,
    )

    # Denormalize case_id onto appearances of this matter
    _propagate_case_to_appearances(matter_id, case_id, role_lbl)

    # ── Detect relations to OTHER cases mentioned in the same text ──
    # Every secondary number found in search_text is a candidate for a
    # companion / precedent / amends / etc. relation to this case.
    # We only write relations when the OTHER case already exists; if
    # not, the reciprocal ingest will create the link from that side.
    relation_ids = []
    if not is_synthetic and extracted and len(extracted) > 1:
        try:
            relation_ids = record_detected_relations(
                primary_case_id=case_id,
                primary_number=app_number,
                extracted_numbers=extracted,
                source_text=extra_text or search_text,
                evidence_source=f"matter {matter_id} ingest",
            )
            if relation_ids:
                log.info(f"    Recorded {len(relation_ids)} candidate "
                         f"relation(s) for case {app_number}")
        except Exception as e:
            log.warning(f"    Relation detection failed (non-fatal): {e}")

    return {
        "case_id":            case_id,
        "application_number": app_number,
        "is_synthetic":       is_synthetic,
        "case_type":          case_type,
        "role_category":      role_cat,
        "role_label":         role_lbl,
        "stage_category":     stage_cat,
        "stage_label":        stage_lbl,
        "link_status":        link_status,
        "link_confidence":    link_confidence,
        "link_method":        link_method,
        "extracted_numbers":  extracted,
        "relation_ids":       relation_ids,
    }


def _find_or_create_case(
    app_number: str,
    case_type: str,
    case_type_confidence: float,
    display_label: str,
    is_synthetic: bool,
) -> int:
    """Upsert a case by application_number. Returns case_id."""
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, case_type, case_type_confidence FROM cases "
            "WHERE application_number = ?",
            (app_number,),
        ).fetchone()
        if row:
            # Existing case — upgrade case_type only if new classification
            # is more confident.
            case_id = row["id"]
            if case_type_confidence > (row["case_type_confidence"] or 0):
                conn.execute(
                    "UPDATE cases SET case_type=?, case_type_confidence=?, "
                    "updated_at=? WHERE id=?",
                    (case_type, case_type_confidence, now, case_id),
                )
            else:
                conn.execute(
                    "UPDATE cases SET updated_at=? WHERE id=?",
                    (now, case_id),
                )
            return case_id
        # New case
        cur = conn.execute(
            "INSERT INTO cases (application_number, case_type, "
            "case_type_confidence, display_label, is_synthetic, "
            "first_seen_date, last_activity_date, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                app_number, case_type, case_type_confidence,
                display_label, 1 if is_synthetic else 0,
                now[:10], now[:10], now, now,
            ),
        )
        return cur.lastrowid


def _upsert_membership(
    case_id: int,
    matter_id: int,
    role_category: str,
    role_label: str,
    role_confidence: float,
    link_status: str,
    link_confidence: float,
    link_method: str,
    link_evidence: str,
) -> None:
    """Insert or update the (case, matter) membership. matter_id is unique
    across memberships, so one matter belongs to exactly one case."""
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, link_status FROM case_memberships WHERE matter_id=?",
            (matter_id,),
        ).fetchone()
        if row:
            # Don't overwrite a researcher-confirmed/rejected link with
            # an auto-suggested one. Only update role fields.
            existing_status = row["link_status"]
            if existing_status in ("manual", "rejected"):
                conn.execute(
                    "UPDATE case_memberships SET role_category=?, role_label=?, "
                    "role_confidence=?, updated_at=? WHERE id=?",
                    (role_category, role_label, role_confidence, now, row["id"]),
                )
                return
            conn.execute(
                "UPDATE case_memberships SET case_id=?, role_category=?, "
                "role_label=?, role_confidence=?, link_status=?, "
                "link_confidence=?, link_method=?, link_evidence=?, "
                "updated_at=? WHERE id=?",
                (
                    case_id, role_category, role_label, role_confidence,
                    link_status, link_confidence, link_method, link_evidence,
                    now, row["id"],
                ),
            )
            return
        conn.execute(
            "INSERT INTO case_memberships (case_id, matter_id, role_category, "
            "role_label, role_confidence, link_status, link_confidence, "
            "link_method, link_evidence, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                case_id, matter_id, role_category, role_label, role_confidence,
                link_status, link_confidence, link_method, link_evidence,
                now, now,
            ),
        )


def _update_case_current_stage(
    case_id: int,
    stage_category: str,
    stage_label: str,
) -> None:
    """Update the case's current_stage, but only if the new observation
    represents a MORE advanced stage than what's already recorded.

    Rationale: a case containing both a "decision_pending" ordinance and
    a "supporting" supplement is overall in decision_pending — the
    supplement doesn't walk the case backwards. Using a rank lets
    observations arrive in any order without corrupting state."""
    # Rank higher = further along the lifecycle
    STAGE_RANK = {
        "intake":           1,
        "analysis":         2,
        "transmittal":      3,
        "external_review":  4,
        "decision_pending": 5,
        "decided":          6,
        "closed":           7,
        "unknown":          0,
    }
    new_rank = STAGE_RANK.get(stage_category, 0)
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT current_stage_category FROM cases WHERE id=?",
            (case_id,),
        ).fetchone()
        if not row:
            return
        cur_rank = STAGE_RANK.get(row["current_stage_category"] or "unknown", 0)
        if new_rank >= cur_rank:
            conn.execute(
                "UPDATE cases SET current_stage_category=?, "
                "current_stage_label=?, last_activity_date=?, updated_at=? "
                "WHERE id=?",
                (stage_category, stage_label, now[:10], now, case_id),
            )
        else:
            # Still bump last_activity_date since we saw new activity
            conn.execute(
                "UPDATE cases SET last_activity_date=?, updated_at=? "
                "WHERE id=?",
                (now[:10], now, case_id),
            )


def _propagate_case_to_appearances(
    matter_id: int, case_id: int, role_label: str
) -> None:
    """Write case_id + role_label onto every appearance of this matter.
    Denormalized for fast case-view queries."""
    with get_db() as conn:
        conn.execute(
            "UPDATE appearances SET case_id=?, case_role_label=? "
            "WHERE matter_id=?",
            (case_id, role_label, matter_id),
        )


# ══════════════════════════════════════════════════════════════════
# RELATION DETECTOR — find companion / precedent / amends links
# ══════════════════════════════════════════════════════════════════
# Given the raw text of a staff report plus the list of all application
# numbers extracted from it, decide which pairs of numbers form a
# relation and what kind. Scores each candidate pair using phrases
# nearby the mentions (e.g. "concurrent CDMP amendment Application No.
# CDMP20250013" is a strong companion signal).

from schema import (
    RELATION_TYPES, RELATION_IS_SYMMETRIC, RELATION_CONTEXT_PATTERNS,
    RELATION_CONTEXT_WINDOW_CHARS, RELATION_MIN_CONFIDENCE,
)


def detect_relations(
    primary_number: str,
    extracted_numbers: list[dict],
    source_text: str,
) -> list[dict]:
    """For every secondary number found alongside primary_number in the
    same text, decide whether and how it relates to primary_number.

    Returns a list of dicts describing candidate relations to record:
      {
        "other_number":    "CDMP20250013",
        "relation_type":   "companion" | "precedent" | "amends" | ...,
        "confidence":      float in [0, 1],
        "evidence":        "text snippet showing why",
        "detection_method": "context_phrase" | "co_occurrence" | "repeated_mention",
      }

    Only pairs with confidence >= RELATION_MIN_CONFIDENCE are returned.
    All are emitted as candidates regardless of score (per session
    policy: researcher confirms each).
    """
    if not extracted_numbers or not source_text:
        return []

    # Find primary's positions (main + additional)
    primary_entry = next(
        (e for e in extracted_numbers if e["number"] == primary_number), None
    )
    if not primary_entry:
        return []
    primary_positions = [(primary_entry["match_start"], primary_entry["match_end"])]
    primary_positions += primary_entry.get("additional_positions", [])

    results = []
    for other in extracted_numbers:
        if other["number"] == primary_number:
            continue
        other_positions = [(other["match_start"], other["match_end"])]
        other_positions += other.get("additional_positions", [])

        # Score each relation type against this pair. Take the best.
        best_type, best_conf, best_evidence = "related", 0.0, ""
        for rel_type in RELATION_CONTEXT_PATTERNS.keys():
            conf, evidence = _score_relation_pair(
                source_text, primary_positions, other_positions, rel_type,
                other["raw_match"],
            )
            if conf > best_conf:
                best_type, best_conf, best_evidence = rel_type, conf, evidence

        # Baseline: two recognized numbers co-occurring in one document
        # always warrants a "related" candidate at minimum, so the
        # researcher sees them together even if no phrase matched.
        if best_conf < RELATION_MIN_CONFIDENCE:
            # Multiple mentions of both → still worth surfacing
            mentions = len(primary_positions) + len(other_positions)
            if mentions >= 3:
                best_type = "related"
                best_conf = 0.45
                best_evidence = (
                    f"Both numbers appear {mentions} times in the same "
                    "document with no specific relation phrase detected."
                )
            else:
                continue  # too weak — skip

        results.append({
            "other_number":    other["number"],
            "other_case_type": other["case_type"],
            "relation_type":   best_type,
            "confidence":      round(best_conf, 3),
            "evidence":        best_evidence[:500],
            "detection_method": "context_phrase" if best_conf >= 0.6
                                else "co_occurrence",
        })

    return results


def _score_relation_pair(
    source_text: str,
    primary_positions: list,
    other_positions: list,
    rel_type: str,
    other_raw: str,
) -> tuple[float, str]:
    """Score a specific (primary, other) pair under a given relation
    type. Checks for context-signal phrases near either mention of
    'other' and within range of 'primary'. Returns (confidence, evidence)."""
    phrases = RELATION_CONTEXT_PATTERNS.get(rel_type, [])
    if not phrases:
        return 0.0, ""

    low = source_text.lower()
    best_conf = 0.0
    best_evidence = ""

    for (o_start, o_end) in other_positions:
        # Window around the "other" number
        win_start = max(0, o_start - RELATION_CONTEXT_WINDOW_CHARS)
        win_end   = min(len(source_text), o_end + RELATION_CONTEXT_WINDOW_CHARS)
        window_low = low[win_start:win_end]

        for phrase in phrases:
            idx = window_low.find(phrase)
            if idx < 0:
                continue

            # Bonus if 'primary' is ALSO nearby (both numbers and the
            # signal phrase all in one span) — much stronger signal.
            near_primary = any(
                abs(p_start - o_start) < RELATION_CONTEXT_WINDOW_CHARS * 2
                for (p_start, _p_end) in primary_positions
            )

            # Base confidence by relation type — companion signals are
            # the most unambiguous; precedent is the weakest because
            # 'similar to' and 'analogous' get used loosely.
            base = {
                "companion":     0.80,
                "amends":        0.75,
                "superseded_by": 0.75,
                "successor":     0.70,
                "precedent":     0.55,
                "related":       0.50,
            }.get(rel_type, 0.50)

            conf = base
            if near_primary:
                conf = min(0.98, conf + 0.10)
            # Cap 'companion' at 0.85 since per session policy these
            # still go through researcher review regardless — the cap
            # keeps us honest about the uncertainty.
            if rel_type == "companion":
                conf = min(conf, 0.85)

            if conf > best_conf:
                # Build an evidence snippet: the sentence containing the
                # phrase plus the 'other' number.
                snippet_start = max(0, win_start + idx - 60)
                snippet_end   = min(len(source_text),
                                     win_start + idx + len(phrase) + 120)
                snippet = source_text[snippet_start:snippet_end].replace("\n", " ")
                snippet = re.sub(r'\s+', ' ', snippet).strip()
                best_conf = conf
                best_evidence = f"…{snippet}…"

    return best_conf, best_evidence


# ══════════════════════════════════════════════════════════════════
# RELATION WRITER — idempotent, canonical ordering, symmetric
# ══════════════════════════════════════════════════════════════════

def _canonical_pair(case_id_1: int, case_id_2: int) -> tuple[int, int]:
    """Return (smaller, larger) so every logical pair has one canonical
    storage order. Combined with the unique index on (case_a_id,
    case_b_id, relation_type) this makes all relation writes idempotent
    no matter which side of the link initiates them."""
    if case_id_1 == case_id_2:
        raise ValueError("Case cannot relate to itself")
    return (case_id_1, case_id_2) if case_id_1 < case_id_2 else (case_id_2, case_id_1)


def upsert_relation(
    case_a_id: int,
    case_b_id: int,
    relation_type: str,
    confidence: float,
    detection_method: str,
    evidence_snippet: str = "",
    evidence_source: str = "",
    status: str = "candidate",
    direction_from_to: tuple[int, int] | None = None,
) -> int | None:
    """Insert or update a relation between two cases.

    For directional relations (precedent, amends, successor, superseded_by):
    pass direction_from_to=(from_case_id, to_case_id). The table stores
    case_a_id < case_b_id canonically and records direction as 'a_to_b'
    or 'b_to_a' so the UI can orient the arrow correctly.

    Returns the relation id, or None on failure.
    """
    if relation_type not in RELATION_TYPES:
        log.warning(f"Unknown relation_type {relation_type!r}; storing as 'related'")
        relation_type = "related"

    try:
        a_id, b_id = _canonical_pair(case_a_id, case_b_id)
    except ValueError:
        return None

    # Encode direction in canonical frame
    if RELATION_IS_SYMMETRIC.get(relation_type, True):
        direction = None
    elif direction_from_to:
        from_id, to_id = direction_from_to
        if from_id == a_id and to_id == b_id:
            direction = "a_to_b"
        elif from_id == b_id and to_id == a_id:
            direction = "b_to_a"
        else:
            direction = None
    else:
        direction = None

    now = now_iso()
    try:
        with get_db() as conn:
            # Check if relation already exists (unique index would reject
            # a duplicate insert but we want to know whether to update).
            row = conn.execute(
                "SELECT id, confidence, status FROM case_relations "
                "WHERE case_a_id=? AND case_b_id=? AND relation_type=?",
                (a_id, b_id, relation_type),
            ).fetchone()

            if row:
                # Don't overwrite a researcher-confirmed/rejected relation
                # with an auto-detection.
                if row["status"] in ("confirmed", "rejected", "manual") \
                        and status == "candidate":
                    # Just refresh confidence + evidence if new is higher
                    if confidence > (row["confidence"] or 0):
                        conn.execute(
                            "UPDATE case_relations SET confidence=?, "
                            "evidence_snippet=?, updated_at=? WHERE id=?",
                            (confidence, evidence_snippet[:500], now, row["id"]),
                        )
                    return row["id"]
                # Otherwise update everything
                conn.execute(
                    "UPDATE case_relations SET confidence=?, status=?, "
                    "direction=?, detection_method=?, evidence_snippet=?, "
                    "evidence_source=?, updated_at=? WHERE id=?",
                    (confidence, status, direction, detection_method,
                     evidence_snippet[:500], evidence_source[:500],
                     now, row["id"]),
                )
                return row["id"]

            cur = conn.execute(
                "INSERT INTO case_relations (case_a_id, case_b_id, "
                "relation_type, direction, confidence, status, "
                "detection_method, evidence_snippet, evidence_source, "
                "created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (a_id, b_id, relation_type, direction, confidence, status,
                 detection_method, evidence_snippet[:500],
                 evidence_source[:500], now, now),
            )
            return cur.lastrowid
    except Exception as e:
        log.warning(f"upsert_relation failed: {e}")
        return None


def record_detected_relations(
    primary_case_id: int,
    primary_number: str,
    extracted_numbers: list[dict],
    source_text: str,
    evidence_source: str = "",
) -> list[int]:
    """High-level: run the detector and write all findings as candidate
    relations. Returns the list of relation ids written. Only writes
    when the OTHER case actually exists in the DB already — we don't
    auto-create cases from detected relations to avoid spawning
    phantom rows. If the other case is missing, the relation is
    silently skipped; next scrape of the other case will find this one
    and create the link from the other direction."""
    if not extracted_numbers:
        return []
    findings = detect_relations(primary_number, extracted_numbers, source_text)
    ids = []
    for f in findings:
        other_case = get_case_by_application_number(f["other_number"])
        if not other_case:
            log.debug(f"  relation skipped — other case "
                      f"{f['other_number']} not yet in DB")
            continue
        # Determine direction for directional relation types. For
        # precedent, the *older* case is the precedent; we don't always
        # know which is older, so leave direction None and let the UI
        # treat it as pointing from primary → other.
        direction = None
        if not RELATION_IS_SYMMETRIC.get(f["relation_type"], True):
            direction = (primary_case_id, other_case["id"])
        rid = upsert_relation(
            case_a_id=primary_case_id,
            case_b_id=other_case["id"],
            relation_type=f["relation_type"],
            confidence=f["confidence"],
            detection_method=f["detection_method"],
            evidence_snippet=f["evidence"],
            evidence_source=evidence_source,
            status="candidate",
            direction_from_to=direction,
        )
        if rid:
            ids.append(rid)
    return ids


# ══════════════════════════════════════════════════════════════════
# RELATION QUERIES
# ══════════════════════════════════════════════════════════════════

def list_relations_for_case(
    case_id: int, include_rejected: bool = False
) -> list[dict]:
    """Return all relations touching this case, with the OTHER case's
    summary fields joined in. Each row includes an 'other_side' dict
    describing the case on the other end of the relation, and a
    'direction_for_this_case' string that tells the UI how to render
    the arrow from THIS case's point of view.

    Directional relation semantics (as seen from this case):
      'companion' / 'related'  → symmetric, direction_for_this_case='none'
      'precedent'              → 'cites' if this case came later,
                                 'cited_by' if earlier
      'amends'                 → 'amends' if this case is the amendment,
                                 'amended_by' if original
      'successor'              → 'succeeds' or 'succeeded_by'
      'superseded_by'          → 'superseded_by' or 'supersedes'
    """
    with get_db() as conn:
        where_status = "" if include_rejected else "AND r.status != 'rejected'"
        rows = conn.execute(
            f"""
            SELECT r.*,
                   c_a.application_number AS a_app_num,
                   c_a.case_type          AS a_case_type,
                   c_a.display_label      AS a_display_label,
                   c_a.current_stage_label AS a_stage_label,
                   c_b.application_number AS b_app_num,
                   c_b.case_type          AS b_case_type,
                   c_b.display_label      AS b_display_label,
                   c_b.current_stage_label AS b_stage_label
            FROM case_relations r
            JOIN cases c_a ON c_a.id = r.case_a_id
            JOIN cases c_b ON c_b.id = r.case_b_id
            WHERE (r.case_a_id = ? OR r.case_b_id = ?)
              {where_status}
            ORDER BY r.status, r.confidence DESC, r.created_at DESC
            """,
            (case_id, case_id),
        ).fetchall()

    result = []
    for r in rows:
        r = dict(r)
        this_is_a = (r["case_a_id"] == case_id)
        other_side = {
            "case_id":       r["case_b_id"] if this_is_a else r["case_a_id"],
            "application_number": r["b_app_num"] if this_is_a else r["a_app_num"],
            "case_type":     r["b_case_type"] if this_is_a else r["a_case_type"],
            "display_label": r["b_display_label"] if this_is_a else r["a_display_label"],
            "stage_label":   r["b_stage_label"] if this_is_a else r["a_stage_label"],
        }
        result.append({
            "id":               r["id"],
            "relation_type":    r["relation_type"],
            "confidence":       r["confidence"],
            "status":           r["status"],
            "detection_method": r["detection_method"],
            "evidence_snippet": r["evidence_snippet"],
            "evidence_source":  r["evidence_source"],
            "direction_for_this_case": _direction_for_case(
                r["relation_type"], r["direction"], this_is_a
            ),
            "other_side":       other_side,
            "confirmed_by":     r["confirmed_by"],
            "confirmed_at":     r["confirmed_at"],
            "created_at":       r["created_at"],
        })
    return result


def _direction_for_case(relation_type: str, direction: str | None,
                         this_is_a: bool) -> str:
    """Translate the canonical-frame direction into from-this-case wording."""
    if RELATION_IS_SYMMETRIC.get(relation_type, True) or not direction:
        return "none"
    # direction == 'a_to_b' means case_a is the FROM, case_b is the TO
    from_is_this = (direction == "a_to_b" and this_is_a) or \
                   (direction == "b_to_a" and not this_is_a)
    verbs = {
        "precedent":     ("cites",         "cited_by"),
        "amends":        ("amends",        "amended_by"),
        "successor":     ("succeeds",      "succeeded_by"),
        "superseded_by": ("superseded_by", "supersedes"),
    }
    fwd, rev = verbs.get(relation_type, ("related", "related"))
    return fwd if from_is_this else rev


def list_candidate_relations(limit: int = 100) -> list[dict]:
    """Candidate relations awaiting researcher review. Joins both
    endpoints for display."""
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT r.*,
                   c_a.application_number AS a_app_num, c_a.case_type AS a_case_type,
                   c_b.application_number AS b_app_num, c_b.case_type AS b_case_type
            FROM case_relations r
            JOIN cases c_a ON c_a.id = r.case_a_id
            JOIN cases c_b ON c_b.id = r.case_b_id
            WHERE r.status = 'candidate'
            ORDER BY r.confidence DESC, r.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# CROSS-REFERENCE CONTEXT (Session 4 — April 2026)
# ══════════════════════════════════════════════════════════════════
# When analyzing an item, the pipeline should see the summaries of
# OTHER related items so Claude can cross-reference them. There are
# three tiers of relatedness, in order of relevance:
#
#   1. Same-Case members analyzed in earlier appearances
#      (e.g. analyzing 3C today; 3C1 was analyzed last month — same
#      case CDMP20250008). Claude needs these to say things like
#      "this ordinance adopts the amendment that was transmitted to
#      the state on [date]."
#
#   2. Same-Case members in the SAME meeting, but not yet processed
#      in this run (handled by Session 1's sibling_results, already
#      wired).
#
#   3. Companion-Case summaries (CDMP + Zoning for same project).
#      A zoning item's brief should reference its companion CDMP.

def build_crossref_context(
    case_id: int,
    current_matter_id: int | None = None,
    max_chars: int = 4000,
) -> str:
    """Build a text block suitable for injection into an analyzer's
    prior_context describing cross-references to other items. Returns
    empty string if nothing to say.

    Pulls:
      - Most recent AI summary from other matters in the SAME Case
        (excluding current_matter_id).
      - Most recent AI summary from matters in COMPANION Cases
        (relation_type = 'companion' AND status != 'rejected').

    Respects max_chars — truncates oldest first if over budget.
    """
    parts = []

    # 1. Other items in THIS case
    with get_db() as conn:
        same_case_rows = conn.execute(
            """
            SELECT m.file_number, m.short_title,
                   cm.role_label, cm.role_category,
                   a.ai_summary_for_appearance, a.watch_points_for_appearance,
                   a.committee_item_number,
                   mt.meeting_date, mt.body_name
            FROM case_memberships cm
            JOIN matters m ON m.id = cm.matter_id
            LEFT JOIN appearances a ON a.matter_id = m.id
            LEFT JOIN meetings mt   ON mt.id = a.meeting_id
            WHERE cm.case_id = ?
              AND cm.matter_id != ?
              AND cm.link_status != 'rejected'
              AND a.ai_summary_for_appearance IS NOT NULL
              AND LENGTH(a.ai_summary_for_appearance) > 30
            ORDER BY mt.meeting_date DESC, a.updated_at DESC
            """,
            (case_id, current_matter_id or 0),
        ).fetchall()

    for r in same_case_rows:
        r = dict(r)
        block = (
            f"[SAME-CASE ITEM — File# {r['file_number']} · "
            f"{r.get('role_label') or r.get('role_category') or 'related'} · "
            f"{r.get('body_name','')} {r.get('meeting_date','')}"
            f"{' · Item ' + r['committee_item_number'] if r.get('committee_item_number') else ''}]\n"
            f"{r['short_title'] or ''}\n"
            f"{(r['ai_summary_for_appearance'] or '')[:1200]}"
        )
        parts.append(block)

    # 2. Matters in companion cases
    with get_db() as conn:
        comp_rows = conn.execute(
            """
            SELECT DISTINCT c_other.application_number, c_other.case_type,
                   m.file_number, m.short_title,
                   cm.role_label, cm.role_category,
                   a.ai_summary_for_appearance,
                   a.committee_item_number,
                   mt.meeting_date, mt.body_name,
                   r.relation_type, r.status AS rel_status
            FROM case_relations r
            JOIN cases c_other ON c_other.id = CASE
                 WHEN r.case_a_id = ? THEN r.case_b_id
                 ELSE r.case_a_id END
            JOIN case_memberships cm ON cm.case_id = c_other.id
                  AND cm.link_status != 'rejected'
            JOIN matters m ON m.id = cm.matter_id
            LEFT JOIN appearances a ON a.matter_id = m.id
            LEFT JOIN meetings mt   ON mt.id = a.meeting_id
            WHERE (r.case_a_id = ? OR r.case_b_id = ?)
              AND r.status != 'rejected'
              AND a.ai_summary_for_appearance IS NOT NULL
              AND LENGTH(a.ai_summary_for_appearance) > 30
            ORDER BY mt.meeting_date DESC, a.updated_at DESC
            """,
            (case_id, case_id, case_id),
        ).fetchall()

    for r in comp_rows:
        r = dict(r)
        status_note = "" if r["rel_status"] == "confirmed" \
            else f" · relation status: {r['rel_status']}"
        block = (
            f"[COMPANION CASE — {r['application_number']} "
            f"({r['case_type']}) · {r['relation_type']}{status_note}]\n"
            f"File# {r['file_number']} — "
            f"{r.get('role_label') or r.get('role_category') or 'related'} · "
            f"{r.get('body_name','')} {r.get('meeting_date','')}"
            f"{' · Item ' + r['committee_item_number'] if r.get('committee_item_number') else ''}\n"
            f"{r['short_title'] or ''}\n"
            f"{(r['ai_summary_for_appearance'] or '')[:1200]}"
        )
        parts.append(block)

    if not parts:
        return ""

    # Assemble with budget. Parts are in recency order; keep newest,
    # drop oldest when over budget.
    header = (
        "CROSS-REFERENCE CONTEXT — other agenda items that are part of "
        "the same Case or a related companion Case. Your brief should "
        "reference these where relevant (see CROSS-REFERENCE RULE in "
        "system prompt).\n\n"
    )
    out = header
    for block in parts:
        if len(out) + len(block) + 2 > max_chars:
            out += "[…additional related items omitted to fit context window…]\n"
            break
        out += block + "\n\n"
    return out.rstrip()


# ══════════════════════════════════════════════════════════════════
# WORKLOAD — weight computation + researcher load aggregation
# ══════════════════════════════════════════════════════════════════
# No new DB columns needed. Weight is DERIVED from existing signals:
#   - ai_risk_level: HIGH / MEDIUM / LOW / CEREMONIAL (Session 1)
#   - pdf existence: item_pdf_local_path not null → heavier
#   - priority:     free-text "High" / "Urgent" → boost
#   - companion/sibling presence: if this matter belongs to a case
#     with companions, add a burden premium (researcher also has to
#     read the companion's context).
#
# Weight scale: 1 (trivial) to 5 (heavy). Not hours — RELATIVE load.

# Risk-level base weights
_RISK_WEIGHTS = {
    "HIGH":       4,
    "MEDIUM":     3,
    "LOW":        2,
    "CEREMONIAL": 1,
}

def compute_item_weight(appearance: dict, case_has_companions: bool = False) -> int:
    """Estimate the workload weight of a single appearance.

    Inputs:
      appearance — dict from the appearances table (joined with matters
                   is fine; we use only appearance-side fields).
      case_has_companions — if True, +1 for the burden of reading a
                            companion case's materials as well.

    Returns: int in [1, 5].
    """
    # Start from AI risk level if we have it. Otherwise default to MEDIUM.
    risk = (appearance.get("ai_risk_level") or "").upper().strip()
    weight = _RISK_WEIGHTS.get(risk, 3)  # unknown → medium

    # PDF length signal: if we have an item PDF on disk, add 1 for
    # "there's actually stuff to read." (When we don't have a PDF,
    # the item is probably agenda-text-only, lighter load.)
    if appearance.get("item_pdf_local_path") or appearance.get("item_pdf_url"):
        weight += 1

    # Priority override
    priority = (appearance.get("priority") or "").strip().lower()
    if priority in ("urgent", "high", "critical"):
        weight += 1

    # Companion burden
    if case_has_companions:
        weight += 1

    # Clamp to [1, 5]
    return max(1, min(5, weight))


def get_current_workload(username: str) -> dict:
    """Compute a researcher's CURRENT active workload. Active =
    workflow_status in ('Assigned', 'In Progress', 'Needs Revision').
    Archived / Finalized don't count.

    Returns:
      {
        "username": str,
        "active_count": int,
        "total_weight": int,
        "by_status": {"Assigned": n, "In Progress": n, ...},
        "heavy_items": [top 5 by weight],
        "cases_touched": int,
      }
    """
    ACTIVE_STATUSES = ("Assigned", "In Progress", "Needs Revision", "Draft Complete")
    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT a.id, a.file_number, a.committee_item_number,
                   a.workflow_status, a.priority, a.due_date,
                   a.ai_risk_level, a.item_pdf_local_path, a.item_pdf_url,
                   a.case_id, a.case_role_label,
                   m.short_title,
                   mt.meeting_date, mt.body_name
            FROM appearances a
            LEFT JOIN matters m   ON m.id = a.matter_id
            LEFT JOIN meetings mt ON mt.id = a.meeting_id
            WHERE a.assigned_to = ?
              AND a.workflow_status IN ({",".join("?" * len(ACTIVE_STATUSES))})
            ORDER BY mt.meeting_date ASC, a.id
            """,
            (username, *ACTIVE_STATUSES),
        ).fetchall()

    items = [dict(r) for r in rows]

    # Compute case-has-companions map in one query so we don't N+1
    case_ids = {i["case_id"] for i in items if i.get("case_id")}
    companion_cases = set()
    if case_ids:
        placeholders = ",".join("?" * len(case_ids))
        with get_db() as conn:
            comp_rows = conn.execute(
                f"""
                SELECT DISTINCT CASE
                    WHEN case_a_id IN ({placeholders}) THEN case_a_id
                    ELSE case_b_id
                END AS cid
                FROM case_relations
                WHERE (case_a_id IN ({placeholders})
                       OR case_b_id IN ({placeholders}))
                  AND relation_type = 'companion'
                  AND status != 'rejected'
                """,
                (*case_ids, *case_ids, *case_ids),
            ).fetchall()
        companion_cases = {r[0] for r in comp_rows}

    # Score each item
    for i in items:
        i["weight"] = compute_item_weight(
            i, case_has_companions=(i.get("case_id") in companion_cases)
        )

    by_status = {}
    for i in items:
        by_status[i["workflow_status"]] = by_status.get(i["workflow_status"], 0) + 1

    heavy = sorted(items, key=lambda i: (-i["weight"], i.get("due_date") or "9999"))[:5]

    return {
        "username":        username,
        "active_count":    len(items),
        "total_weight":    sum(i["weight"] for i in items),
        "by_status":       by_status,
        "heavy_items":     heavy,
        "cases_touched":   len({i["case_id"] for i in items if i.get("case_id")}),
        "items":           items,
    }


def list_all_researcher_workloads() -> list[dict]:
    """Return workload summaries for every researcher who has at least
    one active assignment. Useful for an "assign to least-loaded"
    picker. Sorted ascending by total_weight."""
    with get_db() as conn:
        people_rows = conn.execute(
            """
            SELECT DISTINCT assigned_to
            FROM appearances
            WHERE assigned_to IS NOT NULL AND assigned_to != ''
              AND workflow_status IN
                  ('Assigned','In Progress','Needs Revision','Draft Complete')
            """
        ).fetchall()
    workloads = []
    for r in people_rows:
        name = r[0]
        if not name:
            continue
        w = get_current_workload(name)
        # Strip per-item details for the summary list — too heavy to ship
        w_summary = {k: v for k, v in w.items() if k != "items"}
        workloads.append(w_summary)
    workloads.sort(key=lambda w: (w["total_weight"], w["active_count"]))
    return workloads


def suggest_assignee(
    exclude: list[str] | None = None,
    prefer_over_weight: int | None = None,
) -> dict | None:
    """Pick the least-loaded researcher as a default assignment
    suggestion. `exclude` skips named usernames (e.g. the reviewer).
    `prefer_over_weight` optionally prefers someone whose TOTAL weight
    is below this threshold even if they're not the absolute lowest
    (for "load balance" over "strict minimum").

    Returns a dict with 'username' and 'total_weight', or None if no
    researchers have any assignments on record (bootstrapping case)."""
    exclude = set(exclude or [])
    loads = [w for w in list_all_researcher_workloads()
             if w["username"] not in exclude]
    if not loads:
        return None
    if prefer_over_weight is not None:
        under = [w for w in loads if w["total_weight"] < prefer_over_weight]
        if under:
            return under[0]
    return loads[0]


# ══════════════════════════════════════════════════════════════════
# CASE-COHERENT ASSIGNMENT CHECKS
# ══════════════════════════════════════════════════════════════════
# Enforce: all items in a Case must share an assignee. Same for
# companion cases (per Session 4 Q2 decision: no domain specialists,
# so force-same is safe).
#
# Policy: if a caller tries to assign appearance X to person Y, but
# another appearance in X's case (or companion case) is already
# assigned to Z (Z != Y), REJECT the assignment unless force=True.
# Returns a detailed conflict report the UI can show.

def check_case_assignment_coherence(
    appearance_id: int, proposed_assignee: str,
) -> dict:
    """Check if assigning appearance_id to proposed_assignee would
    conflict with existing assignments on sibling / companion items.

    Returns:
      {
        "ok":            bool,   # True = no conflict, assignment safe
        "conflicts":     [ {file_number, committee_item_number, assigned_to, reason, scope}, ... ],
        "case_id":       int | None,
        "companion_case_ids": [int],
      }

    scope is 'same_case' or 'companion_case' to distinguish strictness.
    """
    proposed = (proposed_assignee or "").strip()
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, matter_id, case_id FROM appearances WHERE id=?",
            (appearance_id,),
        ).fetchone()
        if not row:
            return {"ok": False, "conflicts": [{"reason": "appearance not found"}],
                    "case_id": None, "companion_case_ids": []}
        case_id = row["case_id"]
        if not case_id:
            # Item not linked to a case — nothing to enforce
            return {"ok": True, "conflicts": [], "case_id": None,
                    "companion_case_ids": []}

        # Same-case conflicts
        same_case = conn.execute(
            """
            SELECT a.id, a.file_number, a.committee_item_number, a.assigned_to,
                   a.workflow_status
            FROM appearances a
            WHERE a.case_id = ?
              AND a.id != ?
              AND a.assigned_to IS NOT NULL
              AND a.assigned_to != ''
              AND a.assigned_to != ?
              AND a.workflow_status NOT IN ('Archived', 'Finalized')
            """,
            (case_id, appearance_id, proposed),
        ).fetchall()

        # Companion-case ids
        comp_rows = conn.execute(
            """
            SELECT DISTINCT CASE
                WHEN case_a_id = ? THEN case_b_id
                ELSE case_a_id
            END AS other_case_id
            FROM case_relations
            WHERE (case_a_id = ? OR case_b_id = ?)
              AND relation_type = 'companion'
              AND status != 'rejected'
            """,
            (case_id, case_id, case_id),
        ).fetchall()
        companion_case_ids = [r["other_case_id"] for r in comp_rows]

        companion_conflicts = []
        if companion_case_ids:
            placeholders = ",".join("?" * len(companion_case_ids))
            companion_conflicts = conn.execute(
                f"""
                SELECT a.id, a.file_number, a.committee_item_number,
                       a.assigned_to, a.workflow_status
                FROM appearances a
                WHERE a.case_id IN ({placeholders})
                  AND a.assigned_to IS NOT NULL
                  AND a.assigned_to != ''
                  AND a.assigned_to != ?
                  AND a.workflow_status NOT IN ('Archived', 'Finalized')
                """,
                (*companion_case_ids, proposed),
            ).fetchall()

    conflicts = []
    for r in same_case:
        conflicts.append({
            "appearance_id":         r["id"],
            "file_number":           r["file_number"],
            "committee_item_number": r["committee_item_number"],
            "assigned_to":           r["assigned_to"],
            "workflow_status":       r["workflow_status"],
            "scope":                 "same_case",
            "reason": (f"Item is in the same Case but currently assigned "
                       f"to {r['assigned_to']!r}. All items in one Case "
                       f"should share an assignee."),
        })
    for r in companion_conflicts:
        conflicts.append({
            "appearance_id":         r["id"],
            "file_number":           r["file_number"],
            "committee_item_number": r["committee_item_number"],
            "assigned_to":           r["assigned_to"],
            "workflow_status":       r["workflow_status"],
            "scope":                 "companion_case",
            "reason": (f"Item is in a companion Case but assigned to "
                       f"{r['assigned_to']!r}. Companion cases "
                       f"(e.g. CDMP + Zoning for same project) should "
                       f"share an assignee."),
        })

    return {
        "ok":                 len(conflicts) == 0,
        "conflicts":          conflicts,
        "case_id":            case_id,
        "companion_case_ids": companion_case_ids,
    }



def confirm_relation(relation_id: int, confirmed_by: str) -> bool:
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE case_relations SET status='confirmed', confirmed_by=?, "
            "confirmed_at=?, updated_at=? WHERE id=?",
            (confirmed_by, now, now, relation_id),
        )
        return cur.rowcount > 0


def reject_relation(relation_id: int, rejected_by: str,
                    reason: str = "") -> bool:
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE case_relations SET status='rejected', confirmed_by=?, "
            "confirmed_at=?, evidence_snippet=?, updated_at=? WHERE id=?",
            (rejected_by, now, f"REJECTED: {reason}"[:500], now, relation_id),
        )
        return cur.rowcount > 0


def create_manual_relation(
    case_a_app_num: str,
    case_b_app_num: str,
    relation_type: str,
    created_by: str,
    evidence: str = "",
    direction_from_case_a: bool = True,
) -> int | None:
    """Researcher-created link. Both cases must already exist."""
    a = get_case_by_application_number(case_a_app_num)
    b = get_case_by_application_number(case_b_app_num)
    if not a or not b:
        return None
    direction = None
    if not RELATION_IS_SYMMETRIC.get(relation_type, True):
        direction = (a["id"], b["id"]) if direction_from_case_a else (b["id"], a["id"])
    return upsert_relation(
        case_a_id=a["id"], case_b_id=b["id"],
        relation_type=relation_type,
        confidence=1.0,
        detection_method="manual",
        evidence_snippet=f"Created by {created_by}. {evidence}"[:500],
        evidence_source="manual",
        status="manual",
        direction_from_to=direction,
    )


# ══════════════════════════════════════════════════════════════════
# CASE EVENTS — write observations to the timeline
# ══════════════════════════════════════════════════════════════════

def record_case_event(
    case_id: int,
    event_date: str,
    event_type: str,
    matter_id: int | None = None,
    appearance_id: int | None = None,
    stage_category: str | None = None,
    stage_label: str | None = None,
    body_name: str | None = None,
    action: str | None = None,
    result: str | None = None,
    source: str = "derived",
    notes: str | None = None,
) -> int | None:
    """Insert a case_event. The unique index on (case_id, event_date,
    body_name, action, matter_id) silently prevents duplicates, so this
    is safe to call idempotently during re-scrapes."""
    now = now_iso()
    sort_key = f"{event_date or '9999-12-31'}_{event_type}"
    try:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO case_events (case_id, matter_id, appearance_id, "
                "event_date, event_type, stage_category, stage_label, "
                "body_name, action, result, source, notes, sort_key, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    case_id, matter_id, appearance_id,
                    event_date, event_type, stage_category, stage_label,
                    body_name, action, result, source, notes, sort_key,
                    now,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        # Unique index violation = duplicate, which is fine.
        log.debug(f"case_event insert skipped (likely duplicate): {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# QUERY HELPERS
# ══════════════════════════════════════════════════════════════════

def get_case_by_application_number(app_number: str) -> dict | None:
    """Lookup a case by its application_number. Returns None if not found."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM cases WHERE application_number=?", (app_number,)
        ).fetchone()
        return dict(row) if row else None


def get_case_by_id(case_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM cases WHERE id=?", (case_id,)
        ).fetchone()
        return dict(row) if row else None


def load_full_case(case_id: int) -> dict | None:
    """Load a case + all its members + all their appearances + timeline
    events, in one shot. Returns structured dict suitable for rendering
    a Case view:

    {
      "case": {...},
      "memberships": [ {matter_id, role, status, ...matter_fields}, ... ],
      "appearances": [ {meeting_date, body_name, role_label, ...}, ... ],
      "events":      [ {event_date, event_type, ...}, ... sorted chronologically ],
    }
    """
    case = get_case_by_id(case_id)
    if not case:
        return None

    with get_db() as conn:
        memberships = [dict(r) for r in conn.execute(
            "SELECT cm.*, m.file_number, m.short_title, m.full_title, "
            "m.file_type, m.sponsor, m.current_status, m.current_stage "
            "FROM case_memberships cm "
            "JOIN matters m ON m.id = cm.matter_id "
            "WHERE cm.case_id=? AND cm.link_status != 'rejected' "
            "ORDER BY cm.role_category, cm.created_at",
            (case_id,),
        ).fetchall()]

        appearances = [dict(r) for r in conn.execute(
            "SELECT a.*, mt.meeting_date, mt.body_name, mt.meeting_type "
            "FROM appearances a "
            "JOIN meetings mt ON mt.id = a.meeting_id "
            "WHERE a.case_id=? "
            "ORDER BY mt.meeting_date DESC, a.committee_item_number",
            (case_id,),
        ).fetchall()]

        events = [dict(r) for r in conn.execute(
            "SELECT * FROM case_events WHERE case_id=? "
            "ORDER BY sort_key, created_at",
            (case_id,),
        ).fetchall()]

    # Include relations to other cases (companion, precedent, etc.)
    # so the Case view can render the "Related Cases" card in one fetch.
    related = list_relations_for_case(case_id, include_rejected=False)

    return {
        "case":          case,
        "memberships":   memberships,
        "appearances":   appearances,
        "events":        events,
        "related_cases": related,
    }


def list_candidate_memberships(limit: int = 100) -> list[dict]:
    """Return candidate (unconfirmed) case memberships for researcher review.
    Joins case + matter info so the UI can render without extra queries."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT cm.*, c.application_number, c.case_type, c.display_label, "
            "m.file_number, m.short_title, m.file_type "
            "FROM case_memberships cm "
            "JOIN cases c   ON c.id = cm.case_id "
            "JOIN matters m ON m.id = cm.matter_id "
            "WHERE cm.link_status='candidate' "
            "ORDER BY cm.link_confidence ASC, cm.created_at DESC "
            "LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def confirm_membership(membership_id: int, confirmed_by: str) -> bool:
    """Mark a candidate membership as confirmed. Returns True on success."""
    now = now_iso()
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE case_memberships SET link_status='confirmed', "
            "confirmed_by=?, confirmed_at=?, updated_at=? WHERE id=?",
            (confirmed_by, now, now, membership_id),
        )
        return cur.rowcount > 0


def reject_membership(membership_id: int, rejected_by: str,
                      reason: str = "") -> bool:
    """Mark a candidate membership as rejected. It stays in the DB so we
    don't re-propose the same link. Also clears the appearance denorm."""
    now = now_iso()
    with get_db() as conn:
        row = conn.execute(
            "SELECT matter_id FROM case_memberships WHERE id=?",
            (membership_id,),
        ).fetchone()
        cur = conn.execute(
            "UPDATE case_memberships SET link_status='rejected', "
            "confirmed_by=?, confirmed_at=?, link_evidence=?, updated_at=? "
            "WHERE id=?",
            (rejected_by, now, f"REJECTED: {reason}"[:500], now, membership_id),
        )
        if row:
            conn.execute(
                "UPDATE appearances SET case_id=NULL, case_role_label=NULL "
                "WHERE matter_id=?",
                (row["matter_id"],),
            )
        return cur.rowcount > 0


def create_manual_membership(
    application_number: str,
    matter_id: int,
    role_label: str,
    created_by: str,
    role_category: str = "review",
) -> int | None:
    """Researcher-created link — always stored as status='manual'.
    Creates the case if it doesn't exist yet."""
    now = now_iso()
    existing = get_case_by_application_number(application_number)
    if existing:
        case_id = existing["id"]
    else:
        # Create a placeholder case so the link can attach
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO cases (application_number, case_type, "
                "case_type_confidence, display_label, is_synthetic, "
                "first_seen_date, last_activity_date, created_at, updated_at) "
                "VALUES (?, 'unknown', 0.5, ?, 0, ?, ?, ?, ?)",
                (
                    application_number, f"Manual case {application_number}",
                    now[:10], now[:10], now, now,
                ),
            )
            case_id = cur.lastrowid

    with get_db() as conn:
        # Kill any existing auto-suggestion for this matter first
        conn.execute(
            "DELETE FROM case_memberships WHERE matter_id=?", (matter_id,)
        )
        cur = conn.execute(
            "INSERT INTO case_memberships (case_id, matter_id, role_category, "
            "role_label, role_confidence, link_status, link_confidence, "
            "link_method, link_evidence, confirmed_by, confirmed_at, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1.0, 'manual', 1.0, 'manual', ?, ?, ?, ?, ?)",
            (
                case_id, matter_id, role_category, role_label,
                f"Manual link by {created_by}", created_by, now,
                now, now,
            ),
        )
        conn.execute(
            "UPDATE appearances SET case_id=?, case_role_label=? "
            "WHERE matter_id=?",
            (case_id, role_label, matter_id),
        )
        return cur.lastrowid


# ══════════════════════════════════════════════════════════════════
# BACKLINK — apply linker to existing historical matters
# ══════════════════════════════════════════════════════════════════

def backlink_all_matters(verbose: bool = True) -> dict:
    """Run the linker over every matter in the DB. Useful after deploying
    the case layer on a DB that already has historical data.

    Returns a summary dict: {matters_processed, cases_created,
    candidates_flagged, errors}.
    """
    from repository import get_matter_by_file_number  # lazy import
    summary = {
        "matters_processed": 0,
        "cases_created_or_matched": set(),
        "candidates_flagged": 0,
        "errors": 0,
    }
    with get_db() as conn:
        matter_rows = conn.execute(
            "SELECT m.id, m.file_number, m.short_title, m.full_title, "
            "m.file_type, m.current_stage "
            "FROM matters m "
            "LEFT JOIN case_memberships cm ON cm.matter_id = m.id "
            "WHERE cm.id IS NULL"  # only matters not yet linked
        ).fetchall()

    for row in matter_rows:
        try:
            result = link_matter_to_case(
                matter_id=row["id"],
                short_title=row["short_title"] or "",
                full_title=row["full_title"] or "",
                file_type=row["file_type"] or "",
                file_number=row["file_number"] or "",
                agenda_stage=row["current_stage"] or "",
            )
            summary["matters_processed"] += 1
            summary["cases_created_or_matched"].add(result["case_id"])
            if result["link_status"] == "candidate":
                summary["candidates_flagged"] += 1
            if verbose and summary["matters_processed"] % 25 == 0:
                log.info(f"  backlink: {summary['matters_processed']} matters processed")
        except Exception as e:
            summary["errors"] += 1
            log.warning(f"backlink failed for matter {row['id']}: {e}")

    summary["cases_created_or_matched"] = len(summary["cases_created_or_matched"])
    return summary
