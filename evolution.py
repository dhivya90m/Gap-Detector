"""
evolution.py — Proactive Policy Guard via Slack Socket Mode.

Every channel message passes Gate 1 (trigger keywords) and Gate 2 (domain
keywords). When both fire, Gemini classifies the message into one of two types:

  INSTRUCTION  "We should do X instead of Y"
               → Posts a Proposed Policy Update with [Approve] / [Reject] buttons.
               → On Approve, Gemini surgically edits the Gold Book on Notion.

  DEVIATION    "I am doing X even though the rule says Y"
               → Posts a P0 Drift Warning showing the gap between the
                 conversation and the Gold Book, with estimated Revenue Leak.

Required env vars:
  SLACK_BOT_TOKEN  — Bot OAuth token (xoxb-...)
  SLACK_APP_TOKEN  — App-level token for Socket Mode (xapp-...)
  GEMINI_API_KEY   — Google AI API key
  NOTION_API_KEY   — Notion internal integration token (secret_...)
  NOTION_PAGE_ID   — ID of the Notion page that stores the Gold Book

Required Slack OAuth scopes (Bot Token):
  chat:write, channels:history, groups:history

Required Slack app settings:
  Socket Mode: Enabled
  Event Subscriptions: message.channels, message.groups
"""

import os
import re
import json
import datetime
from google import genai
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from notion_client import Client as NotionClient
from notion_client.errors import APIResponseError

import integrations

GOLD_POLICY_PATH = "refund_policy_gold.txt"  # local fallback for auditor.py
NOTION_PAGE_ID   = os.environ.get("NOTION_PAGE_ID")

app    = App(token=os.environ["SLACK_BOT_TOKEN"])
llm    = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
notion = NotionClient(auth=os.environ.get("NOTION_API_KEY"))

_SOP_MAP = {
    "Refund":     "drafts/SOP-FIN-002_Refund_Claims.txt",
    "Safety":     "drafts/SOP-LOG-001_Driver_Onboarding.txt",
    "Onboarding": "drafts/SOP-LOG-001_Driver_Onboarding.txt",
    "Credential": "drafts/SOP-SEC-003_Credential_Handling.txt",
}


# ---------------------------------------------------------------------------
# Notion — fetch Gold Book, build rule map, surgical edit, changelog
# ---------------------------------------------------------------------------

_NOTION_TEXT_BLOCK_TYPES = {"paragraph", "heading_1", "heading_2", "heading_3"}
_CHANGELOG_PREFIX        = "Last Policy Update:"


def _get_notion_blocks_raw() -> list[dict]:
    """Fetch all blocks from NOTION_PAGE_ID, returning full objects with IDs."""
    if not NOTION_PAGE_ID:
        raise EnvironmentError("NOTION_PAGE_ID is not set.")
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
    return blocks


def get_notion_gold_book() -> str:
    """
    Return the Gold Book as plain text for Gemini analysis.
    Skips the changelog line — it is metadata, not policy content.
    """
    try:
        blocks = _get_notion_blocks_raw()
        lines  = []
        for block in blocks:
            btype = block["type"]
            if btype not in _NOTION_TEXT_BLOCK_TYPES:
                continue
            rich_text = block.get(btype, {}).get("rich_text", [])
            text      = "".join(seg.get("plain_text", "") for seg in rich_text)
            if text and not text.startswith(_CHANGELOG_PREFIX):
                lines.append(text)
        return "\n".join(lines)
    except APIResponseError as e:
        if e.status == 404:
            print(
                "❌ NOTION ERROR: Ensure the integration has been INVITED to the page "
                "and Database IDs are correct."
            )
        raise


