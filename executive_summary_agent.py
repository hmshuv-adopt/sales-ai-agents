#!/usr/bin/env python3
# Recommended: Run daily at 6:00 PM IST (12:30 UTC)
# Cron: 30 12 * * * /usr/bin/python3 /path/to/executive_summary_agent.py
# Or via GitHub Actions / Digital Ocean scheduled job

"""
Executive Deal Summary Agent

This script runs daily (e.g. every evening) to:
1. Fetch ALL active deals from HubSpot in configured stages (Potential Fit, Proposal Sent, Negotiation, Contract)
2. For each deal, collect context: deal name, stage, value, owner, contact, company, last email, last note, days since activity
3. Send context to Claude (claude-3-5-sonnet) to generate a 4-6 sentence executive summary per deal
4. Build a single HTML digest grouped by stage and send to EXECUTIVE_SUMMARY_RECIPIENTS

Usage:
    python executive_summary_agent.py

Environment Variables Required:
    ANTHROPIC_API_KEY - Your Anthropic API key
    HUBSPOT_ACCESS_TOKEN - Your HubSpot private app access token
    EXECUTIVE_SUMMARY_RECIPIENTS - Comma-separated list of email addresses for the digest
    SUMMARY_STAGES - Comma-separated HubSpot deal stage IDs (same order as SUMMARY_STAGE_LABELS)
    FROM_EMAIL - Sender email address for the digest

Environment Variables Optional:
    SUMMARY_STAGE_LABELS - Comma-separated display names for stages (e.g. Potential Fit,Proposal Sent,Negotiation,Contract)
    EXECUTIVE_SUMMARY_SUBJECT - Email subject; use {date} for today's date (default: Daily Deal Pipeline Summary — {date})
    SENDGRID_API_KEY - SendGrid API key for email delivery
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD - SMTP fallback if SendGrid not set
"""

import os
import html
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional
import anthropic
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
HUBSPOT_BASE_URL = "https://api.hubapi.com"

# Digest recipients (comma-separated in env var)
EXECUTIVE_SUMMARY_RECIPIENTS = [
    email.strip()
    for email in os.getenv("EXECUTIVE_SUMMARY_RECIPIENTS", "").split(",")
    if email.strip()
]
if not EXECUTIVE_SUMMARY_RECIPIENTS:
    raise ValueError(
        "EXECUTIVE_SUMMARY_RECIPIENTS environment variable is required (comma-separated emails)"
    )

# Sender email for digest
FROM_EMAIL = os.getenv("FROM_EMAIL", "noreply@example.com")

# Deal stages to include (comma-separated HubSpot stage IDs, in display order)
SUMMARY_STAGES = [
    stage.strip()
    for stage in os.getenv("SUMMARY_STAGES", "").split(",")
    if stage.strip()
]
if not SUMMARY_STAGES:
    raise ValueError(
        "SUMMARY_STAGES environment variable is required.\n"
        "Set it to a comma-separated list of HubSpot deal stage IDs.\n"
        "Find stage IDs in HubSpot: Settings → Objects → Deals → Pipelines."
    )

# Optional display labels for stages (same order as SUMMARY_STAGES)
SUMMARY_STAGE_LABELS = [
    label.strip()
    for label in os.getenv("SUMMARY_STAGE_LABELS", "").split(",")
    if label.strip()
]
# If labels not provided or count mismatch, use stage ID as label
if len(SUMMARY_STAGE_LABELS) != len(SUMMARY_STAGES):
    SUMMARY_STAGE_LABELS = SUMMARY_STAGES

# Email subject template; {date} is replaced with today's date
EXECUTIVE_SUMMARY_SUBJECT = os.getenv(
    "EXECUTIVE_SUMMARY_SUBJECT", "Daily Deal Pipeline Summary — {date}"
)


# --- HubSpot client (same pattern as followup_agent.py) ---


