"""
schema.py — Table definitions and migration helpers for OCA Agenda Intelligence v6

Tables:
  organizations — tenant isolation: each customer gets their own org
  users         — authenticated users, each scoped to an organization
  matters       — one master record per file number (continuity anchor)
  meetings      — one row per committee meeting occurrence
  appearances   — one row per matter-per-meeting appearance
  appearances_fts — FTS5 virtual table for full-text search
"""

DDL_STATEMENTS = [

    # ── organizations ─────────────────────────────────────────
    # Multi-tenancy: each customer gets their own organization.
    # All data tables carry an org_id FK to isolate data per tenant.
    """
    CREATE TABLE IF NOT EXISTS organizations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        slug            TEXT UNIQUE NOT NULL,
        settings        TEXT,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_slug ON organizations(slug)",

    # ── users ─────────────────────────────────────────────────
    # Authenticated users scoped to an organization.
    # role: admin (full access), manager (review+assign), analyst (research)
    """
    CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id          INTEGER NOT NULL,
        username        TEXT NOT NULL,
        email           TEXT UNIQUE NOT NULL,
        password_hash   TEXT NOT NULL,
        display_name    TEXT,
        role            TEXT NOT NULL DEFAULT 'analyst',
        is_active       INTEGER DEFAULT 1,
        last_login_at   TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (org_id) REFERENCES organizations(id)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email    ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_users_org             ON users(org_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_user ON users(org_id, username)",

    # ── matters ────────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS matters (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        file_number             TEXT UNIQUE NOT NULL,
        short_title             TEXT,
        full_title              TEXT,
        file_type               TEXT,
        sponsor                 TEXT,
        department              TEXT,
        control_body            TEXT,
        current_status          TEXT,
        legislative_notes       TEXT,
        latest_ai_summary_part1 TEXT,
        latest_watch_points     TEXT,
        latest_final_briefing   TEXT,
        research_notes_master   TEXT,
        flags_questions_master  TEXT,
        first_seen_date         TEXT,
        last_seen_date          TEXT,
        current_stage           TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )
    """,

    # ── meetings ───────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS meetings (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        body_name           TEXT NOT NULL,
        meeting_date        TEXT NOT NULL,
        meeting_type        TEXT,
        agenda_status       TEXT,
        agenda_version      TEXT,
        final_agenda_url    TEXT,
        agenda_pdf_url      TEXT,
        meeting_family_id   TEXT,
        previous_version_id INTEGER,
        is_current_version  INTEGER DEFAULT 1,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    )
    """,

    # ── appearances ────────────────────────────────────────────
    """
    CREATE TABLE IF NOT EXISTS appearances (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id                   INTEGER NOT NULL,
        meeting_id                  INTEGER NOT NULL,
        file_number                 TEXT NOT NULL,
        raw_agenda_item_number      TEXT,
        committee_item_number       TEXT,
        bcc_item_number             TEXT,
        agenda_stage                TEXT,
        appearance_title            TEXT,
        appearance_notes            TEXT,
        ai_summary_for_appearance   TEXT,
        watch_points_for_appearance TEXT,
        leg_history_summary         TEXT,
        carried_forward_from_prior  INTEGER DEFAULT 0,
        prior_appearance_id         INTEGER,
        analyst_working_notes       TEXT,
        reviewer_notes              TEXT,
        finalized_brief             TEXT,
        workflow_status             TEXT NOT NULL DEFAULT 'New',
        assigned_to                 TEXT,
        reviewer                    TEXT,
        assigned_date               TEXT,
        due_date                    TEXT,
        completion_date             TEXT,
        requires_research           INTEGER DEFAULT 1,
        priority                    TEXT,
        created_at                  TEXT NOT NULL,
        updated_at                  TEXT NOT NULL,
        FOREIGN KEY (matter_id)          REFERENCES matters(id),
        FOREIGN KEY (meeting_id)         REFERENCES meetings(id),
        FOREIGN KEY (prior_appearance_id) REFERENCES appearances(id)
    )
    """,

    # ── indexes ────────────────────────────────────────────────
    "CREATE INDEX IF NOT EXISTS idx_appearances_matter   ON appearances(matter_id)",
    "CREATE INDEX IF NOT EXISTS idx_appearances_meeting  ON appearances(meeting_id)",
    "CREATE INDEX IF NOT EXISTS idx_appearances_file     ON appearances(file_number)",
    "CREATE INDEX IF NOT EXISTS idx_appearances_status   ON appearances(workflow_status)",
    "CREATE INDEX IF NOT EXISTS idx_matters_file         ON matters(file_number)",
    "CREATE INDEX IF NOT EXISTS idx_meetings_date        ON meetings(meeting_date)",
    "CREATE INDEX IF NOT EXISTS idx_meetings_body        ON meetings(body_name)",

    # ── FTS5 full-text search ──────────────────────────────────
    # Searches across matter fields and appearance notes/summaries
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS appearances_fts USING fts5(
        file_number,
        short_title,
        full_title,
        legislative_notes,
        ai_summary_for_appearance,
        watch_points_for_appearance,
        analyst_working_notes,
        reviewer_notes,
        finalized_brief,
        appearance_title,
        content='appearances',
        content_rowid='id'
    )
    """,

    # FTS triggers to keep index in sync
    """
    CREATE TRIGGER IF NOT EXISTS appearances_fts_insert
    AFTER INSERT ON appearances BEGIN
        INSERT INTO appearances_fts(rowid, file_number, short_title, full_title,
            legislative_notes, ai_summary_for_appearance, watch_points_for_appearance,
            analyst_working_notes, reviewer_notes, finalized_brief, appearance_title)
        SELECT new.id,
            new.file_number,
            m.short_title, m.full_title, m.legislative_notes,
            new.ai_summary_for_appearance, new.watch_points_for_appearance,
            new.analyst_working_notes, new.reviewer_notes, new.finalized_brief,
            new.appearance_title
        FROM matters m WHERE m.id = new.matter_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS appearances_fts_update
    AFTER UPDATE ON appearances BEGIN
        INSERT INTO appearances_fts(appearances_fts, rowid, file_number, short_title, full_title,
            legislative_notes, ai_summary_for_appearance, watch_points_for_appearance,
            analyst_working_notes, reviewer_notes, finalized_brief, appearance_title)
        VALUES('delete', old.id, old.file_number, '', '', '', '', '', '', '', '', '');
        INSERT INTO appearances_fts(rowid, file_number, short_title, full_title,
            legislative_notes, ai_summary_for_appearance, watch_points_for_appearance,
            analyst_working_notes, reviewer_notes, finalized_brief, appearance_title)
        SELECT new.id,
            new.file_number,
            m.short_title, m.full_title, m.legislative_notes,
            new.ai_summary_for_appearance, new.watch_points_for_appearance,
            new.analyst_working_notes, new.reviewer_notes, new.finalized_brief,
            new.appearance_title
        FROM matters m WHERE m.id = new.matter_id;
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS appearances_fts_delete
    AFTER DELETE ON appearances BEGIN
        INSERT INTO appearances_fts(appearances_fts, rowid, file_number, short_title, full_title,
            legislative_notes, ai_summary_for_appearance, watch_points_for_appearance,
            analyst_working_notes, reviewer_notes, finalized_brief, appearance_title)
        VALUES('delete', old.id, old.file_number, '', '', '', '', '', '', '', '', '');
    END
    """,

    # ── workflow_history ───────────────────────────────────────
    # Audit trail: every status change, assignment, note, due-date update
    """
    CREATE TABLE IF NOT EXISTS workflow_history (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        appearance_id INTEGER NOT NULL,
        changed_by    TEXT,
        action        TEXT NOT NULL,
        old_value     TEXT,
        new_value     TEXT,
        note          TEXT,
        changed_at    TEXT NOT NULL,
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wh_appearance ON workflow_history(appearance_id)",
    "CREATE INDEX IF NOT EXISTS idx_wh_changed_at ON workflow_history(changed_at)",
]

# Run migrations to add tables/columns that may not exist in older DBs.
# Each migration is wrapped in try/except at call time so we can add columns
# idempotently — SQLite will error on duplicate ALTER, which is fine.
MIGRATION_STATEMENTS = [
    # Audit trail for workflow
    """CREATE TABLE IF NOT EXISTS workflow_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        appearance_id INTEGER NOT NULL,
        changed_by TEXT,
        action TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        note TEXT,
        changed_at TEXT NOT NULL,
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_wh_appearance ON workflow_history(appearance_id)",

    # ── Artifacts: persistent tracking of every file the system generates
    #    (Excel draft, Word draft, final exports, agenda PDFs, item PDFs)
    """CREATE TABLE IF NOT EXISTS artifacts (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id    INTEGER,
        appearance_id INTEGER,
        artifact_type TEXT NOT NULL,
        label         TEXT,
        file_path     TEXT NOT NULL,
        source_url    TEXT,
        created_at    TEXT NOT NULL,
        is_current    INTEGER DEFAULT 1,
        is_final      INTEGER DEFAULT 0,
        size_bytes    INTEGER,
        FOREIGN KEY (meeting_id)    REFERENCES meetings(id),
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_meeting ON artifacts(meeting_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_app     ON artifacts(appearance_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_type    ON artifacts(artifact_type)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_current ON artifacts(is_current)",

    # ── Meeting-level new fields ─────────────────────────────
    "ALTER TABLE meetings ADD COLUMN agenda_page_url TEXT",
    "ALTER TABLE meetings ADD COLUMN export_status TEXT DEFAULT 'Draft'",
    "ALTER TABLE meetings ADD COLUMN last_exported_at TEXT",
    "ALTER TABLE meetings ADD COLUMN finalized_at TEXT",

    # ── Appearance-level new fields ──────────────────────────
    "ALTER TABLE appearances ADD COLUMN matter_url TEXT",
    "ALTER TABLE appearances ADD COLUMN item_pdf_url TEXT",
    "ALTER TABLE appearances ADD COLUMN item_pdf_local_path TEXT",

    # Notes metadata
    "ALTER TABLE appearances ADD COLUMN analyst_notes_updated_at TEXT",
    "ALTER TABLE appearances ADD COLUMN analyst_notes_updated_by TEXT",
    "ALTER TABLE appearances ADD COLUMN reviewer_notes_updated_at TEXT",
    "ALTER TABLE appearances ADD COLUMN reviewer_notes_updated_by TEXT",
    "ALTER TABLE appearances ADD COLUMN finalized_brief_updated_at TEXT",
    "ALTER TABLE appearances ADD COLUMN finalized_brief_updated_by TEXT",

    # Per-appearance transcript analysis (separate from analyst notes)
    "ALTER TABLE appearances ADD COLUMN transcript_analysis TEXT",
    "ALTER TABLE appearances ADD COLUMN transcript_video_url TEXT",
    "ALTER TABLE appearances ADD COLUMN transcript_updated_at TEXT",

    # ── Matter lifecycle timeline ────────────────────────────
    # Stores parsed legislative history events so we can render the full
    # lifecycle of an item from introduction through committee review to
    # BCC adoption — even for actions that pre-date our scraping.
    """CREATE TABLE IF NOT EXISTS matter_timeline (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        matter_id   INTEGER NOT NULL,
        event_date  TEXT,
        body_name   TEXT,
        action      TEXT,
        result      TEXT,
        source      TEXT DEFAULT 'legistar',
        raw_line    TEXT,
        sort_key    TEXT,
        created_at  TEXT NOT NULL,
        FOREIGN KEY (matter_id) REFERENCES matters(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_mt_matter ON matter_timeline(matter_id)",
    "CREATE INDEX IF NOT EXISTS idx_mt_date   ON matter_timeline(event_date)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_mt_unique ON matter_timeline(matter_id, event_date, body_name, action)",

    # Cache the raw legislative history HTML/text per matter for re-parsing.
    "ALTER TABLE matters ADD COLUMN legislative_history_raw TEXT",
    "ALTER TABLE matters ADD COLUMN lifecycle_refreshed_at TEXT",

    # Agenda item code captured from Legistar legislative history
    # (e.g. "3C", "10A2"). Lets us link committee history rows back to
    # specific agenda item numbers when creating cross-stage stubs.
    "ALTER TABLE matter_timeline ADD COLUMN agenda_item TEXT",

    # ── AI analysis cache / cost tracking ────────────────────
    # analysis_input_hash:  deterministic SHA-256 of (model + system_prompt +
    #                       user_message) so we can short-circuit the API
    #                       call when the same inputs have already been
    #                       analyzed for ANY appearance.
    # analysis_tokens_in/out: tokens reported by the Anthropic response for
    #                       the last successful analyze call on this row.
    # analysis_cached_tokens: prompt-cache read hits (10% price on Haiku).
    # analysis_at:          ISO timestamp of the last successful analyze.
    "ALTER TABLE appearances ADD COLUMN analysis_input_hash TEXT",
    "ALTER TABLE appearances ADD COLUMN analysis_tokens_in  INTEGER",
    "ALTER TABLE appearances ADD COLUMN analysis_tokens_out INTEGER",
    "ALTER TABLE appearances ADD COLUMN analysis_cached_tokens INTEGER",
    "ALTER TABLE appearances ADD COLUMN analysis_at TEXT",
    "CREATE INDEX IF NOT EXISTS idx_app_analysis_hash ON appearances(analysis_input_hash)",

    # ── AI Chat per appearance ──────────────────────────────
    # Each researcher can have a private conversation with the AI about
    # a specific agenda item. Chats are scoped to (appearance_id, user)
    # so they stay private. Users can append selected AI responses to
    # their working notes or Part 1 summary via the UI.
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        appearance_id INTEGER NOT NULL,
        username      TEXT NOT NULL,
        role          TEXT NOT NULL CHECK(role IN ('user','assistant')),
        content       TEXT NOT NULL,
        web_search    INTEGER DEFAULT 0,
        appended_to   TEXT,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chat_app_user ON chat_messages(appearance_id, username)",
    "CREATE INDEX IF NOT EXISTS idx_chat_created  ON chat_messages(created_at)",

    # ── Workflow improvements: separate internal notes, resubmission, revision tracking
    # internal_notes: private scratch pad (never shared in review)
    "ALTER TABLE appearances ADD COLUMN internal_notes TEXT",
    # resubmission_comment: analyst's comment when resubmitting after revision
    "ALTER TABLE appearances ADD COLUMN resubmission_comment TEXT",
    # debrief_snapshot: snapshot of AI summary at submission time for change tracking
    "ALTER TABLE appearances ADD COLUMN debrief_snapshot_on_submit TEXT",
    # analyst_notes_snapshot: snapshot of analyst notes at submission time
    "ALTER TABLE appearances ADD COLUMN analyst_notes_snapshot_on_submit TEXT",

    # ── Notifications: alerts for agenda changes, new items, auto-processing
    """CREATE TABLE IF NOT EXISTS notifications (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        type          TEXT NOT NULL,
        title         TEXT NOT NULL,
        body          TEXT,
        meeting_id    INTEGER,
        appearance_id INTEGER,
        metadata      TEXT,
        is_read       INTEGER DEFAULT 0,
        dismissed     INTEGER DEFAULT 0,
        created_at    TEXT NOT NULL,
        FOREIGN KEY (meeting_id)    REFERENCES meetings(id),
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_notif_read    ON notifications(is_read)",
    "CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_notif_type    ON notifications(type)",

    # ── Agenda monitoring: track last scan fingerprint per meeting
    "ALTER TABLE meetings ADD COLUMN last_scan_at TEXT",
    "ALTER TABLE meetings ADD COLUMN item_count INTEGER",

    # ── AI risk classification: stored from AI analysis output
    "ALTER TABLE appearances ADD COLUMN ai_risk_level TEXT",
    "ALTER TABLE appearances ADD COLUMN ai_risk_reason TEXT",

    # ══════════════════════════════════════════════════════════════
    # CASE LAYER (Session 1 — April 2026)
    # ══════════════════════════════════════════════════════════════
    # A "Case" is the lifecycle unit above a matter. One Case can span
    # multiple agenda items (matters) across multiple meetings. Example:
    # CDMP20250013 spawns 3C (ordinance), 3C1 (transmittal resolution),
    # 3C Supplement (staff analysis), all members of one Case.
    #
    # Identity: application_number is the natural key. When we cannot
    # extract an application number from an item, we synthesize one
    # (SYNTHETIC-<hash>) so every matter has exactly one case and the
    # case-view code never has to special-case "no case".
    # ══════════════════════════════════════════════════════════════

    """CREATE TABLE IF NOT EXISTS cases (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        application_number      TEXT UNIQUE NOT NULL,
        case_type               TEXT NOT NULL,
        case_type_confidence    REAL DEFAULT 1.0,
        display_label           TEXT,
        subject_summary         TEXT,
        current_stage_category  TEXT,
        current_stage_label     TEXT,
        current_status          TEXT,
        first_seen_date         TEXT,
        last_activity_date      TEXT,
        is_synthetic            INTEGER DEFAULT 0,
        notes                   TEXT,
        created_at              TEXT NOT NULL,
        updated_at              TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cases_app_num  ON cases(application_number)",
    "CREATE INDEX IF NOT EXISTS idx_cases_type     ON cases(case_type)",
    "CREATE INDEX IF NOT EXISTS idx_cases_activity ON cases(last_activity_date)",

    # case_memberships — matter ↔ case, with role + confidence.
    # One matter belongs to exactly one case (enforced by UNIQUE on matter_id).
    # One case has many matters.
    """CREATE TABLE IF NOT EXISTS case_memberships (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id           INTEGER NOT NULL,
        matter_id         INTEGER NOT NULL UNIQUE,
        role_category     TEXT,
        role_label        TEXT,
        role_confidence   REAL DEFAULT 1.0,
        link_status       TEXT NOT NULL DEFAULT 'confirmed',
        link_confidence   REAL DEFAULT 1.0,
        link_method       TEXT,
        link_evidence     TEXT,
        confirmed_by      TEXT,
        confirmed_at      TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        FOREIGN KEY (case_id)   REFERENCES cases(id),
        FOREIGN KEY (matter_id) REFERENCES matters(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_cm_case    ON case_memberships(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_cm_matter  ON case_memberships(matter_id)",
    "CREATE INDEX IF NOT EXISTS idx_cm_status  ON case_memberships(link_status)",

    # case_events — the timeline. Populated from agenda appearances,
    # legislative history events, and manual researcher entries. Distinct
    # from matter_timeline (which is per-matter leg-history-only) in that
    # these are case-level and can aggregate across matters.
    """CREATE TABLE IF NOT EXISTS case_events (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id           INTEGER NOT NULL,
        matter_id         INTEGER,
        appearance_id     INTEGER,
        event_date        TEXT NOT NULL,
        event_type        TEXT NOT NULL,
        stage_category    TEXT,
        stage_label       TEXT,
        body_name         TEXT,
        action            TEXT,
        result            TEXT,
        source            TEXT DEFAULT 'derived',
        notes             TEXT,
        sort_key          TEXT,
        created_at        TEXT NOT NULL,
        FOREIGN KEY (case_id)       REFERENCES cases(id),
        FOREIGN KEY (matter_id)     REFERENCES matters(id),
        FOREIGN KEY (appearance_id) REFERENCES appearances(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ce_case   ON case_events(case_id)",
    "CREATE INDEX IF NOT EXISTS idx_ce_date   ON case_events(event_date)",
    "CREATE INDEX IF NOT EXISTS idx_ce_stage  ON case_events(stage_category)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_ce_dedup ON case_events(case_id, event_date, body_name, action, matter_id)",

    # Denormalized pointer on appearances for fast case lookup without
    # always going through case_memberships. Kept in sync by the linker.
    "ALTER TABLE appearances ADD COLUMN case_id INTEGER",
    "ALTER TABLE appearances ADD COLUMN case_role_label TEXT",
    "CREATE INDEX IF NOT EXISTS idx_app_case ON appearances(case_id)",

    # ══════════════════════════════════════════════════════════════
    # CASE RELATIONS (Session 2 — April 2026)
    # ══════════════════════════════════════════════════════════════
    # Cases can be linked to other cases. Most important example: a
    # CDMP amendment (CDMP20250013) and its companion zoning application
    # (Z2025000130) are two legally distinct Cases for the same physical
    # project. Neither should subsume the other, but a researcher looking
    # at one needs to see the other.
    #
    # Canonical ordering: for each logical relation, we store exactly ONE
    # row with (case_a_id < case_b_id). The unique index on (case_a_id,
    # case_b_id, relation_type) ensures re-running the scraper from either
    # direction doesn't create duplicates.
    # ══════════════════════════════════════════════════════════════

    """CREATE TABLE IF NOT EXISTS case_relations (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        case_a_id         INTEGER NOT NULL,
        case_b_id         INTEGER NOT NULL,
        relation_type     TEXT NOT NULL,
        direction         TEXT,
        confidence        REAL DEFAULT 0.5,
        status            TEXT NOT NULL DEFAULT 'candidate',
        detection_method  TEXT,
        evidence_snippet  TEXT,
        evidence_source   TEXT,
        confirmed_by      TEXT,
        confirmed_at      TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        FOREIGN KEY (case_a_id) REFERENCES cases(id),
        FOREIGN KEY (case_b_id) REFERENCES cases(id),
        CHECK (case_a_id < case_b_id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_cr_unique "
    "ON case_relations(case_a_id, case_b_id, relation_type)",
    "CREATE INDEX IF NOT EXISTS idx_cr_a      ON case_relations(case_a_id)",
    "CREATE INDEX IF NOT EXISTS idx_cr_b      ON case_relations(case_b_id)",
    "CREATE INDEX IF NOT EXISTS idx_cr_status ON case_relations(status)",
    "CREATE INDEX IF NOT EXISTS idx_cr_type   ON case_relations(relation_type)",

    # ── Delta tracking: cached cross-appearance diffs ──────────
    # JSON blob with structured change detection results from Claude.
    # Populated by delta.py when comparing consecutive appearances
    # of the same matter across different meetings.
    "ALTER TABLE appearances ADD COLUMN delta_from_prior TEXT",

    # ── API Usage Tracking ──────────────────────────────────────
    # Records every Anthropic API call for cost tracking and rate limiting.
    """CREATE TABLE IF NOT EXISTS api_usage (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id          INTEGER NOT NULL,
        call_type       TEXT NOT NULL,
        model           TEXT,
        tokens_in       INTEGER DEFAULT 0,
        tokens_out      INTEGER DEFAULT 0,
        cached_tokens   INTEGER DEFAULT 0,
        cost_estimate   REAL DEFAULT 0,
        appearance_id   INTEGER,
        meeting_id      INTEGER,
        call_date       TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        FOREIGN KEY (org_id) REFERENCES organizations(id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_usage_org    ON api_usage(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_usage_date   ON api_usage(call_date)",
    "CREATE INDEX IF NOT EXISTS idx_usage_type   ON api_usage(org_id, call_type)",

    # ══════════════════════════════════════════════════════════════
    # MULTI-TENANCY (P0 — April 2026)
    # ══════════════════════════════════════════════════════════════
    # Add org_id to all data tables for per-organization data isolation.
    # DEFAULT 1 seeds existing Miami-Dade data under org_id=1.
    # The organizations + users tables are in DDL_STATEMENTS (created on
    # fresh DBs); these migrations ensure existing DBs get them too.
    # ══════════════════════════════════════════════════════════════

    # Create tables if missing (existing DBs may not have them yet)
    """CREATE TABLE IF NOT EXISTS organizations (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        name            TEXT NOT NULL,
        slug            TEXT UNIQUE NOT NULL,
        settings        TEXT,
        is_active       INTEGER DEFAULT 1,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_orgs_slug ON organizations(slug)",

    """CREATE TABLE IF NOT EXISTS users (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id          INTEGER NOT NULL,
        username        TEXT NOT NULL,
        email           TEXT UNIQUE NOT NULL,
        password_hash   TEXT NOT NULL,
        display_name    TEXT,
        role            TEXT NOT NULL DEFAULT 'analyst',
        is_active       INTEGER DEFAULT 1,
        last_login_at   TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        FOREIGN KEY (org_id) REFERENCES organizations(id)
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email    ON users(email)",
    "CREATE INDEX IF NOT EXISTS idx_users_org             ON users(org_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_org_user ON users(org_id, username)",

    # ── org_id on all data tables ────────────────────────────
    # DEFAULT 1 = Miami-Dade (the seed org). All existing rows auto-assign.
    "ALTER TABLE matters          ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE meetings         ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE appearances      ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE workflow_history  ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE artifacts         ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE chat_messages     ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE matter_timeline   ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE notifications     ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE cases             ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE case_memberships  ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",
    "ALTER TABLE case_relations    ADD COLUMN org_id INTEGER NOT NULL DEFAULT 1",

    # Indexes for org-scoped queries (most common access patterns)
    "CREATE INDEX IF NOT EXISTS idx_matters_org      ON matters(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_meetings_org     ON meetings(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_appearances_org  ON appearances(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_cases_org        ON cases(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_notif_org        ON notifications(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_wh_org           ON workflow_history(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_chat_org         ON chat_messages(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_artifacts_org    ON artifacts(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_mt_org           ON matter_timeline(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_cm_org           ON case_memberships(org_id)",
    "CREATE INDEX IF NOT EXISTS idx_cr_org           ON case_relations(org_id)",
]

# Meeting package status values
MEETING_PACKAGE_STATUSES = ["Draft", "In Progress", "Final Ready", "Final Generated"]

# Artifact types
ARTIFACT_TYPES = [
    "excel_draft", "word_draft",
    "excel_final", "word_final",
    "agenda_pdf", "item_pdf",
]

WORKFLOW_STATUSES = [
    "New", "Assigned", "In Progress", "Draft Complete",
    "In Review", "Needs Revision", "Finalized", "Archived"
]

STAGE_ORDER = [
    "committee", "bcc", "preliminary", "official", "supplement", "other"
]


def stage_rank(stage: str) -> int:
    """Return numeric rank for stage comparison (higher = more advanced)."""
    s = (stage or "").lower()
    for i, name in enumerate(STAGE_ORDER):
        if name in s:
            return i
    return -1


# ══════════════════════════════════════════════════════════════════
# CASE LAYER VOCABULARIES
# ══════════════════════════════════════════════════════════════════

# Case types — what kind of thing this Case is. Extensible.
CASE_TYPES = [
    "cdmp",          # Comprehensive Development Master Plan
    "zoning",        # Zoning hearing, district boundary, etc.
    "contract",      # Procurement / contract award, amendment, extension
    "ordinance",     # Stand-alone ordinance (non-CDMP, non-zoning)
    "resolution",    # Stand-alone resolution
    "appointment",   # Board / committee appointment
    "ceremonial",    # Proclamation, recognition, commendation
    "report",        # Report or informational item
    "other",         # Doesn't fit — safe default
    "unknown",       # Classifier couldn't decide
]

# Role categories — what role does THIS matter play in its case?
# Structured so queries can aggregate across case types
# ("show me all pending decisions regardless of case type").
ROLE_CATEGORIES = [
    "initiation",     # starts the case (application, RFP issue)
    "review",         # intermediate review stage
    "decision",       # the action vote — ordinance adoption, contract award
    "transmittal",    # sends to another body (CDMP → state, etc.)
    "supporting",     # supplement, analysis, exhibit — no standalone action
    "administrative", # scrivener, corrections, housekeeping
    "unknown",
]

# Stage categories — where in the lifecycle IS the case right now?
# Similar structure: generic category + free-text case-type-specific label.
STAGE_CATEGORIES = [
    "intake",           # application filed / solicitation issued
    "analysis",         # staff review, PAB review, committee review
    "transmittal",      # sent to external body (state, county, etc.)
    "external_review",  # external body has it (state, FDOT, etc.)
    "decision_pending", # scheduled for final action
    "decided",          # voted on (adopted, awarded, denied)
    "closed",           # case fully closed out
    "unknown",
]

# Membership link status — controls whether the link shows as real
# or as a candidate awaiting researcher confirmation.
LINK_STATUSES = [
    "confirmed",   # auto-linked with high confidence OR researcher-confirmed
    "candidate",   # auto-linked with low confidence, needs review
    "rejected",    # researcher rejected the candidate — don't re-propose
    "manual",      # researcher created this link manually
]

# Method used to create the link — for audit trail.
LINK_METHODS = [
    "application_number",  # shared application number (strongest signal)
    "title_similarity",    # fuzzy title match (weaker)
    "file_number_series",  # related file numbers from same ordinance package
    "manual",              # researcher created
    "legacy_merge",        # back-fill from historical data
]

# Confidence threshold: membership links AT OR ABOVE this score are auto-
# confirmed; below this are marked candidate and queued for review.
CASE_LINK_AUTO_CONFIRM_THRESHOLD = 0.85


# ══════════════════════════════════════════════════════════════════
# CASE RELATION VOCABULARIES (Session 2)
# ══════════════════════════════════════════════════════════════════

# Relation types between two cases. "direction" semantics below are
# described from case_a → case_b where case_a_id < case_b_id (the
# canonical storage order). For symmetric relations, direction is None.
RELATION_TYPES = [
    "companion",      # concurrent applications for the same project
                      # (e.g. CDMP amendment + zoning application).
                      # SYMMETRIC.
    "precedent",      # case_a cites case_b as prior precedent.
                      # DIRECTIONAL: older → newer.
    "amends",         # case_a amends / modifies case_b.
                      # DIRECTIONAL: amendment → original.
    "successor",      # case_a is a re-filing / continuation of case_b.
                      # DIRECTIONAL: newer → older.
    "superseded_by",  # case_a has been superseded by case_b.
                      # DIRECTIONAL: superseded → superseder.
    "related",        # catch-all. Human-asserted or ambiguous.
                      # SYMMETRIC.
]

# Per-relation-type: is this relation symmetric (both sides equivalent)
# or directional (one side plays a distinct role)? Drives UI wording.
RELATION_IS_SYMMETRIC = {
    "companion":     True,
    "precedent":     False,
    "amends":        False,
    "successor":     False,
    "superseded_by": False,
    "related":       True,
}

# Relation status:
#   candidate — auto-detected, awaiting researcher review
#   confirmed — reviewer-accepted or manually created with high trust
#   rejected  — reviewer-rejected; kept in DB so we don't re-propose
#   manual    — researcher-created (implicitly confirmed)
RELATION_STATUSES = ["candidate", "confirmed", "rejected", "manual"]


# Context-signal patterns for companion detection. When one of these
# phrases appears within the CONTEXT_WINDOW_CHARS around a secondary
# application number, the companion detector boosts the confidence of
# classifying that pair as a companion relationship.
RELATION_CONTEXT_PATTERNS = {
    "companion": [
        "concurrent",
        "concurrently",
        "companion",
        "tied to",
        "subject to approval of",
        "subject to the approval of",
        "processed concurrently with",
        "processed with",
        "filed concurrently",
        "heard concurrently",
        "on the same day",
        "same day as",
        "contingent on approval of",
        "contingent upon",
        "accompanied by",
        "accompanies",
    ],
    "precedent": [
        "prior approved",
        "previously approved",
        "similar to",
        "analogous to",
        "as in",
        "consistent with prior",
        "following the precedent",
        "prior cdmp",
        "prior ordinance",
    ],
    "amends": [
        "amending",
        "amend",
        "modifies",
        "modifying",
        "amendment to",
        "amends application",
    ],
    "successor": [
        "re-filing of",
        "re-filed",
        "continuation of",
        "continues",
        "previously filed as",
        "replaces application",
    ],
    "superseded_by": [
        "superseded by",
        "replaced by",
        "no longer in effect",
        "withdrawn in favor of",
    ],
}

# How many characters before and after an extracted number to scan
# for context-signal phrases.
RELATION_CONTEXT_WINDOW_CHARS = 250

# Minimum score to record a companion candidate at all. Below this, the
# co-occurrence is treated as a passing mention rather than a real link.
RELATION_MIN_CONFIDENCE = 0.40
