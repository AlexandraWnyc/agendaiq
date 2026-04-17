"""
planner_sync.py — Power Automate integration for Teams notifications + Planner task sync

Instead of calling Microsoft Graph directly (which requires Azure AD app registration),
we send structured JSON payloads to Power Automate HTTP trigger flows. Power Automate
handles authentication and API calls using the org's credentials.

Two flows are expected:
1. Teams Notification Flow: receives a message payload → posts to a Teams channel
2. Planner Task Flow: receives task data → creates/updates Planner tasks + subtasks

Config keys (in oca_config.json):
  pa_enabled                bool   — master on/off switch
  pa_teams_webhook_url      str    — Power Automate HTTP trigger URL for Teams messages
  pa_planner_webhook_url    str    — Power Automate HTTP trigger URL for Planner tasks
  pa_app_base_url           str    — public URL of AgendaIQ (for deep links in messages)
"""
import json
import logging
import urllib.request
from datetime import datetime

log = logging.getLogger("oca-agent")


def _load_config():
    from notifications import load_config
    return load_config()


def _post_to_flow(url: str, payload: dict, label: str = ""):
    """Send JSON payload to a Power Automate HTTP trigger."""
    if not url:
        return False
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            log.info(f"  PA flow triggered ({label}): status {resp.status}")
            return True
    except Exception as e:
        log.error(f"  PA flow failed ({label}): {e}")
        return False