class HubSpotClient:
    """Client for HubSpot API interactions."""

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def search_deals(
        self, stages: list[str], properties: list[str]
    ) -> list[dict]:
        """Search for deals in specific stages. Handles pagination."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/search"

        payload = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "dealstage",
                            "operator": "IN",
                            "values": stages,
                        }
                    ]
                }
            ],
            "properties": properties,
            "limit": 100,
        }

        all_deals = []
        after = None

        while True:
            if after:
                payload["after"] = after

            response = requests.post(url, headers=self.headers, json=payload)
            response.raise_for_status()
            data = response.json()

            all_deals.extend(data.get("results", []))

            paging = data.get("paging", {})
            if paging.get("next"):
                after = paging["next"]["after"]
            else:
                break

        return all_deals

    def get_associated_contacts(self, deal_id: str) -> list[dict]:
        """Get contacts associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/contacts"

        response = requests.get(url, headers=self.headers)
        response.raise_for_status()

        associations = response.json().get("results", [])
        if not associations:
            return []

        contact_ids = [a.get("toObjectId") or a.get("id") for a in associations]

        contacts_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/contacts/batch/read"
        contacts_payload = {
            "inputs": [{"id": cid} for cid in contact_ids],
            "properties": [
                "email",
                "firstname",
                "lastname",
                "jobtitle",
                "company",
            ],
        }

        contacts_response = requests.post(
            contacts_url, headers=self.headers, json=contacts_payload
        )
        contacts_response.raise_for_status()

        return contacts_response.json().get("results", [])

    def get_associated_company(self, deal_id: str) -> Optional[dict]:
        """Get the company associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v4/objects/deals/{deal_id}/associations/companies"

        response = requests.get(url, headers=self.headers)
        response.raise_for_status()

        associations = response.json().get("results", [])
        if not associations:
            return None

        company_id = associations[0].get("toObjectId") or associations[0].get("id")

        company_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/companies/{company_id}"
        company_response = requests.get(
            company_url,
            headers=self.headers,
            params={
                "properties": "name,industry,numberofemployees,description,website"
            },
        )
        company_response.raise_for_status()

        return company_response.json()

    def get_deal_emails(self, deal_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a deal (with pagination)."""
        return self._get_object_emails("deals", deal_id, limit)

    def get_company_emails(self, company_id: str, limit: int = 50) -> list[dict]:
        """Get emails associated with a company (with pagination)."""
        return self._get_object_emails("companies", company_id, limit)

    def _get_object_emails(
        self, object_type: str, object_id: str, limit: int = 50
    ) -> list[dict]:
        """Get emails associated with any CRM object, with pagination support."""
        all_email_ids = []
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/{object_type}/{object_id}/associations/emails"

        while url:
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()

            data = response.json()
            associations = data.get("results", [])

            for a in associations:
                email_id = a.get("toObjectId") or a.get("id")
                if email_id:
                    all_email_ids.append(email_id)

            paging = data.get("paging", {})
            next_link = paging.get("next", {}).get("link")
            url = next_link if next_link else None

        if not all_email_ids:
            return []

        emails = self._fetch_emails_by_ids(all_email_ids)

        def get_email_timestamp(email):
            props = email.get("properties", {})
            ts = props.get("hs_timestamp") or props.get("hs_createdate") or "0"
            if isinstance(ts, str):
                try:
                    return datetime.fromisoformat(
                        ts.replace("Z", "+00:00")
                    ).timestamp()
                except ValueError:
                    return 0
            return ts / 1000 if ts else 0

        emails.sort(key=get_email_timestamp, reverse=True)
        return emails[:limit]

    def _fetch_emails_by_ids(self, email_ids: list[str]) -> list[dict]:
        """Fetch email details by IDs. Batches of 100 (HubSpot limit)."""
        if not email_ids:
            return []

        unique_ids = list(set(email_ids))
        all_emails = []
        emails_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/emails/batch/read"
        batch_size = 100

        for i in range(0, len(unique_ids), batch_size):
            batch_ids = unique_ids[i : i + batch_size]
            emails_payload = {
                "inputs": [{"id": eid} for eid in batch_ids],
                "properties": [
                    "hs_email_subject",
                    "hs_email_status",
                    "hs_email_direction",
                    "hs_timestamp",
                    "hs_createdate",
                ],
            }
            emails_response = requests.post(
                emails_url, headers=self.headers, json=emails_payload
            )
            emails_response.raise_for_status()
            all_emails.extend(emails_response.json().get("results", []))

        return all_emails

    def get_deal_notes(self, deal_id: str, limit: int = 5) -> list[dict]:
        """Get notes associated with a deal."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/deals/{deal_id}/associations/notes"

        response = requests.get(url, headers=self.headers)
        response.raise_for_status()

        associations = response.json().get("results", [])
        if not associations:
            return []

        note_ids = [a.get("toObjectId") or a.get("id") for a in associations[:limit]]

        notes_url = f"{HUBSPOT_BASE_URL}/crm/v3/objects/notes/batch/read"
        notes_payload = {
            "inputs": [{"id": nid} for nid in note_ids],
            "properties": ["hs_note_body", "hs_timestamp", "hs_createdate"],
        }

        notes_response = requests.post(
            notes_url, headers=self.headers, json=notes_payload
        )
        notes_response.raise_for_status()

        return notes_response.json().get("results", [])

    def get_owners(self) -> dict:
        """Fetch all owners and return a map of owner_id -> email (or id if no email)."""
        url = f"{HUBSPOT_BASE_URL}/crm/v3/owners"
        result = {}
        after = None

        while True:
            params = {"limit": 100}
            if after:
                params["after"] = after

            response = requests.get(url, headers=self.headers, params=params)
            response.raise_for_status()
            data = response.json()

            for owner in data.get("results", []):
                oid = owner.get("id")
                if oid:
                    # Prefer email, else firstname + lastname, else id
                    email = owner.get("email", "")
                    first = owner.get("firstName", "")
                    last = owner.get("lastName", "")
                    name = f"{first} {last}".strip() or email or str(oid)
                    result[str(oid)] = name

            paging = data.get("paging", {})
            if paging.get("next"):
                after = paging["next"].get("after")
            else:
                break

        return result


# --- Helpers: last email (any direction), last note, days since ---


def get_last_email_date_and_subject(emails: list[dict]) -> tuple[Optional[datetime], str]:
    """
    From a list of HubSpot emails (deal + company combined), return the most recent
    email date (inbound or outbound) and its subject. Used for executive summary context.
    """
    if not emails:
        return None, "No emails"

    def ts(email):
        props = email.get("properties", {})
        t = props.get("hs_timestamp") or props.get("hs_createdate")
        if not t:
            return 0
        if isinstance(t, str):
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0
        return t / 1000 if t else 0

    sorted_emails = sorted(emails, key=ts, reverse=True)
    most_recent = sorted_emails[0]
    props = most_recent.get("properties", {})
    subject = props.get("hs_email_subject", "No subject") or "No subject"
    t = props.get("hs_timestamp") or props.get("hs_createdate")
    if not t:
        return None, subject
    try:
        if isinstance(t, str):
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(t / 1000)
        return dt, subject
    except (ValueError, TypeError):
        return None, subject


def get_last_note_text_and_date(
    notes: list[dict],
) -> tuple[str, Optional[datetime]]:
    """
    From a list of HubSpot notes, return the most recent note body and its date.
    """
    if not notes:
        return "No notes", None

    def ts(n):
        props = n.get("properties", {})
        t = props.get("hs_timestamp") or props.get("hs_createdate")
        if not t:
            return 0
        if isinstance(t, str):
            try:
                return datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return 0
        return t / 1000 if t else 0

    sorted_notes = sorted(notes, key=ts, reverse=True)
    most_recent = sorted_notes[0]
    props = most_recent.get("properties", {})
    body = (props.get("hs_note_body") or "No content")[:500]
    t = props.get("hs_timestamp") or props.get("hs_createdate")
    if not t:
        return body, None
    try:
        if isinstance(t, str):
            dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(t / 1000)
        return body, dt
    except (ValueError, TypeError):
        return body, None


def days_since(dt: Optional[datetime]) -> Optional[int]:
    """Return days between dt and now. None if dt is None."""
    if not dt:
        return None
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    delta = now - dt
    return max(0, delta.days)


def format_deal_value(amount: Optional[str], currency_code: Optional[str]) -> str:
    """Format deal amount for display. Show 'Not Set' if amount is null/empty."""
    if amount is None or str(amount).strip() == "":
        return "Not Set"
    try:
        val = float(amount)
    except (ValueError, TypeError):
        return "Not Set"
    symbol = "₹" if (currency_code and str(currency_code).upper() == "INR") else "$"
    return f"{symbol}{val:,.0f}"


# --- Build deal context for Claude ---


def build_deal_context(
    deal: dict,
    hubspot: HubSpotClient,
    owners_map: dict,
    stage_label: str,
) -> Optional[dict]:
    """
    For one deal, fetch associated contact, company, emails (deal + company), notes;
    compute last email date/subject, last note, and days since last activity.
    Returns a single dict with all context needed for Claude, or None on critical failure.
    """
    deal_id = deal["id"]
    props = deal.get("properties", {})

    deal_name = props.get("dealname", "Unknown Deal")
    stage_id = props.get("dealstage", "")
    amount = props.get("amount")
    currency_code = props.get("deal_currency_code")
    owner_id = props.get("hubspot_owner_id", "")
    deal_owner = owners_map.get(str(owner_id), owner_id or "Unassigned")

    # Associated contact
    try:
        contacts = hubspot.get_associated_contacts(deal_id)
    except Exception as e:
        print(f"   ⚠️ Failed to get contacts for {deal_name}: {e}")
        contacts = []
    contact = contacts[0] if contacts else {}
    contact_props = contact.get("properties", {})
    contact_name = (
        f"{contact_props.get('firstname', '')} {contact_props.get('lastname', '')}".strip()
        or "Unknown"
    )
    contact_title = contact_props.get("jobtitle", "Unknown")
    contact_email = contact_props.get("email", "No email")

    # Associated company
    try:
        company = hubspot.get_associated_company(deal_id)
    except Exception as e:
        print(f"   ⚠️ Failed to get company for {deal_name}: {e}")
        company = None
    company_props = company.get("properties", {}) if company else {}
    company_name = company_props.get("name", "Unknown Company")
    company_industry = company_props.get("industry", "Unknown")
    company_size = company_props.get("numberofemployees", "Unknown")

    # Emails: deal + company (deduplicated by id), then last email date + subject
    try:
        deal_emails = hubspot.get_deal_emails(deal_id, limit=50)
        company_id = company.get("id") if company else None
        company_emails = (
            hubspot.get_company_emails(company_id, limit=50) if company_id else []
        )
    except Exception as e:
        print(f"   ⚠️ Failed to get emails for {deal_name}: {e}")
        deal_emails = []
        company_emails = []

    seen = set()
    unique_emails = []
    for e in deal_emails + company_emails:
        eid = e.get("id")
        if eid and eid not in seen:
            seen.add(eid)
            unique_emails.append(e)

    last_email_date, last_email_subject = get_last_email_date_and_subject(
        unique_emails
    )

    # Notes
    try:
        notes = hubspot.get_deal_notes(deal_id, limit=5)
    except Exception as e:
        print(f"   ⚠️ Failed to get notes for {deal_name}: {e}")
        notes = []
    last_note_text, last_note_date = get_last_note_text_and_date(notes)

    # Last activity = max of last email and last note
    last_activity_date = None
    if last_email_date and last_note_date:
        last_activity_date = max(last_email_date, last_note_date)
    elif last_email_date:
        last_activity_date = last_email_date
    elif last_note_date:
        last_activity_date = last_note_date

    days_since_activity = days_since(last_activity_date)
    if days_since_activity is None:
        days_since_activity = "N/A"

    return {
        "deal_name": deal_name,
        "stage": stage_label,
        "deal_value": format_deal_value(amount, currency_code),
        "deal_owner": deal_owner,
        "contact_name": contact_name,
        "contact_title": contact_title,
        "contact_email": contact_email,
        "company_name": company_name,
        "company_industry": company_industry,
        "company_size": company_size,
        "last_email_date": last_email_date.strftime("%Y-%m-%d %H:%M")
        if last_email_date
        else "N/A",
        "last_email_subject": last_email_subject,
        "last_note_text": last_note_text,
        "last_note_date": last_note_date.strftime("%Y-%m-%d %H:%M")
        if last_note_date
        else "N/A",
        "days_since_activity": days_since_activity,
    }


# --- Claude: generate 4-6 sentence executive summary per deal ---


def generate_deal_summary(
    client: anthropic.Anthropic, deal_context: dict
) -> str:
    """
    Call Claude (claude-3-5-sonnet) to generate a 4-6 sentence executive summary
    for one deal. Professional, honest (call out if stuck), ending with one clear next action.
    """
    system = """You are a senior sales intelligence assistant helping an executive team understand their deal pipeline. Your job is to write brief, accurate executive summaries of individual deals. Be professional and conversational. Be honest: if a deal looks stuck or cold, say so clearly. Do not invent any information that is not present in the context. Always end with one specific, actionable next step for the sales team."""

    user = f"""Based on the following deal context, write a single paragraph (4-6 sentences) that covers:
1. Who the contact is and what their company does
2. Current deal stage and deal value
3. What the last interaction was and when
4. Whether the deal looks healthy, progressing, or stuck
5. One clear recommended next action for the sales team

Write in a professional but conversational tone. Do not use bullet points — write one flowing paragraph. Do not make up facts.

Deal context:
- Deal: {deal_context['deal_name']} | Stage: {deal_context['stage']} | Value: {deal_context['deal_value']} | Owner: {deal_context['deal_owner']}
- Contact: {deal_context['contact_name']}, {deal_context['contact_title']} | {deal_context['contact_email']}
- Company: {deal_context['company_name']} | Industry: {deal_context['company_industry']} | Size: {deal_context['company_size']}
- Last email: {deal_context['last_email_date']} — Subject: {deal_context['last_email_subject']}
- Last note/activity: {deal_context['last_note_date']} — {deal_context['last_note_text'][:300]}
- Days since last activity: {deal_context['days_since_activity']}

Write your 4-6 sentence executive summary paragraph below:"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = response.content[0].text
        return text.strip() if text else "No summary generated."
    except Exception as e:
        return f"[Summary unavailable: {str(e)}]"


# --- HTML digest: grouped by stage, inline CSS ---


# Stage label -> (badge background color, border color) for inline styles
STAGE_COLORS = {
    "potential fit": ("#fef3c7", "#d97706"),
    "proposal sent": ("#ffedd5", "#ea580c"),
    "negotiation": ("#fee2e2", "#dc2626"),
    "contract": ("#dcfce7", "#16a34a"),
}