def _get_rule_block_map() -> dict:
    """
    Scan blocks and return a map of rule number → block IDs.

    Returns: {
      "1": {"heading_id": str, "paragraph_id": str | None, "paragraph_text": str},
      ...
    }
    """
    blocks       = _get_notion_blocks_raw()
    rule_map: dict = {}
    current_rule   = None

    for block in blocks:
        btype = block["type"]
        if btype not in _NOTION_TEXT_BLOCK_TYPES:
            continue
        rich_text = block.get(btype, {}).get("rich_text", [])
        text      = "".join(seg.get("plain_text", "") for seg in rich_text)

        rule_match = re.search(r'\bRULE\s+(\d+)\b', text, re.IGNORECASE)
        if rule_match and btype in ("heading_1", "heading_2", "heading_3"):
            current_rule = rule_match.group(1)
            rule_map[current_rule] = {
                "heading_id":     block["id"],
                "heading_text":   text,
                "paragraph_id":   None,
                "paragraph_text": "",
            }
        elif current_rule and btype == "paragraph" and text:
            entry = rule_map[current_rule]
            if entry["paragraph_id"] is None:
                entry["paragraph_id"]   = block["id"]
                entry["paragraph_text"] = text
            else:
                entry["paragraph_text"] += "\n" + text

    print(f"[Guard] Rule block map: { {k: v['heading_text'][:50] for k, v in rule_map.items()} }")
    return rule_map


def _update_changelog_block(approver: str) -> None:
    """
    Find the single 'Last Policy Update:' block and update it in place.
    Appends a new one at the bottom if none exists yet.
    """
    timestamp      = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    changelog_text = f"{_CHANGELOG_PREFIX} {timestamp} — approved by @{approver}"

    try:
        blocks       = _get_notion_blocks_raw()
        changelog_id = None
        for block in reversed(blocks):
            btype = block["type"]
            if btype not in _NOTION_TEXT_BLOCK_TYPES:
                continue
            rt   = block.get(btype, {}).get("rich_text", [])
            text = "".join(seg.get("plain_text", "") for seg in rt)
            if text.startswith(_CHANGELOG_PREFIX):
                changelog_id = block["id"]
                break

        if changelog_id:
            notion.blocks.update(
                changelog_id,
                paragraph={"rich_text": [{"type": "text", "text": {"content": changelog_text}}]},
            )
            print(f"[Guard] Changelog updated: {changelog_text}")
        else:
            notion.blocks.children.append(
                block_id=NOTION_PAGE_ID,
                children=[{
                    "object": "block",
                    "type":   "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": changelog_text}}]
                    },
                }],
            )
            print(f"[Guard] Changelog block created: {changelog_text}")
    except Exception as exc:
        print(f"[Guard] ❌ Changelog update failed: {exc}")


def _surgical_update_notion(
    action: str,
    rule_entry: dict | None,
    new_rule_text: str,
    approver: str,
    new_rule_num: str = "",
    new_rule_title: str = "",
) -> None:
    """
    Write the approved change to Notion using one of three modes:
      replace   — overwrite the existing rule paragraph block in place
      append    — add the new provision as a sub-bullet to the existing block
      new_rule  — append a heading_3 + paragraph pair for a brand-new rule
    Updates the single changelog block at the bottom regardless.
    """
    if not NOTION_PAGE_ID:
        return

    try:
        if action == "new_rule":
            _append_new_rule_block(new_rule_num, new_rule_title, new_rule_text)

        elif action == "append" and rule_entry and rule_entry.get("paragraph_id"):
            combined = rule_entry["paragraph_text"].rstrip() + "\n• " + new_rule_text.strip()
            notion.blocks.update(
                rule_entry["paragraph_id"],
                paragraph={"rich_text": [{"type": "text", "text": {"content": combined[:2000]}}]},
            )
            print("[Guard] Sub-bullet appended to existing rule block ✓")

        elif action == "replace" and rule_entry and rule_entry.get("paragraph_id"):
            notion.blocks.update(
                rule_entry["paragraph_id"],
                paragraph={"rich_text": [{"type": "text", "text": {"content": new_rule_text[:2000]}}]},
            )
            print("[Guard] Rule paragraph block replaced in place ✓")

        else:
            print(f"[Guard] No paragraph block ID for action '{action}' — appending fallback.")
            _append_rule_fallback(new_rule_text)

    except Exception as exc:
        print(f"[Guard] ❌ Surgical update failed: {exc} — appending fallback.")
        _append_rule_fallback(new_rule_text)

    _update_changelog_block(approver)


