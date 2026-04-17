"""
notifications.py — Email + Webhook reminder system for OCA Agenda Intelligence v6

Checks for overdue and due-soon appearances hourly.
Configured via oca_config.json in the project directory.

Config keys:
  email_enabled       bool   — master on/off switch for email
  smtp_host           str    — e.g. "smtp.gmail.com"
  smtp_port           int    — e.g. 587
  smtp_user           str    — sender address
  smtp_password       str    — sender password (use app password for Gmail)
  smtp_use_tls        bool   — true for most providers
  notify_recipients   list   — email addresses to notify
  reminder_days       int    — how many days ahead to warn (default 7)
  team_members        list   — [{name, email}] for assignment UI
  webhook_enabled     bool   — master on/off switch for Teams/Slack webhook
  webhook_url         str    — incoming webhook URL (Teams or Slack)
"""
import json, smtplib, logging, threading, time
import urllib.request
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from datetime import datetime

log = logging.getLogger("oca-agent")

from paths import CONFIG_PATH  # honors DATA_DIR in cloud, project dir locally

DEFAULT_CONFIG = {
    "email_enabled": False,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_use_tls": True,
    "notify_recipients": [],
    "reminder_days": 7,
    "team_members": [],
    "webhook_enabled": False,
    "webhook_url": "",
    "pa_enabled": False,
    "pa_teams_webhook_url": "",
    "pa_planner_webhook_url": "",
    "pa_app_base_url": "",
}


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text())
            return {**DEFAULT_CONFIG, **saved}
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))


def _send_email(cfg: dict, subject: str, body_html: str):
    if not cfg.get("email_enabled"):
        return
    recipients = cfg.get("notify_recipients", [])
    if not recipients or not cfg.get("smtp_user"):
        return
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = cfg["smtp_user"]
        msg["To"]      = ", ".join(recipients)
        msg.attach(MIMEText(body_html, "html"))

        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as s:
            if cfg.get("smtp_use_tls"):
                s.starttls()
            s.login(cfg["smtp_user"], cfg["smtp_password"])
            s.sendmail(cfg["smtp_user"], recipients, msg.as_string())
        log.info(f"  Email sent: {subject}")
    except Exception as e:
        log.error(f"  Email failed: {e}")


def _detect_webhook_type(url: str) -> str:
    """Auto-detect webhook type from URL."""
    if not url:
        return "unknown"
    url_lower = url.lower()
    if "office.com" in url_lower or "microsoft.com" in url_lower or "webhook.office" in url_lower:
        return "teams"
    if "hooks.slack.com" in url_lower or "slack" in url_lower:
        return "slack"
    # Default to Slack-compatible format (works with most webhooks)
    return "slack"


def _send_webhook(cfg: dict, title: str, message: str, color: str = "#003087",
                   facts: list = None):
    """Send notification to Teams or Slack incoming webhook.

    Args:
        cfg: config dict with webhook_url and webhook_enabled
        title: notification title/header
        message: notification body text
        color: hex color for the card/attachment accent
        facts: optional list of {"name": ..., "value": ...} for structured data
    """
    if not cfg.get("webhook_enabled"):
        return
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return

    hook_type = _detect_webhook_type(url)

    try:
        if hook_type == "teams":
            # Microsoft Teams Adaptive Card via Incoming Webhook
            # Teams deprecated Office 365 connectors; use Adaptive Card format
            facts_blocks = []
            if facts:
                for f in facts:
                    facts_blocks.append({
                        "type": "ColumnSet",
                        "columns": [
                            {"type": "Column", "width": "auto", "items": [
                                {"type": "TextBlock", "text": f["name"] + ":", "weight": "Bolder", "size": "Small"}
                            ]},
                            {"type": "Column", "width": "stretch", "items": [
                                {"type": "TextBlock", "text": f["value"], "size": "Small", "wrap": True}
                            ]}
                        ]
                    })

            payload = {
                "type": "message",
                "attachments": [{
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": [
                            {
                                "type": "Container",
                                "style": "emphasis",
                                "items": [
                                    {"type": "TextBlock", "text": title, "weight": "Bolder",
                                     "size": "Medium", "color": "Accent"},
                                ]
                            },
                            {"type": "TextBlock", "text": message, "wrap": True, "size": "Small"},
                            *facts_blocks,
                            {"type": "TextBlock", "text": "— AgendaIQ · OCA · Miami-Dade County",
                             "size": "Small", "isSubtle": True, "spacing": "Medium"}
                        ]
                    }
                }]
            }
        else:
            # Slack-compatible payload
            fields = []
            if facts:
                for f in facts:
                    fields.append({"title": f["name"], "value": f["value"], "short": True})

            payload = {
                "text": f"*{title}*",
                "attachments": [{
                    "color": color,
                    "text": message,
                    "fields": fields,
                    "footer": "AgendaIQ · OCA · Miami-Dade County",
                }]
            }

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                      headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info(f"  Webhook sent ({hook_type}): {title} — status {resp.status}")
    except Exception as e:
        log.error(f"  Webhook failed ({hook_type}): {e}")