def _stage_style(label: str) -> tuple[str, str]:
    key = label.lower().strip()
    return STAGE_COLORS.get(key, ("#f3f4f6", "#6b7280"))


def format_digest_html(
    deals_by_stage: dict[str, list[dict]], stage_order: list[str], stage_labels: list[str]
) -> str:
    """
    Build the executive summary HTML email: header, subheader, one section per stage
    (skipped if no deals), then each deal with AI summary box. Inline CSS only.
    """
    today = datetime.now().strftime("%B %d, %Y")
    total_deals = sum(len(deals) for deals in deals_by_stage.values())
    # Time in IST (UTC+5:30)
    ist = timezone(timedelta(hours=5, minutes=30))
    generated_at = datetime.now(ist).strftime("%I:%M %p IST")

    # Use 'output' so we don't shadow the 'html' module (needed for html.escape)
    output = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Executive Deal Pipeline Summary</title>
</head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
    <div style="background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 24px; border-radius: 10px; margin-bottom: 24px;">
        <h1 style="margin: 0; font-size: 22px;">📊 Executive Deal Pipeline Summary | {today}</h1>
        <p style="margin: 10px 0 0 0; opacity: 0.95; font-size: 14px;">Total deals: {total_deals} across {len([s for s in stage_order if deals_by_stage.get(s)])} stages | Generated at {generated_at}</p>
    </div>