def _append_rule_fallback(new_rule_text: str) -> None:
    """Append the new rule text as a plain paragraph — used when block update fails."""
    chunks = [new_rule_text[i:i + 2000] for i in range(0, len(new_rule_text), 2000)]
    notion.blocks.children.append(
        block_id=NOTION_PAGE_ID,
        children=[{
            "object": "block",
            "type":   "paragraph",
            "paragraph": {"rich_text": [{"type": "text", "text": {"content": chunk}}]},
        } for chunk in chunks],
    )
    print(f"[Guard] Fallback append: {len(chunks)} block(s) added.")


def _append_new_rule_block(rule_num: str, title: str, content: str) -> None:
    """Append a heading_3 + paragraph pair for a brand-new rule."""
    notion.blocks.children.append(
        block_id=NOTION_PAGE_ID,
        children=[
            {
                "object": "block",
                "type":   "heading_3",
                "heading_3": {
                    "rich_text": [{"type": "text", "text": {"content": f"RULE {rule_num}: {title}"}}]
                },
            },
            {
                "object": "block",
                "type":   "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
                },
            },
        ],
    )
    print(f"[Guard] New RULE {rule_num} '{title}' appended to Gold Book ✓")


# ---------------------------------------------------------------------------
# Domain gate — only gate needed; Gemini handles intent classification
# ---------------------------------------------------------------------------

_DOMAIN_PATTERNS: list[tuple[str, str]] = [
    (r"\brefunds?\b",       "Refund"),     # matches 'refund' and 'refunds'
    (r"\bthreshold\b",      "Refund"),
    (r"\$\d+",              "Refund"),     # no \b — $ is not a word char
    (r"\bonboarding\b",     "Onboarding"),
    (r"\bsafety\b",         "Safety"),
    (r"\bbackground\b",     "Safety"),
    (r"\bdrivers?\b",       "Safety"),
    (r"\bactivat\w*\b",     "Safety"),
    (r"\b2fa\b",            "Credential"),
    (r"\bauthentication\b", "Credential"),
    (r"\bcredential\b",     "Credential"),
    (r"\bbypass\b",         "Credential"),
]


def _passes_gates(text: str) -> tuple[bool, str | None]:
    """Single domain gate — returns (passed, domain). Gemini classifies intent."""
    for pattern, domain in _DOMAIN_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True, domain
    return False, None


# ---------------------------------------------------------------------------
# Intelligence layer — Gemini classifies type + performs gap analysis
# ---------------------------------------------------------------------------

def _analyse_with_gemini(message: str, gold_policy: str) -> dict:
    """
    Single Gemini call. Returns a dict with:
      message_type      — "instruction" | "deviation" | "other"
      is_contradiction  — bool
      domain            — Refund | Safety | Onboarding | Credential | Other
      proposed_change   — one-sentence description
      contradicted_rule — rule name/number
      gap               — e.g. "$25 Gold Book vs $100 proposed"
      revenue_leak      — e.g. "$3.75 per contact"
      severity          — P0 | P1 | P2
    """
    prompt = (
        "You are a compliance analyst for Global Retail.\n\n"

        "GOLD BOOK (source of truth):\n"
        f"{gold_policy}\n\n"

        "SLACK MESSAGE:\n"
        f'"""{message}"""\n\n'

        "Step 1 — Classify the message type:\n"
        "  'instruction' — the speaker is proposing that the policy SHOULD BE changed\n"
        "                  (e.g. 'We should lower the limit', 'Let us update the rule')\n"
        "  'deviation'   — the speaker is describing that they ARE or HAVE acted\n"
        "                  outside the current policy right now\n"
        "                  (e.g. 'I approved it even though', 'We bypassed the check')\n"
        "  'other'       — neither of the above\n\n"

        "Step 2 — If type is 'instruction' or 'deviation', compare the message against\n"
        "the Gold Book rules and identify any contradiction.\n"
        "  Gap: show the exact difference (e.g. '$25 Gold Book vs $100 proposed').\n"
        "  Revenue Leak: estimate per-contact impact using 5% approval rate on dollar\n"
        "  drift (e.g. $75 drift x 5% = $3.75 per contact). For non-financial rules\n"
        "  use 'Liability / Security risk — not quantifiable'.\n"
        "  Severity: P0 if safety-critical or drift >= $50; P1 if drift $10-$49; "
        "P2 if < $10.\n\n"

        "Step 3 — If type is 'instruction', check whether the proposed change is ALREADY\n"
        "fully reflected in the current Gold Book text. If the Gold Book already states\n"
        "exactly what the message proposes, set is_already_aligned to true.\n\n"

        "Respond with a JSON object only — no markdown fences:\n"
        "{\n"
        '  "message_type": "instruction or deviation or other",\n'
        '  "is_contradiction": true or false,\n'
        '  "is_already_aligned": true or false,\n'
        '  "domain": "Refund or Safety or Onboarding or Credential or Other",\n'
        '  "proposed_change": "one sentence — what is being changed or done",\n'
        '  "contradicted_rule": "rule name or empty string",\n'
        '  "gap": "e.g. $25 Gold Book vs $100 proposed, or empty string",\n'
        '  "revenue_leak": "e.g. $3.75 per contact, or empty string",\n'
        '  "severity": "P0 or P1 or P2 or empty string"\n'
        "}"
    )

    response = llm.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    raw = response.text.strip().strip("```json").strip("```").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message_type": "other", "is_contradiction": False, "domain": "Other"}


