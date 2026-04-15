"""
artifacts.py — First-class artifact tracking for OCA Agenda Intelligence v6.

Every file the system produces or caches (Excel draft, Word draft, final exports,
agenda PDF, item PDF) gets a row in the `artifacts` table.  The UI queries this
table to surface what the user can download for each meeting/item.
"""
import logging
from pathlib import Path
from db import get_db
from utils import now_iso

log = logging.getLogger("oca-agent")


# ── Registration ──────────────────────────────────────────────

def register_artifact(
    artifact_type: str,
    file_path: str | Path,
    meeting_id: int | None = None,
    appearance_id: int | None = None,
    label: str | None = None,
    source_url: str | None = None,
    is_final: bool = False,
    supersede_previous: bool = True,
) -> int:
    """Insert an artifact row.  If `supersede_previous` and another artifact of
    the same type exists for the same meeting/appearance, mark the prior ones
    as not-current (is_current=0) but keep them in the table for history.
    Returns the new artifact id."""
    fp = Path(file_path)
    size = fp.stat().st_size if fp.exists() else None
    now = now_iso()

    with get_db() as conn:
        if supersede_previous:
            if meeting_id is not None and appearance_id is None:
                conn.execute(
                    """UPDATE artifacts SET is_current=0
                       WHERE meeting_id=? AND appearance_id IS NULL
                         AND artifact_type=? AND is_current=1""",
                    (meeting_id, artifact_type),
                )
            elif appearance_id is not None:
                conn.execute(
                    """UPDATE artifacts SET is_current=0
                       WHERE appearance_id=? AND artifact_type=? AND is_current=1""",
                    (appearance_id, artifact_type),
                )

        conn.execute(
            """INSERT INTO artifacts
               (meeting_id, appearance_id, artifact_type, label, file_path,
                source_url, created_at, is_current, is_final, size_bytes)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                meeting_id, appearance_id, artifact_type, label,
                str(fp.resolve()), source_url, now, 1, 1 if is_final else 0, size,
            ),
        )
        row = conn.execute(
            "SELECT id FROM artifacts ORDER BY id DESC LIMIT 1"
        ).fetchone()
        log.info(f"  Artifact registered: {artifact_type} #{row['id']} → {fp.name}")
        return row["id"]


def get_artifact(artifact_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()
        return dict(row) if row else None


def get_current_artifacts_for_meeting(meeting_id: int) -> list[dict]:
    """All current artifacts attached to a meeting (meeting-level + item-level)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM artifacts
               WHERE meeting_id=? AND is_current=1
               ORDER BY is_final DESC, artifact_type ASC, created_at DESC""",
            (meeting_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_current_meeting_level_artifacts(meeting_id: int) -> list[dict]:
    """Only artifacts attached to the meeting itself (not individual items)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM artifacts
               WHERE meeting_id=? AND appearance_id IS NULL AND is_current=1
               ORDER BY is_final DESC, artifact_type ASC""",
            (meeting_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_artifacts_for_appearance(appearance_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM artifacts
               WHERE appearance_id=? AND is_current=1
               ORDER BY created_at DESC""",
            (appearance_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_final_export(meeting_id: int) -> list[dict]:
    """Return current final artifacts for a meeting (excel_final + word_final)."""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM artifacts
               WHERE meeting_id=? AND is_final=1 AND is_current=1
               ORDER BY artifact_type""",
            (meeting_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_artifact(artifact_id: int):
    """Mark an artifact as not current (soft delete)."""
    with get_db() as conn:
        conn.execute(
            "UPDATE artifacts SET is_current=0 WHERE id=?", (artifact_id,)
        )