"""

    for stage_id, label in zip(stage_order, stage_labels):
        deals = deals_by_stage.get(stage_id, [])
        if not deals:
            continue

        bg, border = _stage_style(label)
        output += f"""
    <div style="margin-bottom: 24px;">
        <h2 style="font-size: 16px; margin: 0 0 12px 0; color: #374151;">{label.upper()} — {len(deals)} Deal(s)</h2>
"""
        for d in deals:
            # Escape user content for safe HTML (use 'html' module, not local variable)
            safe_summary = html.escape(d.get("ai_summary", "No summary available.")).replace(
                "\n", "<br>"
            )
            output += f"""
        <div style="background: #fafafa; border-radius: 8px; padding: 16px; margin-bottom: 16px; border-left: 4px solid {border};">
            <p style="margin: 0 0 8px 0; font-size: 15px;"><strong>{html.escape(str(d['deal_name']))}</strong> | {html.escape(str(d['stage']))} | {html.escape(str(d['deal_value']))}</p>
            <p style="margin: 0 0 8px 0; font-size: 13px; color: #555;">Contact: {html.escape(str(d['contact_name']))}, {html.escape(str(d['contact_title']))} | Company: {html.escape(str(d['company_name']))}, {html.escape(str(d['company_industry']))}, {html.escape(str(d['company_size']))}</p>
            <p style="margin: 0 0 12px 0; font-size: 13px; color: #666;">Last Activity: {html.escape(str(d['last_email_date']))} — {html.escape(str(d['days_since_activity']))} days ago | Last Email Subject: {html.escape(str(d['last_email_subject']))}</p>
            <div style="background: #e5e7eb; border-left: 3px solid #6b7280; padding: 12px; margin-top: 8px; font-size: 13px; color: #374151; line-height: 1.5;">
                {safe_summary}
            </div>
        </div>