# ---------------------------------------------------------------------------
# Gemini — apply approved instruction to the Gold Book
# ---------------------------------------------------------------------------

def _generate_rule_edit(rule_text: str, instruction: str) -> str:
    """
    Ask Gemini to generate ONLY the modified paragraph for a single rule.
    Does not receive or return the full policy — prevents duplicate master books.
    """
    prompt = (
        "You are a policy rule editor for Global Retail.\n"
        "Generate ONLY the new rule body as a single concise paragraph.\n"
        "Do NOT rewrite the whole policy document.\n"
        "Do NOT include rule headers, rule numbers, dividers, or preamble.\n"
        "Return ONLY the updated paragraph text — nothing else.\n\n"
        f"CURRENT RULE TEXT:\n{rule_text}\n\n"
        f"CHANGE INSTRUCTION:\n{instruction}\n\n"
        "Return ONLY the updated rule paragraph."
    )
    print("[Guard] Calling Gemini for single-rule edit...")
    response = llm.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=prompt,
    )
    print("[Guard] Gemini responded.")
    result = response.text.strip()
    print(f"[Guard] New rule text preview: {result[:120]}...")
    return result


def _resolve_target_rule(instruction: str, rule_map: dict) -> dict:
    """
    When no direct rule number match is found, ask Gemini to pick the closest
    existing rule or declare the proposal a brand-new topic.

    Returns: {"action": "replace"|"append"|"new_rule", "rule_num": "N", "new_rule_title": "..."}
      replace   — proposal directly changes an existing rule's core logic
      append    — proposal adds a new provision or exception to an existing rule
      new_rule  — proposal covers a topic not addressed by any existing rule
    """
    rules_summary = "\n".join(
        f"RULE {num}: {entry['heading_text']}"
        for num, entry in sorted(rule_map.items())
    )
    next_num = str(max((int(k) for k in rule_map), default=0) + 1)

    prompt = (
        "You are a policy rule resolver for Global Retail.\n\n"
        f"EXISTING RULES:\n{rules_summary}\n\n"
        f"PROPOSED CHANGE:\n{instruction}\n\n"
        "Pick ONE action:\n"
        "  'replace'  — the proposal directly modifies an existing rule's core logic\n"
        "  'append'   — the proposal adds a new provision or exception to an existing rule\n"
        "  'new_rule' — the proposal covers a topic not addressed by any existing rule\n\n"
        "If 'replace' or 'append', set rule_num to the number of the best matching rule.\n"
        f"If 'new_rule', set rule_num to '{next_num}' and provide a 3-6 word new_rule_title.\n\n"
        "Respond with JSON only — no markdown fences:\n"
        '{"action": "replace or append or new_rule", "rule_num": "N", "new_rule_title": ""}'
    )

    print("[Guard] Calling Gemini to resolve target rule...")
    response = llm.models.generate_content(model="gemini-2.5-flash-lite", contents=prompt)
    raw = response.text.strip().strip("```json").strip("```").strip()
    try:
        result = json.loads(raw)
        result.setdefault("action", "new_rule")
        result.setdefault("rule_num", next_num)
        result.setdefault("new_rule_title", "New Policy Rule")
        print(f"[Guard] Resolver result: {result}")
        return result
    except json.JSONDecodeError:
        print("[Guard] Resolver JSON parse failed — defaulting to new_rule.")
        return {"action": "new_rule", "rule_num": next_num, "new_rule_title": "New Policy Rule"}