def test_webhook(cfg: dict) -> dict:
    """Send a test message to the configured webhook. Returns {ok, error}."""
    url = cfg.get("webhook_url", "").strip()
    if not url:
        return {"ok": False, "error": "No webhook URL configured"}
    hook_type = _detect_webhook_type(url)
    try:
        _send_webhook(
            {**cfg, "webhook_enabled": True},
            "AgendaIQ — Test Notification",
            "This is a test message from AgendaIQ. Webhook notifications are working!",
            color="#003087",
            facts=[
                {"name": "Type", "value": f"Detected as {hook_type.title()}"},
                {"name": "Status", "value": "Connected successfully"},
            ]
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_member_email(name: str) -> str | None:
    """Look up a team member's email by their display name in config."""
    cfg = load_config()
    for m in cfg.get("team_members", []):
        if m.get("name") == name:
            return m.get("email") or None
    return None


def send_assignment_notification(appearance: dict):
    """Email the assigned team member when an item is assigned to them."""
    cfg = load_config()
    if not cfg.get("email_enabled"):
        return
    assignee = appearance.get("assigned_to") or ""
    if not assignee:
        return
    email = get_member_email(assignee)
    if not email:
        log.info(f"  No email for '{assignee}' — assignment notification skipped")
        return

    file_num  = appearance.get("file_number", "")
    title     = (appearance.get("short_title") or appearance.get("appearance_title") or "")[:80]
    due       = appearance.get("due_date") or "Not set"
    meeting   = appearance.get("meeting_date") or ""
    body_name = appearance.get("body_name") or ""

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#003087;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>&#x1F4CB; AgendaIQ &#x2014; Item Assigned to You</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>A new agenda item requires your attention</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:20px;'>
        <p style='margin:0 0 16px;'>Hi <strong>{assignee}</strong>, the following item has been assigned to you:</p>
        <table style='width:100%;border-collapse:collapse;margin-bottom:16px;background:#f8fafc;'>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;width:130px;border-bottom:1px solid #e2e8f0;'>File #</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#003087;'>{file_num}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Title</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{title}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Meeting</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{meeting}{' &#x2014; ' + body_name if body_name else ''}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;'>Due Date</td>
              <td style='padding:9px 14px;'>{due}</td></tr>
        </table>
        <p style='color:#64748b;font-size:12px;margin:0;'>&#x2014; AgendaIQ &middot; Office of the Commission Auditor &middot; Miami-Dade County</p>
      </div>
    </div>"""

    cfg_to = dict(cfg)
    cfg_to["notify_recipients"] = [email]
    _send_email(cfg_to, f"[AgendaIQ] Assigned to You: {file_num} — {title[:45]}", html)

    # Webhook notification
    _send_webhook(cfg, f"📋 Item Assigned — {file_num}",
        f"*{file_num}* has been assigned to *{assignee}*\n{title}",
        color="#003087",
        facts=[
            {"name": "File #", "value": file_num},
            {"name": "Assigned To", "value": assignee},
            {"name": "Meeting", "value": f"{meeting} — {body_name}" if body_name else meeting},
            {"name": "Due", "value": due},
        ])


def send_draft_complete_notification(appearance: dict):
    """Email the reviewer when an item reaches Draft Complete status."""
    cfg = load_config()
    if not cfg.get("email_enabled"):
        return
    reviewer = appearance.get("reviewer") or ""
    if not reviewer:
        log.info("  Draft Complete notification skipped — no reviewer set on this item")
        return
    email = get_member_email(reviewer)
    if not email:
        log.info(f"  No email for reviewer '{reviewer}' — notification skipped")
        return

    file_num    = appearance.get("file_number", "")
    title       = (appearance.get("short_title") or appearance.get("appearance_title") or "")[:80]
    assigned_to = appearance.get("assigned_to") or "Unknown"
    due         = appearance.get("due_date") or "Not set"

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#00843d;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>&#x2705; AgendaIQ &#x2014; Draft Brief Ready for Review</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>A brief is ready for your review and approval</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:20px;'>
        <p style='margin:0 0 16px;'>Hi <strong>{reviewer}</strong>, a draft brief is ready for your review:</p>
        <table style='width:100%;border-collapse:collapse;margin-bottom:16px;background:#f8fafc;'>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;width:130px;border-bottom:1px solid #e2e8f0;'>File #</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#003087;'>{file_num}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Title</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{title}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Analyst</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{assigned_to}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;'>Due Date</td>
              <td style='padding:9px 14px;'>{due}</td></tr>
        </table>
        <p style='color:#64748b;font-size:12px;margin:0;'>&#x2014; AgendaIQ &middot; Office of the Commission Auditor &middot; Miami-Dade County</p>
      </div>
    </div>"""

    cfg_to = dict(cfg)
    cfg_to["notify_recipients"] = [email]
    _send_email(cfg_to, f"[AgendaIQ] Review Needed: {file_num} — {title[:45]}", html)

    # Webhook notification
    _send_webhook(cfg, f"✅ Draft Ready for Review — {file_num}",
        f"*{assigned_to}* submitted *{file_num}* for review by *{reviewer}*\n{title}",
        color="#00843d",
        facts=[
            {"name": "File #", "value": file_num},
            {"name": "Analyst", "value": assigned_to},
            {"name": "Reviewer", "value": reviewer},
            {"name": "Due", "value": due},
        ])


def send_revision_notification(appearance: dict):
    """Email the analyst when their item is sent back for revision by the reviewer."""
    cfg = load_config()
    if not cfg.get("email_enabled"):
        return
    analyst = appearance.get("assigned_to") or ""
    if not analyst:
        return
    email = get_member_email(analyst)
    if not email:
        log.info(f"  No email for analyst '{analyst}' — revision notification skipped")
        return

    file_num    = appearance.get("file_number", "")
    title       = (appearance.get("short_title") or appearance.get("appearance_title") or "")[:80]
    reviewer    = appearance.get("reviewer") or "Reviewer"
    reviewer_notes = (appearance.get("reviewer_notes") or "").strip()[:500]

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#d97706;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>&#x21A9; AgendaIQ &#x2014; Revision Requested</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>Your draft has been sent back for revision</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:20px;'>
        <p style='margin:0 0 16px;'>Hi <strong>{analyst}</strong>, your reviewer (<strong>{reviewer}</strong>) has requested changes to:</p>
        <table style='width:100%;border-collapse:collapse;margin-bottom:16px;background:#f8fafc;'>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;width:130px;border-bottom:1px solid #e2e8f0;'>File #</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#003087;'>{file_num}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Title</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{title}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;'>Reviewer</td>
              <td style='padding:9px 14px;'>{reviewer}</td></tr>
        </table>
        {'<div style="background:#fffbeb;border:1px solid #fde68a;border-radius:6px;padding:12px;margin-bottom:16px;"><p style="margin:0 0 4px;font-weight:600;font-size:13px;color:#92400e;">Reviewer Feedback:</p><p style="margin:0;font-size:13px;color:#78350f;white-space:pre-wrap;">' + reviewer_notes + '</p></div>' if reviewer_notes else ''}
        <p style='color:#64748b;font-size:12px;margin:0;'>&#x2014; AgendaIQ &middot; Office of the Commission Auditor &middot; Miami-Dade County</p>
      </div>
    </div>"""

    cfg_to = dict(cfg)
    cfg_to["notify_recipients"] = [email]
    _send_email(cfg_to, f"[AgendaIQ] Revision Requested: {file_num} — {title[:45]}", html)

    # Webhook notification
    _send_webhook(cfg, f"↩ Revision Requested — {file_num}",
        f"*{reviewer}* sent *{file_num}* back to *{analyst}* for revision\n{title}",
        color="#d97706",
        facts=[
            {"name": "File #", "value": file_num},
            {"name": "Reviewer", "value": reviewer},
            {"name": "Analyst", "value": analyst},
            {"name": "Feedback", "value": reviewer_notes[:200] if reviewer_notes else "No comment"},
        ])


def send_approval_notification(appearance: dict):
    """Email the analyst when their item has been approved/finalized by the reviewer."""
    cfg = load_config()
    if not cfg.get("email_enabled"):
        return
    analyst = appearance.get("assigned_to") or ""
    if not analyst:
        return
    email = get_member_email(analyst)
    if not email:
        log.info(f"  No email for analyst '{analyst}' — approval notification skipped")
        return

    file_num    = appearance.get("file_number", "")
    title       = (appearance.get("short_title") or appearance.get("appearance_title") or "")[:80]
    reviewer    = appearance.get("reviewer") or "Reviewer"

    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#059669;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>&#x2705; AgendaIQ &#x2014; Brief Approved</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>Your draft has been approved and finalized</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:20px;'>
        <p style='margin:0 0 16px;'>Hi <strong>{analyst}</strong>, your brief has been approved by <strong>{reviewer}</strong>:</p>
        <table style='width:100%;border-collapse:collapse;margin-bottom:16px;background:#f8fafc;'>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;width:130px;border-bottom:1px solid #e2e8f0;'>File #</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;font-weight:600;color:#003087;'>{file_num}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;border-bottom:1px solid #e2e8f0;'>Title</td>
              <td style='padding:9px 14px;border-bottom:1px solid #e2e8f0;'>{title}</td></tr>
          <tr><td style='padding:9px 14px;font-weight:600;color:#475569;'>Status</td>
              <td style='padding:9px 14px;font-weight:600;color:#059669;'>Finalized &#x2714;</td></tr>
        </table>
        <p style='color:#64748b;font-size:12px;margin:0;'>&#x2014; AgendaIQ &middot; Office of the Commission Auditor &middot; Miami-Dade County</p>
      </div>
    </div>"""

    cfg_to = dict(cfg)
    cfg_to["notify_recipients"] = [email]
    _send_email(cfg_to, f"[AgendaIQ] Approved: {file_num} — {title[:45]}", html)

    # Webhook notification
    _send_webhook(cfg, f"✅ Brief Approved — {file_num}",
        f"*{reviewer}* approved *{file_num}* — status is now *Finalized*\n{title}",
        color="#059669",
        facts=[
            {"name": "File #", "value": file_num},
            {"name": "Analyst", "value": analyst},
            {"name": "Reviewer", "value": reviewer},
            {"name": "Status", "value": "Finalized ✓"},
        ])


def send_overdue_alert(overdue_items: list):
    cfg = load_config()
    if not overdue_items:
        return
    rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee;'>"
        f"<a href='#' style='color:#003087;'>{r.get('file_number','')}</a></td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('short_title','')[:60]}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:#dc2626;font-weight:600;'>{r.get('due_date','')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('assigned_to','Unassigned')}</td>"
        f"</tr>"
        for r in overdue_items
    )
    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#003087;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>🔴 AgendaIQ — Overdue Items</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>
          {len(overdue_items)} item(s) have passed their due date</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:16px;'>
        <table style='width:100%;border-collapse:collapse;'>
          <thead><tr style='background:#f8fafc;'>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>File #</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Title</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Due Date</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Assigned To</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style='color:#64748b;font-size:12px;margin-top:16px;'>
          — AgendaIQ · Office of the Commission Auditor · Miami-Dade County</p>
      </div>
    </div>"""
    _send_email(cfg, f"[AgendaIQ] {len(overdue_items)} Overdue Item(s) — Action Required", html)

    # Webhook notification for overdue items
    items_list = "\n".join(
        f"• *{r.get('file_number','')}* — {r.get('short_title','')[:50]} (due {r.get('due_date','?')}, assigned to {r.get('assigned_to','Unassigned')})"
        for r in overdue_items[:10]
    )
    _send_webhook(cfg, f"🔴 {len(overdue_items)} Overdue Item(s)",
        items_list,
        color="#dc2626")


def send_due_soon_reminder(due_soon_items: list, days: int = 7):
    cfg = load_config()
    if not due_soon_items:
        return
    rows = "".join(
        f"<tr><td style='padding:6px 12px;border-bottom:1px solid #eee;'>"
        f"{r.get('file_number','')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('short_title','')[:60]}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:#d97706;font-weight:600;'>{r.get('due_date','')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('assigned_to','Unassigned')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('workflow_status','')}</td>"
        f"</tr>"
        for r in due_soon_items
    )
    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#92400e;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>🟡 AgendaIQ — Due Soon Reminder</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>
          {len(due_soon_items)} item(s) due within {days} days</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:16px;'>
        <table style='width:100%;border-collapse:collapse;'>
          <thead><tr style='background:#f8fafc;'>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>File #</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Title</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Due Date</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Assigned To</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Status</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style='color:#64748b;font-size:12px;margin-top:16px;'>
          — AgendaIQ · Office of the Commission Auditor · Miami-Dade County</p>
      </div>
    </div>"""
    _send_email(cfg, f"[AgendaIQ] Reminder: {len(due_soon_items)} Item(s) Due Within {days} Days", html)