"""
        output += "    </div>\n"

    output += """
    <div style="text-align: center; color: #9ca3af; font-size: 12px; margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb;">
        <p style="margin: 0;">This digest was auto-generated by Sales AI Agent. Do not reply to this email.</p>
    </div>
</body>
</html>
"""
    return output


# --- Send digest: SendGrid, SMTP, or file fallback ---


def send_digest_email_sendgrid(
    to_emails: list[str], html_content: str, api_key: str, subject: str
) -> None:
    """Send the digest email using SendGrid."""
    url = "https://api.sendgrid.com/v3/mail/send"
    payload = {
        "personalizations": [{"to": [{"email": e} for e in to_emails]}],
        "from": {"email": FROM_EMAIL, "name": "Executive Summary Agent"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_content}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    response = requests.post(url, headers=headers, json=payload)
    response.raise_for_status()
    print(f"✅ Digest email sent to {', '.join(to_emails)}")


def send_digest_email_smtp(
    to_emails: list[str], html_content: str, subject: str
) -> None:
    """Send the digest email using SMTP."""
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = os.getenv("SMTP_HOST")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    from_email = os.getenv("SMTP_FROM_EMAIL", smtp_user)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(to_emails)
    msg.attach(MIMEText(html_content, "html"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, to_emails, msg.as_string())
    print(f"✅ Digest email sent to {', '.join(to_emails)}")


# --- Main ---


def main() -> list[dict]:
    """
    Main execution flow: fetch deals in SUMMARY_STAGES, build context per deal,
    call Claude for summary, build HTML digest, send or save to file.
    Handles empty pipeline (0 deals) and per-deal errors without crashing.
    """
    print("📊 Starting Executive Deal Summary Agent...")
    print(f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("-" * 50)

    hubspot_token = os.getenv("HUBSPOT_ACCESS_TOKEN")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    sendgrid_key = os.getenv("SENDGRID_API_KEY")

    if not hubspot_token:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is required")
    if not anthropic_key:
        raise ValueError("ANTHROPIC_API_KEY environment variable is required")

    hubspot = HubSpotClient(hubspot_token)
    claude = anthropic.Anthropic(api_key=anthropic_key)

    # Stage ID -> display label (same order as SUMMARY_STAGES)
    stage_id_to_label = dict(zip(SUMMARY_STAGES, SUMMARY_STAGE_LABELS))

    # Fetch all deals in configured stages (no stale filter)
    print(f"\n📊 Fetching deals in stages: {', '.join(stage_id_to_label.get(s, s) for s in SUMMARY_STAGES)}")
    deal_properties = [
        "dealname",
        "dealstage",
        "amount",
        "deal_currency_code",
        "hubspot_owner_id",
        "closedate",
        "hs_lastmodifieddate",
    ]
    try:
        all_deals = hubspot.search_deals(stages=SUMMARY_STAGES, properties=deal_properties)
    except Exception as e:
        print(f"❌ Failed to fetch deals from HubSpot: {e}")
        raise

    print(f"   Found {len(all_deals)} deals")

    # Group by stage in configured order (so digest order is stable)
    deals_by_stage = {s: [] for s in SUMMARY_STAGES}
    for deal in all_deals:
        stage_id = deal.get("properties", {}).get("dealstage", "")
        if stage_id in deals_by_stage:
            deals_by_stage[stage_id].append(deal)

    # Owners map for display names
    try:
        owners_map = hubspot.get_owners()
    except Exception as e:
        print(f"   ⚠️ Could not fetch owners: {e}. Using owner IDs.")
        owners_map = {}

    # Build context and AI summary for each deal (try/except per deal)
    enriched_by_stage = {s: [] for s in SUMMARY_STAGES}
    total_processed = 0
    total_failed = 0

    for stage_id in SUMMARY_STAGES:
        stage_label = stage_id_to_label.get(stage_id, stage_id)
        stage_deals = deals_by_stage.get(stage_id, [])
        for i, deal in enumerate(stage_deals):
            deal_name = deal.get("properties", {}).get("dealname", "Unknown")
            total_processed += 1
            print(f"\n🔍 Processing deal {total_processed}/{len(all_deals)}: {deal_name}")
            try:
                ctx = build_deal_context(deal, hubspot, owners_map, stage_label)
                if not ctx:
                    total_failed += 1
                    continue
            except Exception as e:
                print(f"   ❌ Error building context: {e}")
                total_failed += 1
                continue

            try:
                summary = generate_deal_summary(claude, ctx)
                ctx["ai_summary"] = summary
            except Exception as e:
                print(f"   ⚠️ Claude summary failed: {e}")
                ctx["ai_summary"] = f"[Summary failed: {str(e)}]"

            enriched_by_stage[stage_id].append(ctx)
            print(f"   ✓ Summary generated")

    # Build digest
    today_str = datetime.now().strftime("%Y-%m-%d")
    subject = EXECUTIVE_SUMMARY_SUBJECT.format(date=today_str)

    # If no deals at all, still send a short digest so recipients know the run succeeded
    if len(all_deals) == 0:
        html_digest = f"""