# ---------------------------------------------------------------------------
# Slack output — DEVIATION path: P0 Drift Warning
# ---------------------------------------------------------------------------

def _post_drift_warning(
    client,
    channel: str,
    thread_ts: str,
    alert: dict,
    sop_path: str,
) -> None:
    severity     = alert.get("severity", "P0")
    domain       = alert.get("domain", "Unknown")
    change       = alert.get("proposed_change", "Active policy deviation detected.")
    rule         = alert.get("contradicted_rule", "Gold Book rule")
    gap          = alert.get("gap", "See Gold Book for details.")
    revenue_leak = alert.get("revenue_leak", "")

    leak_line = (
        f"*Estimated Revenue Leak:* `{revenue_leak}`"
        if revenue_leak
        else "*Risk Type:* Liability / Security — not quantifiable per contact"
    )

    client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        blocks=[
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": ":rotating_light:  ACTIVE DEVIATION — P0 Drift Warning",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Severity*\n`{severity}`"},
                    {"type": "mrkdwn", "text": f"*Domain*\n{domain}"},
                    {"type": "mrkdwn", "text": f"*Violated Rule*\n{rule}"},
                    {"type": "mrkdwn", "text": f"*Affected SOP*\n`{sop_path}`"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*What is happening:*\n{change}\n\n"
                        f"*Gap vs Gold Book:*\n`{gap}`\n\n"
                        f"{leak_line}"
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            ":shield: This is an active deviation from the Gold Book, not a "
                            "proposal. Escalate to the Policy Review Board immediately."
                        ),
                    }
                ],
            },
        ],
        text=(
            f"ACTIVE DEVIATION [{severity}]: {change} | "
            f"Violates {rule} | Gap: {gap}"
        ),
    )


# ---------------------------------------------------------------------------
# Button: Approve — apply the change to the Gold Book via Gemini
# ---------------------------------------------------------------------------

