"""
schema.py — Table definitions and migration helpers for OCA Agenda Intelligence v6

Tables:
  matters     — one master record per file number (continuity anchor)
  meetings    — one row per committee meeting occurrence
  appearances — one row per matter-per-meeting appearance
  appearances_fts — FTS5 virtual table for full-text search
"""

DDL_STATEMENTS = [

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
    "In Review", "Finalized", "Archived"
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
