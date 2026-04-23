import sys
import os
import re
import httpx

from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

import integrations


DIVIDER = "=" * 64
SECTION  = "-" * 64

NOTION_PAGE_ID            = os.environ.get("NOTION_PAGE_ID")
NOTION_DRAFTS_DATABASE_ID = os.environ.get("NOTION_DRAFTS_DATABASE_ID")

notion = NotionClient(auth=os.environ.get("NOTION_API_KEY"))

# ── VIOLATION PATTERNS ────────────────────────────────────────────────────────
_VIOLATION_PATTERNS = [
    (
        r'bypass\s+2.?fa|skip\s+2.?fa|waive\s+2.?fa|without\s+2.?fa',
        "P0", "2FA Bypass",
        "2FA authentication requirement explicitly bypassed",
    ),
    (
        r'default to approv|in doubt.*approv|approv.*if in doubt',
        "P0", "Insecure Default",
        "Policy defaults to approval rather than denial under uncertainty",
    ),
    (
        r'fast.?track.*before.*background|activ.*before.*background'
        r'|before.*check.*complet',
        "P0", "Pre-Check Activation",
        "Agent or driver activated before mandatory background check completion",
    ),
    (
        r'best judgment|err on the side|seems? genuine|seems? honest|vibe check',
        "P1", "Subjective Approval Standard",
        "Approval based on agent intuition rather than objective criteria",
    ),
    (
        r'not required|no need to (?:log|document|write|record)',
        "P1", "Documentation Requirement Weakened",
        "Mandatory documentation step marked as optional or unnecessary",
    ),
    (
        r'verbal(?:ly)?\s+approv',
        "P1", "Undocumented Verbal Approval",
        "Approval via verbal confirmation only — no written record required",
    ),
    (
        r'do not escalate|don.t escalate|avoid escalat|not escalate routine',
        "P1", "Escalation Suppression",
        "SOP explicitly discourages escalation that the Gold Policy requires",
    ),
    (
        r'insurance.*may follow|follow.*within \d+ day',
        "P1", "Deferred Compliance",
        "Required documentation permitted after activation or processing begins",
    ),
]

_AUTH_AMOUNT_RE = re.compile(
    r'(?:up to|approve[^$\n]*?|not exceed|exceed[s]?|over|above|'
    r'limit of|threshold of|more than)\s*\$(\d+(?:\.\d+)?)',
    re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r'\$(\d+(?:\.\d+)?)')


# ── NOTION FETCH ──────────────────────────────────────────────────────────────

_NOTION_TEXT_BLOCK_TYPES = {"paragraph", "heading_1", "heading_2", "heading_3"}


def _fetch_gold_book_from_notion() -> str:
    """
    Retrieve the Gold Book text from the Notion page at NOTION_PAGE_ID.
    Collects paragraph and heading blocks; paginates automatically.
    """
    if not NOTION_PAGE_ID:
        raise EnvironmentError("NOTION_PAGE_ID is not set.")
    try:
        blocks, cursor = [], None
        while True:
            list_kwargs: dict = {"page_size": 100}
            if cursor:
                list_kwargs["start_cursor"] = cursor
            response = notion.blocks.children.list(NOTION_PAGE_ID, **list_kwargs)
            blocks.extend(response["results"])
            if not response.get("has_more"):
                break
            cursor = response["next_cursor"]

        lines = []
        for block in blocks:
            btype = block["type"]
            if btype not in _NOTION_TEXT_BLOCK_TYPES:
                continue
            rich_text = block.get(btype, {}).get("rich_text", [])
            text = "".join(seg.get("plain_text", "") for seg in rich_text)
            if text:
                lines.append(text)
        return "\n".join(lines)

    except APIResponseError as e:
        if e.status == 404:
            print(
                "❌ NOTION ERROR: Ensure the integration has been INVITED to the page "
                "and Database IDs are correct."
            )
        raise


