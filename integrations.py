"""
integrations.py — Slack notifications and Google Sheets logging for the Gap Detector.

Required env vars:
  SLACK_BOT_TOKEN      — Bot OAuth token (xoxb-...)
  SLACK_CHANNEL_ID     — Channel or DM to post alerts to
  GOOGLE_SHEETS_ID     — Spreadsheet ID from the Sheets URL
  GOOGLE_CREDS_JSON    — Path to a service-account credentials JSON file
"""

import os
import re
import json
import datetime

# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

def send_slack_alert(breaches: list[dict]) -> None:
    """Post a P0 Critical alert to Slack for each critical breach."""
    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        raise RuntimeError(
            "slack_sdk is not installed. Run: pip install slack-sdk"
        )

    token      = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")

    if not token or not channel_id:
        raise EnvironmentError(
            "SLACK_BOT_TOKEN and SLACK_CHANNEL_ID must be set as environment variables."
        )

    client = WebClient(token=token)

    for breach in breaches:
        text = (
            f":rotating_light: *P0 CRITICAL BREACH DETECTED* :rotating_light:\n"
            f"*ID:* {breach['id']}\n"
            f"*Rule:* {breach['rule']}\n"
            f"*Finding:* {breach['finding']}"
        )
        try:
            client.chat_postMessage(channel=channel_id, text=text)
        except SlackApiError as e:
            raise RuntimeError(f"Slack API error for {breach['id']}: {e.response['error']}")


# ---------------------------------------------------------------------------
# Slack — Policy proposal with contextual snippet
# ---------------------------------------------------------------------------

def _extract_contextual_snippet(gold_text: str, contradicted_rule: str) -> str:
    """
    Pull the specific rule block from the Gold Book that matches the
    contradicted rule. Falls back to the first 400 characters if not found.
    """
    num_match = re.search(r'rule\s*(\d)', contradicted_rule, re.IGNORECASE)
    if num_match:
        n = num_match.group(1)
        # Grab from "RULE N:" up to the next rule section or closing divider.
        pattern = rf'(RULE {n}:.*?)(?=RULE \d+:|={4,}|NON-COMPLIANCE)'
        m = re.search(pattern, gold_text, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return gold_text[:400].strip()


def post_proposed_change(
    client,
    channel: str,
    thread_ts: str,
    alert: dict,
    gold_policy_path: str = "",
    gold_text: str = "",
) -> None:
    """
    Post a Proposed Policy Update card showing:
    - The proposed change and gap
    - A Contextual Snippet of the exact rule being contradicted
    - Approve / Reject buttons

    Pass gold_text directly (from Notion fetch) to skip the local file read.
    Falls back to reading gold_policy_path if gold_text is empty.
    """
    if not gold_text:
        try:
            with open(gold_policy_path, "r", encoding="utf-8") as f:
                gold_text = f.read()
        except OSError:
            gold_text = ""

    change  = alert.get("proposed_change", "Proposed policy change.")
    rule    = alert.get("contradicted_rule", "Gold Book rule")
    gap     = alert.get("gap", "")
    domain  = alert.get("domain", "Unknown")
    sop     = alert.get("sop_path", "N/A")

    snippet  = _extract_contextual_snippet(gold_text, rule) if gold_text else "(policy file unavailable)"
    gap_line = f"\n*Gap if applied:* `{gap}`" if gap else ""

    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":memo:  Proposed Policy Update Detected",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Domain*\n{domain}"},
                    {"type": "mrkdwn", "text": f"*Affected SOP*\n`{sop}`"},
                    {"type": "mrkdwn", "text": f"*Contradicts*\n{rule}"},
                ],
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Proposed Change:*\n{change}"
                        f"{gap_line}"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Contextual Snippet — {rule}:*\n"
                        f"```{snippet}```"
                    ),
                },
            },
            {"type": "divider"},
            {
                "type": "actions",
                "block_id": "policy_decision",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Approve Change", "emoji": True},
                        "style": "primary",
                        "action_id": "approve_change",
                        "value": json.dumps({"change": change, "contradicted_rule": rule}),
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Reject", "emoji": True},
                        "style": "danger",
                        "action_id": "reject_change",
                        "value": "reject",
                    },
                ],
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":shield: Approval writes the surgical edit directly to the Gold Book.",
                    }
                ],
            },
        ],
        text=f"Proposed Policy Update: {change}",
    )


def post_policy_update_confirmation(
    client,
    channel: str,
    thread_ts: str,
    approver: str,
    updated_policy: str,
) -> None:
    """
    Post the full updated Gold Book in the thread immediately after approval
    so the team can review the result of the surgical edit.
    """
    # Split at the first rule block so the preview starts at the rules, not the header.
    rules_match = re.search(r'(RULE 1:.*)', updated_policy, re.DOTALL | re.IGNORECASE)
    policy_body = rules_match.group(1) if rules_match else updated_policy

    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":white_check_mark:  Gold Book Updated — Surgical Edit Applied",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"Approved and written by *@{approver}*. Full updated policy:",
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"```{policy_body[:2800]}{'...' if len(policy_body) > 2800 else ''}```",
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": ":lock: This is the live Gold Book. All SOPs are now measured against this version.",
                    }
                ],
            },
        ],
        text=f"Gold Book updated by @{approver}. Surgical edit applied.",
    )


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