<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Executive Deal Pipeline Summary</title></head>
<body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; line-height: 1.6; color: #333; max-width: 800px; margin: 0 auto; padding: 20px;">
    <div style="background: linear-gradient(135deg, #1e3a5f 0%, #2d5a87 100%); color: white; padding: 24px; border-radius: 10px;">
        <h1 style="margin: 0; font-size: 22px;">📊 Executive Deal Pipeline Summary | {datetime.now().strftime('%B %d, %Y')}</h1>
        <p style="margin: 10px 0 0 0;">Total deals: 0 across 0 stages | Generated at {datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%I:%M %p IST')}</p>
    </div>
    <div style="padding: 24px; text-align: center; color: #666;">No deals in the configured stages today. Pipeline is clear for the selected stages.</div>
    <div style="text-align: center; color: #9ca3af; font-size: 12px; margin-top: 32px; padding-top: 16px; border-top: 1px solid #e5e7eb;">
        <p style="margin: 0;">This digest was auto-generated by Sales AI Agent. Do not reply to this email.</p>
    </div>
</body>
</html>
"""
    else:
        html_digest = format_digest_html(
            enriched_by_stage, SUMMARY_STAGES, SUMMARY_STAGE_LABELS
        )

    # Save local copy (always)
    digest_filename = f"deal_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    digest_path = os.path.join(os.path.dirname(__file__), digest_filename)
    with open(digest_path, "w", encoding="utf-8") as f:
        f.write(html_digest)
    print(f"\n📄 Saved local copy: {digest_path}")

    # Send email if configured
    if sendgrid_key:
        print("📧 Sending digest via SendGrid...")
        try:
            send_digest_email_sendgrid(
                EXECUTIVE_SUMMARY_RECIPIENTS, html_digest, sendgrid_key, subject
            )
        except Exception as e:
            print(f"⚠️ SendGrid failed: {e}. Digest saved to {digest_path}")
    elif os.getenv("SMTP_HOST"):
        print("📧 Sending digest via SMTP...")
        try:
            send_digest_email_smtp(
                EXECUTIVE_SUMMARY_RECIPIENTS, html_digest, subject
            )
        except Exception as e:
            print(f"⚠️ SMTP failed: {e}. Digest saved to {digest_path}")
    else:
        print(
            f"⚠️ No email service configured. Digest saved to: {digest_path}\n"
            "   Set SENDGRID_API_KEY or SMTP_* to enable email delivery."
        )

    print(f"\n✅ Agent completed successfully!")
    print(f"   Deals fetched: {len(all_deals)}")
    print(f"   Summaries generated: {total_processed - total_failed}")
    if total_failed:
        print(f"   Failed: {total_failed}")

    # Return flat list of enriched deals for tests
    result = []
    for stage_id in SUMMARY_STAGES:
        result.extend(enriched_by_stage.get(stage_id, []))
    return result


if __name__ == "__main__":
    main()