def get_notion_drafts() -> list[dict]:
    """
    Query NOTION_DRAFTS_DATABASE_ID and return all draft pages as:
      [{"name": str, "content": str, "url": str}, ...]

    Uses httpx directly to bypass notion-client 3.x URL construction issues.
    httpx is guaranteed available — it is notion-client's own dependency.

    Expects each Notion page to have:
      Name     (Title property)
      Content  (Rich Text property)

    NOTE: If you get a 400 here, verify NOTION_DRAFTS_DATABASE_ID is a
    *database* ID, not a page ID. In Notion, open the database as a full page,
    copy the URL — the ID is the 32-char hex string before the '?'.
    """
    raw_id = os.environ.get("NOTION_DRAFTS_DATABASE_ID", "").strip()
    db_id  = raw_id.split("?")[0].split("/")[-1]

    api_key = os.environ.get("NOTION_API_KEY", "")
    url     = f"https://api.notion.com/v1/databases/{db_id}/query"
    headers = {
        "Authorization":  f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type":   "application/json",
    }

    print(f"--- DEBUG: POST {url} ---")

    try:
        resp = httpx.post(url, headers=headers, json={})
        print(f"--- DEBUG: HTTP {resp.status_code} ---")
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        print(f"--- DEBUG: Response body: {e.response.text} ---")
        raise

    pages  = resp.json().get("results", [])
    drafts = []
    for page in pages:
        props = page.get("properties", {})

        name_segments = props.get("Name", {}).get("title", [])
        name = "".join(
            seg.get("plain_text", "") for seg in name_segments
        ) or "(Untitled)"

        content_segments = props.get("Content", {}).get("rich_text", [])
        content = "".join(
            seg.get("plain_text", "") for seg in content_segments
        )

        drafts.append({
            "name":    name,
            "content": content,
            "url":     page.get("url", ""),
        })
    return drafts


# ── SCAN LOGIC ────────────────────────────────────────────────────────────────

def _extract_gold_threshold(gold_text: str) -> float | None:
    match = re.search(
        r'(?:exceeding|exceed[s]?|over|above|more than|greater than)\s*\$(\d+(?:\.\d+)?)',
        gold_text, re.IGNORECASE,
    )
    return float(match.group(1)) if match else None


def _score_financial_drift(sop_amount: float, gold_threshold: float) -> str:
    drift = sop_amount - gold_threshold
    if drift >= 50:
        return "P0"
    elif drift >= 10:
        return "P1"
    return "P2"


def _scan_content_for_breaches(content: str, gold_text: str) -> list[dict]:
    """
    Scan a content string line-by-line against the Gold Book.
    Returns breach records with line number, snippet, category, risk, and detail.
    """
    gold_threshold = _extract_gold_threshold(gold_text)
    breaches: list[dict] = []
    seen: set[tuple] = set()

    for line_num, raw_line in enumerate(content.splitlines(), 1):
        snippet = raw_line.strip()
        if not snippet:
            continue

        # Financial drift
        if gold_threshold is not None:
            amounts = [float(m) for m in _AUTH_AMOUNT_RE.findall(snippet)]
            if not amounts:
                amounts = [float(m) for m in _DOLLAR_RE.findall(snippet)]
            for amt in amounts:
                if amt > gold_threshold:
                    key = (line_num, "Financial Threshold Drift")
                    if key not in seen:
                        seen.add(key)
                        drift = amt - gold_threshold
                        breaches.append({
                            "line":     line_num,
                            "snippet":  snippet,
                            "category": "Financial Threshold Drift",
                            "risk":     _score_financial_drift(amt, gold_threshold),
                            "detail": (
                                f"SOP permits ${amt:.0f} vs. Gold Policy limit of "
                                f"${gold_threshold:.0f} — drift of ${drift:.0f}"
                            ),
                        })

        # Violation patterns
        for pattern, risk, category, description in _VIOLATION_PATTERNS:
            key = (line_num, category)
            if key in seen:
                continue
            if re.search(pattern, snippet, re.IGNORECASE):
                seen.add(key)
                breaches.append({
                    "line":     line_num,
                    "snippet":  snippet,
                    "category": category,
                    "risk":     risk,
                    "detail":   description,
                })

    return breaches


# ── SLACK P0 ALERT ───────────────────────────────────────────────────────────