def log_policy_evolution_to_notion(summary: str, approver: str) -> None:
    """
    Log a Slack-approved policy change to the Notion Audit Database.

    Creates a page with these exact properties (add them to your Notion DB):
      Summary     (Title)
      Event Type  (Select  — value: "Policy Evolution")
      Action      (Select  — value: "Approved Change")
      Source      (Rich Text — value: "Slack Interaction")
      Approved By (Rich Text — Slack username of the approver)
      Date        (Date)
    """
    try:
        from notion_client import Client as NotionClient
        from notion_client.errors import APIResponseError
    except ImportError:
        raise RuntimeError("notion-client is not installed. Run: pip install notion-client")

    api_key = os.environ.get("NOTION_API_KEY")
    db_id   = os.environ.get("NOTION_DATABASE_ID")

    if not api_key or not db_id:
        raise EnvironmentError(
            "NOTION_API_KEY and NOTION_DATABASE_ID must be set as environment variables."
        )

    notion    = NotionClient(auth=api_key)
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

    try:
        notion.pages.create(
            parent={"database_id": db_id},
            properties={
                "Summary": {
                    "title": [{"type": "text", "text": {"content": summary[:255]}}]
                },
                "Event Type": {
                    "select": {"name": "Policy Evolution"}
                },
                "Action": {
                    "select": {"name": "Approved Change"}
                },
                "Source": {
                    "rich_text": [{"type": "text", "text": {"content": "Slack Interaction"}}]
                },
                "Approved By": {
                    "rich_text": [{"type": "text", "text": {"content": approver}}]
                },
                "Date": {
                    "date": {"start": timestamp}
                },
            },
        )
    except APIResponseError as e:
        if e.status == 404:
            print(
                "❌ NOTION ERROR: Ensure the integration has been INVITED to the page "
                "and Database IDs are correct."
            )
        raise


def _build_audit_properties(
    finding: str,
    severity: str,
    details: str,
    timestamp: str,
) -> dict:
    """
    Build the Notion properties dict for the Audit Log database.

    Shared by both auditor.py (breach logging) and evolution.py (approval logging)
    to guarantee an identical payload structure and prevent 400 mismatches.

    Schema (exact column names required in Notion):
      Finding  — Title
      Severity — Select  (P0 | P1 | P2 | Evolution | blank → null clears the field)
      Details  — Rich Text
      Date     — Date
    """
    properties: dict = {
        "Finding": {
            "title": [{"type": "text", "text": {"content": finding[:255]}}]
        },
        "Details": {
            "rich_text": [{"type": "text", "text": {"content": details[:2000]}}]
        },
        "Date": {
            "date": {"start": timestamp}
        },
    }

    # Severity is optional — omit the property entirely when blank so Notion
    # does not reject an empty select value with a 400.
    if severity:
        properties["Severity"] = {"select": {"name": severity}}

    return properties


def log_to_notion_audit(finding: str, severity: str, details: str) -> None:
    """
    Create a new page in the Notion Audit Log database.

    Args:
      finding  — written to Finding (Title)
      severity — written to Severity (Select). Pass "" to leave the field blank.
      details  — written to Details (Rich Text). Use a Notion URL for breaches,
                 or a plain description for approval events.

    Database must have these exact columns:
      Finding  (Title)
      Severity (Select — options: P0, P1, P2, Evolution)
      Details  (Rich Text)
      Date     (Date)
    """
    try:
        from notion_client import Client as NotionClient
        from notion_client.errors import APIResponseError
    except ImportError:
        raise RuntimeError(
            "notion-client is not installed. Run: pip install notion-client"
        )

    api_key = os.environ.get("NOTION_API_KEY")
    db_id   = os.environ.get("NOTION_DATABASE_ID")

    if not api_key or not db_id:
        raise EnvironmentError(
            "NOTION_API_KEY and NOTION_DATABASE_ID must be set as environment variables."
        )

    notion    = NotionClient(auth=api_key)
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    properties = _build_audit_properties(finding, severity, details, timestamp)

    try:
        notion.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
    except APIResponseError as e:
        # Print the exact payload so the rejected key-value pair is visible.
        print(f"❌ NOTION AUDIT LOG FAILED — HTTP {e.status}")
        print(f"   Payload sent:")
        for key, val in properties.items():
            print(f"     {key!r}: {val}")
        if e.status == 404:
            print(
                "   Cause: integration not invited to the database, or "
                "NOTION_DATABASE_ID is wrong."
            )
        raise


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------

def log_to_sheets(breaches: list[dict], risks: list[tuple]) -> None:
    """Append one row per breach to a Google Sheet audit log."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        raise RuntimeError(
            "gspread and google-auth are not installed. "
            "Run: pip install gspread google-auth"
        )

    creds_path = os.environ.get("GOOGLE_CREDS_JSON")
    sheet_id   = os.environ.get("GOOGLE_SHEETS_ID")

    if not creds_path or not sheet_id:
        raise EnvironmentError(
            "GOOGLE_CREDS_JSON and GOOGLE_SHEETS_ID must be set as environment variables."
        )

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds  = Credentials.from_service_account_file(creds_path, scopes=scopes)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(sheet_id).sheet1

    risk_map = {ref: (priority, rationale) for ref, priority, rationale in risks}
    timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    rows = []
    for breach in breaches:
        priority, rationale = risk_map.get(breach["id"], ("", ""))
        rows.append([
            timestamp,
            breach["id"],
            breach["rule"],
            breach["finding"],
            priority,
            rationale,
        ])

    sheet.append_rows(rows, value_input_option="USER_ENTERED")