def _app_link(app_id: int, cfg: dict = None) -> str:
    """Build a deep link to an appearance in AgendaIQ."""
    cfg = cfg or _load_config()
    base = (cfg.get("pa_app_base_url") or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/#item-{app_id}"


# ── Teams Channel Notifications via Power Automate ────────────

def send_teams_message(title: str, message: str, color: str = "#003087",
                        facts: list = None, link_url: str = "", link_label: str = ""):
    """Send a formatted message to Teams via Power Automate.

    The Power Automate flow receives this JSON and posts an Adaptive Card.
    """
    cfg = _load_config()
    if not cfg.get("pa_enabled"):
        return
    url = cfg.get("pa_teams_webhook_url", "").strip()
    if not url:
        return

    payload = {
        "title": title,
        "message": message,
        "color": color,
        "facts": facts or [],
        "link_url": link_url,
        "link_label": link_label or "Open in AgendaIQ",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": "AgendaIQ"
    }
    _post_to_flow(url, payload, f"teams: {title[:50]}")


# ── Planner Task Sync via Power Automate ──────────────────────

def create_planner_task(analyst: str, meeting_date: str, body_name: str,
                         items: list, due_date: str = ""):
    """Create a parent Planner task with subtasks (checklist items).

    Called when items are bulk-assigned to an analyst for a meeting.

    Args:
        analyst: name of the assigned analyst
        meeting_date: ISO date of the meeting
        body_name: committee or BCC meeting name
        items: list of dicts with keys: id, file_number, item_number, title, app_link
        due_date: optional ISO date for the task due date
    """
    cfg = _load_config()
    if not cfg.get("pa_enabled"):
        return
    url = cfg.get("pa_planner_webhook_url", "").strip()
    if not url:
        return

    # Build subtask checklist
    checklist = []
    for it in items:
        item_num = it.get("item_number") or ""
        file_num = it.get("file_number") or ""
        title = it.get("title") or ""
        app_link = it.get("app_link") or _app_link(it.get("id", 0), cfg)

        label = f"{item_num} {file_num}".strip()
        if title:
            label += f" — {title[:80]}"
        if app_link:
            label += f" | {app_link}"

        checklist.append({
            "title": label,
            "isChecked": False,
            "appearance_id": it.get("id"),
            "file_number": file_num,
        })

    payload = {
        "action": "create_task",
        "task_title": f"{analyst} — {body_name} — {meeting_date}",
        "assigned_to": analyst,
        "due_date": due_date or meeting_date,
        "meeting_date": meeting_date,
        "body_name": body_name,
        "status": "Assigned",
        "checklist": checklist,
        "notes": (
            f"AgendaIQ assignment: {len(items)} item(s) for "
            f"{body_name} meeting on {meeting_date}.\n\n"
            f"Analyst: {analyst}\n"
            f"Items: {', '.join(it.get('file_number','') for it in items)}"
        ),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": "AgendaIQ"
    }
    _post_to_flow(url, payload, f"planner-create: {analyst} {meeting_date}")


def update_planner_task_status(analyst: str, meeting_date: str, body_name: str,
                                appearance_id: int, file_number: str,
                                new_status: str, old_status: str = ""):
    """Update a Planner task/subtask status when workflow status changes.

    Power Automate flow will:
    1. Find the parent task by title pattern
    2. Update the subtask (checklist item) containing this file_number
    3. Recompute parent status: if any subtask In Progress → parent In Progress,
       if all Finalized → parent Finalized
    """
    cfg = _load_config()
    if not cfg.get("pa_enabled"):
        return
    url = cfg.get("pa_planner_webhook_url", "").strip()
    if not url:
        return

    # Map AgendaIQ statuses to Planner-compatible states
    planner_status = _map_status(new_status)

    payload = {
        "action": "update_status",
        "task_title_pattern": f"{analyst} — {body_name} — {meeting_date}",
        "appearance_id": appearance_id,
        "file_number": file_number,
        "old_status": old_status,
        "new_status": new_status,
        "planner_status": planner_status,
        "is_complete": new_status in ("Finalized", "Archived"),
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "source": "AgendaIQ"
    }
    _post_to_flow(url, payload, f"planner-status: {file_number} → {new_status}")


def _map_status(agendaiq_status: str) -> str:
    """Map AgendaIQ workflow status to Planner task percentage.

    Planner uses percentComplete: 0=Not started, 50=In progress, 100=Completed.
    """
    return {
        "New":            "Not started",
        "Assigned":       "Not started",
        "In Progress":    "In progress",
        "Draft Complete":  "In progress",
        "In Review":      "In progress",
        "Needs Revision": "In progress",
        "Finalized":      "Completed",
        "Archived":       "Completed",
    }.get(agendaiq_status, "Not started")


# ── Combined notification helper ──────────────────────────────

def notify_assignment(enriched_appearance: dict, items_in_batch: list = None):
    """Send both Teams message and Planner task for a new assignment.

    Called from the workflow update endpoint when an analyst is assigned.
    If items_in_batch is provided, creates a single parent task with all items.
    Otherwise creates a single-item task.
    """
    cfg = _load_config()
    if not cfg.get("pa_enabled"):
        return

    analyst = enriched_appearance.get("assigned_to") or ""
    meeting_date = enriched_appearance.get("meeting_date") or ""
    body_name = enriched_appearance.get("body_name") or ""
    file_num = enriched_appearance.get("file_number") or ""
    title = (enriched_appearance.get("short_title") or
             enriched_appearance.get("appearance_title") or "")[:80]
    due = enriched_appearance.get("due_date") or ""
    app_id = enriched_appearance.get("id", 0)

    # Teams notification
    app_link = _app_link(app_id, cfg)
    send_teams_message(
        title=f"📋 Item Assigned — {file_num}",
        message=f"{file_num} has been assigned to {analyst}",
        color="#003087",
        facts=[
            {"name": "File #", "value": file_num},
            {"name": "Title", "value": title},
            {"name": "Assigned To", "value": analyst},
            {"name": "Meeting", "value": f"{meeting_date} — {body_name}" if body_name else meeting_date},
            {"name": "Due", "value": due or "Not set"},
        ],
        link_url=app_link,
    )

    # Planner task (single item if no batch)
    if not items_in_batch:
        items_in_batch = [{
            "id": app_id,
            "file_number": file_num,
            "item_number": (enriched_appearance.get("committee_item_number") or
                           enriched_appearance.get("bcc_item_number") or
                           enriched_appearance.get("raw_agenda_item_number") or ""),
            "title": title,
        }]

    create_planner_task(
        analyst=analyst,
        meeting_date=meeting_date,
        body_name=body_name,
        items=items_in_batch,
        due_date=due,
    )


def notify_status_change(enriched_appearance: dict, old_status: str, new_status: str):
    """Send Teams message + Planner status update on workflow status change."""
    cfg = _load_config()
    if not cfg.get("pa_enabled"):
        return

    analyst = enriched_appearance.get("assigned_to") or ""
    reviewer = enriched_appearance.get("reviewer") or ""
    meeting_date = enriched_appearance.get("meeting_date") or ""
    body_name = enriched_appearance.get("body_name") or ""
    file_num = enriched_appearance.get("file_number") or ""
    title = (enriched_appearance.get("short_title") or
             enriched_appearance.get("appearance_title") or "")[:80]
    app_id = enriched_appearance.get("id", 0)
    app_link = _app_link(app_id, cfg)

    # Status-specific Teams messages
    messages = {
        "Draft Complete": {
            "title": f"✅ Draft Ready for Review — {file_num}",
            "message": f"{analyst} submitted {file_num} for review by {reviewer}",
            "color": "#00843d",
        },
        "Needs Revision": {
            "title": f"↩ Revision Requested — {file_num}",
            "message": f"{reviewer} sent {file_num} back to {analyst} for revision",
            "color": "#d97706",
        },
        "Finalized": {
            "title": f"✅ Brief Approved — {file_num}",
            "message": f"{reviewer} approved {file_num} — now Finalized",
            "color": "#059669",
        },
        "In Progress": {
            "title": f"🔄 Work Started — {file_num}",
            "message": f"{analyst} started working on {file_num}",
            "color": "#2563eb",
        },
    }

    msg = messages.get(new_status)
    if msg:
        send_teams_message(
            title=msg["title"],
            message=msg["message"],
            color=msg["color"],
            facts=[
                {"name": "File #", "value": file_num},
                {"name": "Title", "value": title},
                {"name": "Status", "value": f"{old_status} → {new_status}"},
                {"name": "Analyst", "value": analyst},
                {"name": "Reviewer", "value": reviewer or "—"},
            ],
            link_url=app_link,
        )

    # Planner status update
    update_planner_task_status(
        analyst=analyst,
        meeting_date=meeting_date,
        body_name=body_name,
        appearance_id=app_id,
        file_number=file_num,
        new_status=new_status,
        old_status=old_status,
    )


# ── Test helper ───────────────────────────────────────────────

def test_power_automate(cfg: dict = None) -> dict:
    """Send test payloads to configured Power Automate flows."""
    cfg = cfg or _load_config()
    results = {}

    if cfg.get("pa_teams_webhook_url"):
        ok = _post_to_flow(
            cfg["pa_teams_webhook_url"],
            {
                "title": "AgendaIQ — Test Connection",
                "message": "Power Automate → Teams integration is working!",
                "color": "#003087",
                "facts": [
                    {"name": "Type", "value": "Teams Notification"},
                    {"name": "Status", "value": "Connected"},
                ],
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "source": "AgendaIQ"
            },
            "test-teams"
        )
        results["teams"] = "OK" if ok else "Failed"
    else:
        results["teams"] = "Not configured"

    if cfg.get("pa_planner_webhook_url"):
        ok = _post_to_flow(
            cfg["pa_planner_webhook_url"],
            {
                "action": "test",
                "task_title": "AgendaIQ — Test Task (safe to delete)",
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "source": "AgendaIQ"
            },
            "test-planner"
        )
        results["planner"] = "OK" if ok else "Failed"
    else:
        results["planner"] = "Not configured"

    return results