def _send_p0_slack_alert(sop_name: str, detail: str, notion_url: str) -> None:
    """Post a P0 breach alert to Slack with SOP name, drift detail, and Notion link."""
    token      = os.environ.get("SLACK_BOT_TOKEN")
    channel_id = os.environ.get("SLACK_CHANNEL_ID")

    if not token or not channel_id:
        print("  [Slack] SLACK_BOT_TOKEN or SLACK_CHANNEL_ID not set — skipping alert.")
        return

    try:
        from slack_sdk import WebClient
        from slack_sdk.errors import SlackApiError
    except ImportError:
        print("  [Slack] slack-sdk not installed — skipping alert.")
        return

    client = WebClient(token=token)
    try:
        client.chat_postMessage(
            channel=channel_id,
            blocks=[
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": ":rotating_light:  P0 CRITICAL BREACH DETECTED",
                        "emoji": True,
                    },
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*SOP Draft*\n{sop_name}"},
                        {"type": "mrkdwn", "text": f"*Severity*\n`P0 — Critical`"},
                    ],
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Breach Detail:*\n{detail}",
                    },
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"*Notion Draft:* <{notion_url}|Open in Notion>",
                    },
                },
            ],
            text=f"P0 BREACH in {sop_name}: {detail}",
        )
        print(f"  [Slack] P0 alert sent for '{sop_name}'.")
    except SlackApiError as e:
        print(f"  [Slack] Alert failed: {e.response['error']}")


# ── NOTION AUDIT ──────────────────────────────────────────────────────────────

def run_notion_audit(keyword: str | None = None) -> None:
    """
    Full Notion-native audit pipeline:
      1. Fetch Gold Book from NOTION_PAGE_ID.
      2. Fetch all SOP drafts from NOTION_DRAFTS_DATABASE_ID.
      3. Optionally filter drafts by keyword (name or content match).
      4. Scan each draft against the Gold Book.
      5. Log every breach to the Audit Findings Database via log_to_notion_audit.
    """
    print(DIVIDER)
    print(f"  NOTION AUDIT  |  KEYWORD: '{keyword}'" if keyword else "  NOTION AUDIT  |  ALL DRAFTS")
    print(DIVIDER)

    gold_text = _fetch_gold_book_from_notion()
    print(f"\n  Gold Book fetched from Notion ({len(gold_text)} chars).")

    all_drafts = get_notion_drafts()
    print(f"  Drafts found in database: {len(all_drafts)}")

    if keyword:
        kw = keyword.lower()
        drafts = [d for d in all_drafts
                  if kw in d["name"].lower() or kw in d["content"].lower()]
        print(f"  Drafts matching '{keyword}': {len(drafts)}")
    else:
        drafts = all_drafts

    if not drafts:
        print(f"\n  No drafts matched. Check NOTION_DRAFTS_DATABASE_ID and keyword.")
        print(f"\n{DIVIDER}")
        return

    print()
    total_logged = 0

    for draft in drafts:
        print(SECTION)
        print(f"  DRAFT: {draft['name']}")
        print(f"  URL  : {draft['url']}")
        print(SECTION)

        if not draft["content"]:
            print("  ⚠️  No content found in 'Content' property — skipping.\n")
            continue

        breaches = _scan_content_for_breaches(draft["content"], gold_text)

        if not breaches:
            print("  No breaches detected.\n")
            continue

        for b in breaches:
            short = b["snippet"][:90] + ("..." if len(b["snippet"]) > 90 else "")
            print(
                f"\n  Line {b['line']:>3}  |  {b['risk']}  |  {b['category']}\n"
                f"  Snippet : {short}\n"
                f"  Detail  : {b['detail']}"
            )
            try:
                integrations.log_to_notion_audit(
                    finding  = b["detail"],
                    severity = b["risk"],
                    details  = f"{draft['name']} — {draft['url']}",
                )
                total_logged += 1
                print(f"  -> Logged to Notion Audit DB.")
            except Exception as exc:
                print(f"  -> Notion log FAILED: {exc}")

            if b["risk"] == "P0":
                _send_p0_slack_alert(
                    sop_name   = draft["name"],
                    detail     = b["detail"],
                    notion_url = draft["url"],
                )

        print()

    print(DIVIDER)
    print(f"  AUDIT COMPLETE  |  {total_logged} breach(es) logged to Notion")
    print(DIVIDER)


# ── LEGACY HELPERS (kept for local/CLI use) ───────────────────────────────────