# ── Background checker ────────────────────────────────────────

_last_check = {"overdue": None, "due_soon": None}


def run_checks():
    """Check for overdue and due-soon items.

    Overdue:  one summary email → notify_recipients (team-lead visibility).
    Due soon: individual reminder → each assignee's own email only.
    """
    try:
        from workflow import get_overdue_appearances, get_due_soon_appearances
        cfg = load_config()
        days = cfg.get("reminder_days", 7)

        # ── Overdue: summary blast to team leads ──────────────────
        overdue = get_overdue_appearances()
        if overdue:
            send_overdue_alert(overdue)
            log.info(f"  Notification: {len(overdue)} overdue items (team summary sent)")

        # ── Due soon: individual reminder per assignee ────────────
        due_soon = get_due_soon_appearances(days)
        if due_soon:
            # Group by assignee
            by_person: dict = {}
            for item in due_soon:
                person = item.get("assigned_to") or "__unassigned__"
                by_person.setdefault(person, []).append(item)

            sent = 0
            for person, items in by_person.items():
                if person == "__unassigned__":
                    continue  # skip unassigned — no one to notify
                email = get_member_email(person)
                if email:
                    cfg_personal = dict(cfg)
                    cfg_personal["notify_recipients"] = [email]
                    send_due_soon_reminder(items, days)   # reuse HTML builder
                    # Override recipients for this call
                    _send_due_soon_to(cfg_personal, items, days, person)
                    sent += 1
            log.info(f"  Notification: {len(due_soon)} items due within {days} days "
                     f"({sent} individual reminder(s) sent)")

    except Exception as e:
        log.error(f"  Notification check failed: {e}")