@app.action("approve_change")
def handle_approve(ack, body, client):
    ack()

    # Decode button payload — contains both the change text and which rule is affected.
    raw_value = body["actions"][0]["value"]
    try:
        payload          = json.loads(raw_value)
        instruction      = payload.get("change", raw_value)
        contradicted_rule = payload.get("contradicted_rule", "")
    except (json.JSONDecodeError, TypeError):
        instruction      = raw_value
        contradicted_rule = ""

    channel    = body["container"]["channel_id"]
    message_ts = body["container"]["message_ts"]
    thread_ts  = body["message"].get("thread_ts", message_ts)
    approver   = body["user"]["name"]

    print(f"\n[Guard] Approve by @{approver} | Rule: '{contradicted_rule}' | Change: {instruction[:80]}")

    # 1. Fetch live Gold Book and build rule block map from Notion.
    try:
        rule_map   = _get_rule_block_map()
        rule_num   = re.search(r'\d+', contradicted_rule)
        rule_num   = rule_num.group(0) if rule_num else None
        rule_entry = rule_map.get(rule_num) if rule_num else None

        action         = "replace"
        new_rule_num   = ""
        new_rule_title = ""

        if rule_entry is None:
            # No direct number match — ask Gemini to find the closest rule or create a new one.
            print(f"[Guard] No direct match for '{contradicted_rule}' — resolving via Gemini...")
            resolved       = _resolve_target_rule(instruction, rule_map)
            action         = resolved.get("action", "new_rule")
            resolved_num   = resolved.get("rule_num", "")
            new_rule_title = resolved.get("new_rule_title", "New Policy Rule")

            if action in ("replace", "append"):
                rule_entry = rule_map.get(resolved_num)
                if rule_entry is None:
                    # Resolved number still absent — treat as new rule.
                    action       = "new_rule"
                    new_rule_num = resolved_num
            else:
                new_rule_num = resolved_num

        current_rule_text = rule_entry["paragraph_text"] if rule_entry else instruction

    except Exception as exc:
        print(f"[Guard] ❌ Gold Book fetch FAILED: {exc}")
        return

    # 2. Ask Gemini to generate ONLY the modified rule paragraph.
    try:
        new_rule_text = _generate_rule_edit(current_rule_text, instruction)
    except Exception as exc:
        print(f"[Guard] ❌ Gemini rule edit FAILED: {exc}")
        return

    # 3. Surgical block update on Notion — replace, append sub-bullet, or create new rule.
    try:
        _surgical_update_notion(
            action         = action,
            rule_entry     = rule_entry,
            new_rule_text  = new_rule_text,
            approver       = approver,
            new_rule_num   = new_rule_num,
            new_rule_title = new_rule_title,
        )
    except Exception as exc:
        print(f"[Guard] ❌ Notion surgical update FAILED: {exc}")

    # 4. Log to Audit DB — standardised payload matching auditor.py schema.
    try:
        rule_label = contradicted_rule if contradicted_rule else "Policy"
        integrations.log_to_notion_audit(
            finding  = f"Policy Evolution: {rule_label} Updated",
            severity = "",   # blank — Severity is irrelevant for approvals
            details  = f"Approved by @{approver} via Slack interaction. Change: {instruction}",
        )
        print("[Guard] Policy approval logged to Notion Audit DB.")
    except Exception as exc:
        print(f"[Guard] Audit DB log failed: {exc}")

    # 5. Replace Slack card with final confirmation.
    client.chat_update(
        channel=channel,
        ts=message_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":white_check_mark: *Change Approved.*\n"
                        "Notion Gold Book updated and log entry created in Audit DB."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"Approved by *@{approver}* · Change: _{instruction}_",
                    }
                ],
            },
        ],
        text="✅ Change Approved. Notion Gold Book updated and log entry created in Audit DB.",
    )


# ---------------------------------------------------------------------------
# Button: Reject — dismiss with no file changes
# ---------------------------------------------------------------------------

@app.action("reject_change")
def handle_reject(ack, body, client):
    ack()

    channel    = body["container"]["channel_id"]
    message_ts = body["container"]["message_ts"]
    rejector   = body["user"]["name"]

    print(f"\n[Guard] Rejected by @{rejector} — Gold Book unchanged.")

    client.chat_update(
        channel=channel,
        ts=message_ts,
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f":x: *Policy Update Rejected by @{rejector}*\n"
                        "The Gold Book was not modified."
                    ),
                },
            }
        ],
        text="Policy update rejected. No changes made.",
    )


# ---------------------------------------------------------------------------
# Helpers — state awareness
# ---------------------------------------------------------------------------

def _extract_amounts(text: str) -> list[str]:
    """Return every dollar amount string found in text, e.g. ['$50', '$100']."""
    return re.findall(r'\$\d+(?:\.\d+)?', text)


def _already_reflected(amounts: list[str], gold_policy: str) -> bool:
    """
    True when every dollar amount in the message already appears in the Gold Book
    (checked as both '$50' and '$50.00' to handle formatting differences).
    """
    if not amounts:
        return False
    for raw in amounts:
        value = float(raw.lstrip('$'))
        short = f'${int(value)}'          # e.g. '$50'
        long  = f'${value:.2f}'           # e.g. '$50.00'
        if short not in gold_policy and long not in gold_policy:
            return False
    return True


# ---------------------------------------------------------------------------
# Single message listener — all routing logic lives here
# ---------------------------------------------------------------------------