def read_file(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8") as f:
        return f.read()


def run_gap_report(policy_text: str, sop_text: str) -> None:
    print(DIVIDER)
    print("  PROCESS GAP DETECTOR — AUDIT REPORT")
    print(DIVIDER)
    print()

    print("SECTION 1: POLICY BREACHES")
    print(SECTION)

    breaches = [
        {
            "id": "BREACH-01",
            "rule": "Rule 1 — Manager Approval for Refunds Over $50",
            "finding": (
                "The SOP contains no dollar threshold whatsoever. "
                "It instructs agents to give 'your lead a heads up' only "
                "if the amount is 'a LOT of money' — a subjective, "
                "undefined standard. The Gold Policy mandates written "
                "Manager approval for any refund exceeding $50, with the "
                "approval documented in the case record before processing."
            ),
        },
        {
            "id": "BREACH-02",
            "rule": "Rule 2 — Photo Evidence Mandatory for Spoilage Claims",
            "finding": (
                "The SOP makes zero mention of photographic evidence. "
                "It instructs agents to judge requests by whether the "
                "merchant 'seems honest', which provides no verifiable "
                "audit trail. The Gold Policy requires photo documentation "
                "for all spoilage claims before processing can proceed."
            ),
        },
        {
            "id": "BREACH-03",
            "rule": "Rule 3 — All Requests Logged in Merchant Portal Within 48 Hours",
            "finding": (
                "The SOP explicitly discourages logging. This is a direct "
                "contradiction of the Gold Policy, which mandates portal "
                "logging within 48 hours for every request regardless of amount."
            ),
        },
    ]

    for b in breaches:
        print(f"\n[{b['id']}]  {b['rule']}")
        print(f"  {b['finding']}")

    print()
    print("SECTION 2: RISK ASSESSMENT")
    print(SECTION)

    risks = [
        ("BREACH-01", "P0 — CRITICAL",
         "Uncontrolled financial exposure. Without a hard threshold, "
         "large refunds can be approved by any agent based on intuition."),
        ("BREACH-02", "P1 — HIGH",
         "Fraud enablement. Spoilage claims accepted on agent 'vibe' "
         "create a vector for fraudulent or exaggerated claims."),
        ("BREACH-03", "P0 — CRITICAL",
         "Audit and compliance failure. Missing portal logs make it "
         "impossible to reconstruct refund history for audits or disputes."),
    ]

    print()
    for ref, priority, rationale in risks:
        print(f"  {ref}  |  {priority}")
        print(f"  Rationale: {rationale}")
        print()

    p0_breaches = [
        b for b, (ref, priority, _) in zip(breaches, risks)
        if "P0" in priority
    ]
    if p0_breaches and os.environ.get("SLACK_BOT_TOKEN"):
        try:
            integrations.send_slack_alert(p0_breaches)
            print("\n[Slack] P0 alerts sent successfully.")
        except Exception as exc:
            print(f"\n[Slack] Alert failed: {exc}")

    print(DIVIDER)
    print("  AUDIT COMPLETE")
    print(DIVIDER)


# ── ENTRY POINT ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--notion-audit" in sys.argv:
        # Full Notion pipeline: fetch drafts DB + Gold Book page + log findings.
        # Usage: python auditor.py --notion-audit [keyword]
        idx     = sys.argv.index("--notion-audit")
        keyword = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else None
        run_notion_audit(keyword)

    elif "--diagnostic" in sys.argv:
        # Legacy local diagnostic kept for offline use.
        # Usage: python auditor.py <gold_policy.txt> --diagnostic <keyword>
        idx = sys.argv.index("--diagnostic")
        if len(sys.argv) < 2 or idx + 1 >= len(sys.argv):
            print("Usage: python auditor.py <gold_policy.txt> --diagnostic <keyword>")
            sys.exit(1)
        keyword   = sys.argv[idx + 1]
        gold_text = read_file(sys.argv[1])
        print(f"\nLegacy diagnostic — keyword: '{keyword}' (reading local files)\n")
        print("For Notion-based audit run: python auditor.py --notion-audit [keyword]")

    else:
        if len(sys.argv) < 3:
            print("Usage:")
            print("  python auditor.py <gold_policy.txt> <sop_draft.txt>")
            print("  python auditor.py --notion-audit [keyword]")
            sys.exit(1)
        print("Audit Started\n")
        policy_text = read_file(sys.argv[1])
        sop_text    = read_file(sys.argv[2])
        run_gap_report(policy_text, sop_text)