def _send_due_soon_to(cfg: dict, items: list, days: int, person: str):
    """Send a personalised due-soon reminder to a single assignee."""
    rows = "".join(
        f"<tr>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('file_number','')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('short_title','')[:60]}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;color:#d97706;font-weight:600;'>{r.get('due_date','')}</td>"
        f"<td style='padding:6px 12px;border-bottom:1px solid #eee;'>{r.get('workflow_status','')}</td>"
        f"</tr>"
        for r in items
    )
    html = f"""
    <div style='font-family:Arial,sans-serif;max-width:700px;'>
      <div style='background:#92400e;color:#fff;padding:16px 24px;border-radius:8px 8px 0 0;'>
        <h2 style='margin:0;font-size:18px;'>&#x23F0; AgendaIQ &#x2014; Your Items Due Soon</h2>
        <p style='margin:4px 0 0;opacity:.8;font-size:13px;'>
          {len(items)} item(s) assigned to you are due within {days} days</p>
      </div>
      <div style='border:1px solid #e2e8f0;border-top:none;border-radius:0 0 8px 8px;padding:16px;'>
        <p style='margin:0 0 12px;'>Hi <strong>{person}</strong>, please review the following items:</p>
        <table style='width:100%;border-collapse:collapse;'>
          <thead><tr style='background:#f8fafc;'>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>File #</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Title</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Due Date</th>
            <th style='padding:8px 12px;text-align:left;font-size:12px;color:#64748b;'>Status</th>
          </tr></thead>
          <tbody>{rows}</tbody>
        </table>
        <p style='color:#64748b;font-size:12px;margin-top:16px;'>
          &#x2014; AgendaIQ &middot; Office of the Commission Auditor &middot; Miami-Dade County</p>
      </div>
    </div>"""
    _send_email(cfg, f"[AgendaIQ] Reminder: {len(items)} Item(s) Due Within {days} Days", html)


def start_background_checker(interval_hours: int = 1):
    """Start a daemon thread that runs checks every N hours."""
    def _loop():
        while True:
            run_checks()
            time.sleep(interval_hours * 3600)
    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info(f"  Notification checker started (every {interval_hours}h)")