@app.message(re.compile(".*"))
def handle_message(body, event, client):
    # Bolt routes subtype events (edits, deletes) here too — drop them.
    if body.get("event", {}).get("subtype") is not None:
        return

    print(f"\n[Guard] Message received: {event.get('text', '')[:80]}")

    # Ignore bot-posted messages to prevent feedback loops.
    if event.get("bot_id"):
        return

    # Pull text from the event payload directly.
    text = body.get("event", {}).get("text", "").strip()
    if not text:
        return

    # Gate 1 + Gate 2 — regex and domain check.
    passed, domain = _passes_gates(text)
    if not passed:
        return

    print(f"\n[Guard] Gates passed | Domain hint: {domain} | Message: {text[:100]}")

    # Read the current Gold Book from Notion — wrapped so a fetch failure
    # prints the exact error instead of killing the handler silently.
    try:
        gold_policy = get_notion_gold_book()
        print(f"[Guard] Gold Book fetched ({len(gold_policy)} chars).")
    except Exception as exc:
        print(f"[Guard] ❌ Gold Book fetch FAILED: {exc}")
        print("[Guard] Check NOTION_PAGE_ID and that the integration is invited to the page.")
        return

    # State awareness — check before calling Gemini.
    amounts = _extract_amounts(text)
    if amounts and _already_reflected(amounts, gold_policy):
        print(f"[Guard] Amounts {amounts} already in Gold Book — no action needed.")
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event.get("thread_ts", event["ts"]),
            text=":white_check_mark: This is already reflected in the Gold Book.",
        )
        return

    # Intelligence layer — classify and gap-analyse with Gemini.
    alert    = _analyse_with_gemini(text, gold_policy)
    msg_type = alert.get("message_type", "other")

    print(f"[Guard] Gemini: type={msg_type} | "
          f"contradiction={alert.get('is_contradiction')} | "
          f"aligned={alert.get('is_already_aligned')} | "
          f"domain={alert.get('domain')} | gap={alert.get('gap')}")

    # Gemini-level alignment check (catches non-dollar cases).
    if msg_type == "instruction" and alert.get("is_already_aligned"):
        print("[Guard] Gemini confirms Gold Book already aligned — notifying channel.")
        client.chat_postMessage(
            channel=event["channel"],
            thread_ts=event.get("thread_ts", event["ts"]),
            text=":white_check_mark: This is already reflected in the Gold Book.",
        )
        return

    if msg_type == "other" or not alert.get("is_contradiction"):
        print("[Guard] No actionable contradiction — standing down.")
        return

    effective_domain = alert.get("domain", domain)
    sop_path = _SOP_MAP.get(effective_domain)
    if not sop_path:
        print(f"[Guard] No SOP mapped for domain '{effective_domain}'.")
        return

    channel   = event["channel"]
    thread_ts = event.get("thread_ts", event["ts"])

    if msg_type == "instruction":
        print("[Guard] INSTRUCTION — posting Proposed Update with Approve/Reject.")
        alert["sop_path"] = sop_path
        integrations.post_proposed_change(
            client    = client,
            channel   = channel,
            thread_ts = thread_ts,
            alert     = alert,
            gold_text = gold_policy,   # Notion-fetched content — no local file read
        )

    elif msg_type == "deviation":
        print("[Guard] DEVIATION — posting P0 Drift Warning.")
        _post_drift_warning(
            client    = client,
            channel   = channel,
            thread_ts = thread_ts,
            alert     = alert,
            sop_path  = sop_path,
        )


# ---------------------------------------------------------------------------
# Subtype sink — must be registered AFTER @app.message so Bolt routes new
# messages to the logic handler first. This only fires for message_deleted,
# message_changed, etc. which @app.message never receives.
# ---------------------------------------------------------------------------

@app.event("message")
def handle_message_subtypes(body, logger):
    pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not os.environ.get("SLACK_APP_TOKEN"):
        raise EnvironmentError("SLACK_APP_TOKEN must be set to run Socket Mode.")
    if not os.environ.get("GEMINI_API_KEY"):
        raise EnvironmentError("GEMINI_API_KEY must be set for Gemini analysis.")

    print("Proactive Policy Guard active — monitoring all channel messages...")
    SocketModeHandler(app, app_token=os.environ["SLACK_APP_TOKEN"]).start()
