# AI-Driven Process Gap Detector — Notion Engine

> A fully automated compliance enforcement system that monitors Slack conversations in real time, detects policy deviations, proposes and surgically applies Gold Book updates via Gemini, and logs every finding and approval to Notion — with zero manual file management.

---

## Table of Contents

1. [What This System Does](#what-this-system-does)
2. [Architecture Overview](#architecture-overview)
3. [How Each Component Works](#how-each-component-works)
   - [auditor.py — Scheduled Gap Audit](#auditorpy--scheduled-gap-audit)
   - [evolution.py — Proactive Policy Guard](#evolutionpy--proactive-policy-guard)
   - [integrations.py — Shared Notification Layer](#integrationspy--shared-notification-layer)
4. [The Intelligence Layer (Gemini)](#the-intelligence-layer-gemini)
5. [The Gold Book (Notion-Native)](#the-gold-book-notion-native)
6. [Slack Bot Flows](#slack-bot-flows)
   - [INSTRUCTION Flow](#instruction-flow)
   - [DEVIATION Flow](#deviation-flow)
   - [Approve Flow](#approve-flow)
   - [Reject Flow](#reject-flow)
7. [Surgical Rule Matching](#surgical-rule-matching)
8. [Notion Audit Log](#notion-audit-log)
9. [Project Structure](#project-structure)
10. [Environment Variables](#environment-variables)
11. [Notion Setup](#notion-setup)
12. [Slack App Setup](#slack-app-setup)
13. [Running the System](#running-the-system)
14. [End-to-End Example](#end-to-end-example)
15. [Design Decisions](#design-decisions)

---

## What This System Does

Operations teams generate hundreds of Slack messages daily. Hidden inside those messages are two dangerous signals:

| Signal | Example | Risk |
|---|---|---|
| **Policy Proposal** | "We should raise the refund limit to $200" | Rule changes applied without governance |
| **Active Deviation** | "I approved the $300 refund even though we're only allowed $150" | Live breach of Gold Book rules |

This system intercepts both signals automatically:

- **Auditor** (`auditor.py`) runs on demand or on a schedule. It fetches every SOP draft from a Notion database and scans each one against the Gold Book, firing a Slack P0 alert and writing an audit log entry for every breach found.
- **Policy Guard** (`evolution.py`) runs 24/7 as a Slack Socket Mode bot. Every message in the monitored channel passes through a two-stage gate (regex domain filter → Gemini intent classifier). When a policy signal is detected, the bot posts an interactive Approve/Reject card. On approval, Gemini generates only the changed rule paragraph, and the system surgically updates that single Notion block — no full-document rewrites, no duplicate content.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Slack Channel                            │
│   User types: "We should skip 2FA for temp access tokens"       │
└────────────────────────┬────────────────────────────────────────┘
                         │  Socket Mode (real-time)
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│  evolution.py — Proactive Policy Guard                          │
│                                                                 │
│  Gate 1: Domain regex (2fa, credential, bypass → "Credential") │
│  Gate 2: Gemini classifies → "instruction" / "deviation"       │
│                                                                 │
│  INSTRUCTION path:                                              │
│    Post Approve/Reject card with contextual snippet             │
│    On Approve →                                                 │
│      _resolve_target_rule() → "append to Rule 3"               │
│      _generate_rule_edit() → new paragraph text                 │
│      _surgical_update_notion() → update single Notion block     │
│      log_to_notion_audit() → Audit DB entry                     │
│                                                                 │
│  DEVIATION path:                                                │
│    Post P0 Drift Warning (no Notion write)                      │
└────────────────────┬──────────────────────────────────────────--┘
                     │
          ┌──────────┴──────────┐
          ▼                     ▼
  ┌───────────────┐    ┌────────────────────┐
  │  Notion       │    │  Notion            │
  │  Gold Book    │    │  Audit Log DB      │
  │  (Page)       │    │  (Database)        │
  └───────────────┘    └────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  auditor.py — Scheduled Compliance Scan                         │
│                                                                 │
│  Fetch Gold Book text from Notion (blocks API)                  │
│  Fetch all SOP drafts from Notion Drafts DB (httpx)             │
│  Scan each SOP against Gold Book rules with Gemini              │
│  → Log breach to Notion Audit DB                                │
│  → Fire Slack P0 block card if severity == P0                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## How Each Component Works

### auditor.py — Scheduled Gap Audit

The auditor is a one-shot script (run it manually or via cron/scheduler) that:

1. **Fetches the Gold Book** from the Notion page identified by `NOTION_PAGE_ID`. It calls `notion.blocks.children.list()` with pagination and assembles plain text from all paragraph and heading blocks, skipping the changelog line.

2. **Fetches SOP Drafts** from the Notion database identified by `NOTION_DRAFTS_DATABASE_ID`. Because `notion-client` v3 removed `databases.query()`, this call is made directly via `httpx.post` to `https://api.notion.com/v1/databases/{id}/query`. Each SOP is read as a dict with `name`, `content`, and `url` fields.

3. **Scans for breaches** using a Gemini prompt (`_scan_content_for_breaches`). The prompt receives the SOP content and the Gold Book text, and returns a JSON array of breach objects with `detail`, `rule`, and `risk` (P0/P1/P2) fields.

4. **Logs each breach** to the Notion Audit Log database via `integrations.log_to_notion_audit()`.

5. **Fires a Slack P0 alert** for any breach with `risk == "P0"` via `_send_p0_slack_alert()` — a formatted Slack Block Kit card with a clickable "Open in Notion" link to the offending SOP page.

**Entry point:**
```bash
python auditor.py
# or with a keyword filter:
python -c "from auditor import run_notion_audit; run_notion_audit('refund')"
```

---

### evolution.py — Proactive Policy Guard

The guard runs as a persistent Slack Socket Mode application. It listens to every message in the monitored channel and routes through the following pipeline:

#### Stage 1 — Bot Filter
Messages from bots (including itself) are dropped immediately to prevent feedback loops.

#### Stage 2 — Domain Gate (regex)
Twelve regex patterns map message content to one of four policy domains:

| Pattern | Domain |
|---|---|
| `refund`, `threshold`, `$\d+` | Refund |
| `onboarding` | Onboarding |
| `safety`, `background`, `driver`, `activat*` | Safety |
| `2fa`, `authentication`, `credential`, `bypass` | Credential |

Messages matching none of these patterns are dropped with no API calls made.

#### Stage 3 — State Awareness (dollar-amount shortcut)
Before calling Gemini, the guard checks whether every dollar amount in the message already exists verbatim in the Gold Book. If so, it posts a "already reflected" notice and returns — no LLM call needed.

#### Stage 4 — Gemini Classification
A single Gemini call (`_analyse_with_gemini`) returns a structured JSON object:

```json
{
  "message_type": "instruction",
  "is_contradiction": true,
  "is_already_aligned": false,
  "domain": "Credential",
  "proposed_change": "Skip 2FA for temporary access tokens",
  "contradicted_rule": "Rule 3: Credential Integrity & 2FA",
  "gap": "Gold Book: 2FA mandatory vs. proposed: optional for temp tokens",
  "revenue_leak": "Liability / Security risk — not quantifiable",
  "severity": "P0"
}
```

`message_type` can be:
- `instruction` — speaker proposes a rule change ("we should...")
- `deviation` — speaker reports an active breach ("I bypassed...")
- `other` — no policy signal detected → no action

#### Stage 5 — Routing
- `instruction` + `is_contradiction: true` → posts Approve/Reject card via `integrations.post_proposed_change()`
- `deviation` + `is_contradiction: true` → posts P0 Drift Warning via `_post_drift_warning()`
- `other` or `is_already_aligned: true` → silent pass or "already reflected" notice

---

### integrations.py — Shared Notification Layer

A pure-utility module with no runtime state. All functions are called by both `auditor.py` and `evolution.py`.

| Function | Purpose |
|---|---|
| `send_slack_alert(breaches)` | Simple text alert for a list of P0 breaches |
| `post_proposed_change(client, channel, thread_ts, alert, gold_text)` | Slack Block Kit card with Approve/Reject buttons and a contextual snippet from the Gold Book |
| `post_policy_update_confirmation(client, channel, thread_ts, approver, updated_policy)` | Confirmation card showing the updated policy after approval |
| `log_to_notion_audit(finding, severity, details)` | Creates a page in the Notion Audit Log database |
| `_build_audit_properties(finding, severity, details, timestamp)` | Shared payload builder ensuring both scripts use identical Notion property keys |
| `log_to_sheets(breaches, risks)` | (Optional) Appends breach rows to a Google Sheet |

`_build_audit_properties` is the single source of truth for the Notion payload. It conditionally omits `Severity` when blank to avoid Notion's 400 error on empty select fields.

---

## The Intelligence Layer (Gemini)

Three distinct Gemini calls power different parts of the pipeline:

### 1. `_analyse_with_gemini` — Intent Classification + Gap Analysis
**Model:** `gemini-2.5-flash-lite`
**When:** Every domain-gated Slack message
**Input:** Raw message text + Gold Book full text
**Output:** JSON with message_type, is_contradiction, gap, revenue_leak, severity

### 2. `_resolve_target_rule` — Rule Resolver
**Model:** `gemini-2.5-flash-lite`
**When:** Approve button pressed, but `contradicted_rule` from the button payload doesn't directly match a rule number in the Gold Book block map
**Input:** Proposed change text + list of all existing rule headings
**Output:**
```json
{"action": "append", "rule_num": "3", "new_rule_title": ""}
```
Three possible actions:
- `replace` — the proposal overwrites a rule's core logic
- `append` — the proposal adds a new provision to an existing rule (written as a sub-bullet)
- `new_rule` — a completely new topic; creates RULE N with a heading and paragraph

### 3. `_generate_rule_edit` — Surgical Rule Rewriter
**Model:** `gemini-2.5-flash-lite`
**When:** Approve button pressed, after rule resolution
**Input:** Current rule paragraph text only (not the whole policy) + change instruction
**Output:** Only the updated paragraph text — no headers, no surrounding rules, no preamble

---

## The Gold Book (Notion-Native)

The Gold Book is a Notion page. It is never stored as a local file during runtime. The system reads it fresh from Notion on every relevant event.

### Block Structure
```
[heading_3]  RULE 1: Refund Threshold ($150)
[paragraph]  All refund requests exceeding $150.00 require...

[heading_3]  RULE 2: Driver Safety & Background Checks
[paragraph]  No driver may be activated on the platform until...

[heading_3]  RULE 3: Credential Integrity & 2FA
[paragraph]  Two-Factor Authentication (2FA) is mandatory...

[paragraph]  Last Policy Update: 2026-04-22 13:45 UTC — approved by @alice
```

### Block Map
`_get_rule_block_map()` scans the page once per approval and returns:
```python
{
  "1": {"heading_id": "abc...", "paragraph_id": "def...", "paragraph_text": "All refund..."},
  "2": {"heading_id": "ghi...", "paragraph_id": "jkl...", "paragraph_text": "No driver..."},
  "3": {"heading_id": "mno...", "paragraph_id": "pqr...", "paragraph_text": "Two-Factor..."},
}
```

### Surgical Update Modes
| Action | Notion API Call | Effect |
|---|---|---|
| `replace` | `notion.blocks.update(paragraph_id, ...)` | Overwrites exactly one paragraph block |
| `append` | `notion.blocks.update(paragraph_id, ...)` | Concatenates `\n• new_provision` to existing text |
| `new_rule` | `notion.blocks.children.append(page_id, [...])` | Adds heading_3 + paragraph at bottom |

### Changelog
A single `Last Policy Update:` paragraph at the bottom of the page is updated in-place on every approval via `_update_changelog_block()`. No new blocks are appended for changelog entries.

---

## Slack Bot Flows

### INSTRUCTION Flow

```
User: "We should skip 2FA for temporary access tokens"
         │
         ▼
  Domain gate passes (credential keyword)
         │
         ▼
  Gemini: message_type = "instruction", is_contradiction = true
         │
         ▼
  Bot posts to thread:
  ┌────────────────────────────────────────────────────┐
  │  📝  Proposed Policy Update Detected               │
  │  Domain: Credential  |  SOP: SOP-SEC-003           │
  │  Contradicts: Rule 3: Credential Integrity & 2FA   │
  │  Proposed Change: Skip 2FA for temp access tokens  │
  │  Gap: 2FA mandatory vs. proposed optional for temp │
  │  ─────────────────────────────────────────────     │
  │  Contextual Snippet — Rule 3:                      │
  │  ```Two-Factor Authentication (2FA) is mandatory…```│
  │  ─────────────────────────────────────────────     │
  │  [ Approve Change ]    [ Reject ]                  │
  └────────────────────────────────────────────────────┘
```

### DEVIATION Flow

```
User: "I bypassed the 2FA check for that corporate account"
         │
         ▼
  Domain gate passes (bypass + credential keywords)
         │
         ▼
  Gemini: message_type = "deviation", is_contradiction = true
         │
         ▼
  Bot posts to thread:
  ┌────────────────────────────────────────────────────┐
  │  🚨  ACTIVE DEVIATION — P0 Drift Warning           │
  │  Severity: P0  |  Domain: Credential               │
  │  Violated Rule: Rule 3: Credential Integrity & 2FA │
  │  What is happening: 2FA bypassed for corp account  │
  │  Gap vs Gold Book: 2FA mandatory / bypass occurred │
  │  Risk: Liability / Security — not quantifiable     │
  │  🛡️ Escalate to Policy Review Board immediately.  │
  └────────────────────────────────────────────────────┘
```

### Approve Flow

```
User clicks [ Approve Change ]
         │
         ▼
  1. Decode button payload → instruction + contradicted_rule
  2. Build rule block map from Notion (fresh fetch)
  3. Try direct rule number match from contradicted_rule
         │
         ├─ Match found ──────────────────────────────────────┐
         │                                                     │
         └─ No match → _resolve_target_rule(Gemini) ──────────┤
                       → action: "append", rule_num: "3"      │
                                                              ▼
  4. _generate_rule_edit(current_rule_text, instruction)
     → Gemini returns only the updated paragraph text
         │
         ▼
  5. _surgical_update_notion(action, rule_entry, new_text, ...)
     → notion.blocks.update on single paragraph block
     → _update_changelog_block() — one line at page bottom
         │
         ▼
  6. log_to_notion_audit(finding="Policy Evolution: Rule 3 Updated",
                         severity="", details="Approved by @alice...")
         │
         ▼
  7. chat_update() replaces the Approve/Reject card with:
     "✅ Change Approved. Notion Gold Book updated and log entry created."
```

### Reject Flow

```
User clicks [ Reject ]
         │
         ▼
  chat_update() replaces card with:
  "❌ Policy Update Rejected by @user. The Gold Book was not modified."
  (No Notion writes. No audit log entry.)
```

---

## Surgical Rule Matching

This is the core differentiator of the system. When a user proposes something like "We should add a policy for temporary access tokens", the Gemini classification might return `contradicted_rule: "Credential Policy"` — not an exact match to "Rule 3: Credential Integrity & 2FA".

**Old behavior:** Fall back to appending an unstructured paragraph at the bottom of the page.

**New behavior (three-stage resolution):**

```
1. Extract rule number from contradicted_rule string (regex \d+)
   → Found "3" → look up rule_map["3"] → direct match ✓

2. If no digit found, or digit not in rule_map:
   → Call _resolve_target_rule(instruction, rule_map)
   → Gemini sees all rule headings and picks:
       "append to Rule 3" (adds sub-bullet to Credential rule)
       OR "replace Rule 2" (changes core safety logic)
       OR "new_rule: Temporary Access Policy" (creates RULE 4)

3. Execute the resolved action surgically:
   → append: existing_text + "\n• " + new_provision (one block.update call)
   → replace: overwrite paragraph block (one block.update call)
   → new_rule: append heading_3 + paragraph (one block.append call)
```

---

## Notion Audit Log

Every breach (from auditor) and every approved change (from evolution) is recorded in the same Notion database.

### Required Database Schema

| Column | Type | Values |
|---|---|---|
| `Finding` | Title | Auto-set by each script |
| `Severity` | Select | P0, P1, P2 (blank for approvals) |
| `Details` | Rich Text | SOP URL for breaches; approval info for evolutions |
| `Date` | Date | UTC timestamp of event |

### Sample Entries

| Finding | Severity | Details | Date |
|---|---|---|---|
| SOP-SEC-003: Agents bypass 2FA for corp accounts | P0 | [SOP link] | 2026-04-22 |
| Policy Evolution: Rule 3 Updated | _(blank)_ | Approved by @alice via Slack. Change: skip 2FA for temp tokens | 2026-04-22 |

---

## Project Structure

```
notion_engine/
├── evolution.py        # Slack Socket Mode bot — real-time policy guard
├── auditor.py          # One-shot compliance scan against Notion drafts
├── integrations.py     # Slack blocks, Notion logging, Google Sheets (shared)
├── requirements.txt    # All Python dependencies
├── .env                # Environment variables (never commit this)
└── refund_policy_gold.txt  # Local reference copy (not used at runtime)
```

---

## Environment Variables

Copy `.env` and fill in every value before running.

```env
# ── Notion ────────────────────────────────────────────────────────────────────
NOTION_API_KEY=secret_...          # Internal integration token from notion.so/my-integrations
NOTION_PAGE_ID=                    # Gold Book page ID (from the page URL)
NOTION_DATABASE_ID=                # Audit Log database ID
NOTION_DRAFTS_DATABASE_ID=         # SOP Drafts database ID

# ── Slack ─────────────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...           # Bot OAuth token
SLACK_APP_TOKEN=xapp-...           # App-level token (Socket Mode)
SLACK_CHANNEL_ID=                  # Channel or DM for P0 alerts from auditor

# ── Google Sheets (optional) ──────────────────────────────────────────────────
GOOGLE_SHEETS_ID=
GOOGLE_CREDS_JSON=path/to/service-account.json

# ── Gemini ────────────────────────────────────────────────────────────────────
GEMINI_API_KEY=
```

**How to find Notion IDs:**
- **Page ID:** Open the Gold Book page → Copy link → the 32-character hex string at the end is the ID (hyphens optional)
- **Database ID:** Open the database as a full page → same pattern in the URL before the `?v=` parameter

---

## Notion Setup

### 1. Create an Internal Integration

1. Go to [notion.so/my-integrations](https://www.notion.so/my-integrations)
2. Click **+ New integration**
3. Name it `Gap Detector`, select your workspace
4. Copy the **Internal Integration Secret** → this is `NOTION_API_KEY`

### 2. Invite the Integration to Each Page/Database

For every Notion resource the system touches (Gold Book page, Audit Log database, Drafts database):

1. Open the page or database in Notion
2. Click **...** (top-right) → **Connections** → **Connect to** → select `Gap Detector`

Without this step, all API calls return `404 Object not found`.

### 3. Gold Book Page Structure

Create a Notion page with this structure (use Heading 3 for rule titles):

```
### RULE 1: Refund Threshold ($150)
All refund requests exceeding $150.00 require formal Manager approval...

### RULE 2: Driver Safety & Background Checks
No driver may be activated on the platform until a Background Check...

### RULE 3: Credential Integrity & 2FA
Two-Factor Authentication (2FA) is mandatory for all account resets...
```

### 4. Audit Log Database

Create a Notion database with these exact column names:

| Column | Type |
|---|---|
| Finding | Title |
| Severity | Select (add options: P0, P1, P2) |
| Details | Rich Text |
| Date | Date |

### 5. SOP Drafts Database

Create a Notion database with these columns:

| Column | Type |
|---|---|
| Name | Title |
| Content | Rich Text |

Each row is one SOP draft. The auditor reads `Name` and `Content` from each page.

---

## Slack App Setup

### 1. Create the App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name: `Gap Detector`, pick your workspace

### 2. Enable Socket Mode

**Settings → Socket Mode → Enable Socket Mode**

Generate an App-Level Token with scope `connections:write` → this is `SLACK_APP_TOKEN`

### 3. Bot Token Scopes

**OAuth & Permissions → Bot Token Scopes**, add:

| Scope | Purpose |
|---|---|
| `chat:write` | Post messages and update cards |
| `channels:history` | Read public channel messages |
| `groups:history` | Read private channel messages |

Install the app to your workspace → copy the **Bot User OAuth Token** → this is `SLACK_BOT_TOKEN`

### 4. Event Subscriptions

**Event Subscriptions → Enable Events → Subscribe to bot events**, add:

- `message.channels`
- `message.groups`

### 5. Interactivity

**Interactivity & Shortcuts → Enable Interactivity**

(No Request URL needed — Socket Mode handles all payloads.)

### 6. Invite the Bot to Your Channel

In Slack: `/invite @Gap Detector` in the channel you want monitored.

---

## Running the System

### Install dependencies

```bash
pip install -r requirements.txt
```

### Load environment variables

```bash
# Linux / macOS
export $(grep -v '^#' .env | xargs)

# Windows (PowerShell)
Get-Content .env | Where-Object { $_ -notmatch '^#' -and $_ -ne '' } |
  ForEach-Object { $parts = $_ -split '=', 2; [System.Environment]::SetEnvironmentVariable($parts[0], $parts[1]) }
```

### Run the Policy Guard (persistent bot)

```bash
python evolution.py
```

Output on startup:
```
Proactive Policy Guard active — monitoring all channel messages...
```

### Run the Compliance Auditor (one-shot scan)

```bash
python auditor.py
```

Or run programmatically with an optional keyword filter:

```python
from auditor import run_notion_audit
run_notion_audit("refund")  # only audit refund-related SOPs
run_notion_audit()           # audit all SOPs in the database
```

---

## End-to-End Example

**Scenario:** A team member proposes a new policy for temporary access tokens — a topic that doesn't exactly match any existing rule title.

**Step 1 — Message sent in Slack:**
```
"We should allow bypassing the 2FA check when issuing temporary access tokens 
to contractors. The current flow causes too many support tickets."
```

**Step 2 — Domain gate fires:** `bypass` and `2fa` both match Credential patterns.

**Step 3 — Gemini classifies:** `message_type: "instruction"`, `contradicted_rule: "Credential Policy"`.

**Step 4 — Bot posts Approve/Reject card** in the thread with a contextual snippet from Rule 3.

**Step 5 — Manager clicks [Approve Change].**

**Step 6 — Rule resolution:** `contradicted_rule: "Credential Policy"` contains no digit → `_resolve_target_rule()` asks Gemini which rule to target → Gemini responds:
```json
{"action": "append", "rule_num": "3", "new_rule_title": ""}
```

**Step 7 — Gemini writes only the new provision:**
```
Two-Factor Authentication (2FA) is mandatory for all account resets. Under no 
circumstances should an agent bypass the SMS/Email verification step.
• Temporary contractor access tokens are exempt from 2FA if issued for ≤24 hours 
  and approved by a Senior Agent.
```

**Step 8 — Surgical Notion update:** `notion.blocks.update(paragraph_id_of_rule_3, ...)` — exactly one API call, exactly one block changed.

**Step 9 — Changelog updated:** `Last Policy Update: 2026-04-22 14:03 UTC — approved by @alice`

**Step 10 — Audit log created:**
```
Finding:  Policy Evolution: Rule 3 Updated
Severity: (blank)
Details:  Approved by @alice via Slack interaction. Change: allow bypassing 2FA for temp tokens...
Date:     2026-04-22T14:03:00.000Z
```

**Step 11 — Slack card updated:** "✅ Change Approved. Notion Gold Book updated and log entry created in Audit DB."

---

## Design Decisions

**Why `httpx` for Notion database queries?**
`notion-client` v3.0.0 removed `databases.query()`. Rather than pinning to v2, the SOP fetch uses `httpx.post` directly against the Notion REST API. All other Notion calls (block reads, block updates, page creates) still use the SDK.

**Why surgically update blocks instead of rewriting the page?**
Rewriting the full page on each approval caused duplicate "Master Book" sections to accumulate in Notion. Surgical block updates (`notion.blocks.update`) touch only the single paragraph block that belongs to the changed rule, leaving all other blocks untouched.

**Why encode `contradicted_rule` in the Approve button value?**
Slack's interactive components fire asynchronously — by the time the user clicks Approve, the original message context is gone. Embedding the rule identifier in the button `value` field as JSON means the handler knows exactly which Gold Book rule to target without a second Gemini classification call.

**Why omit `Severity` from the Notion payload when it's blank?**
Notion's API returns HTTP 400 if you pass `{"select": {"name": ""}}` for a Select field. The `_build_audit_properties` helper conditionally excludes the `Severity` key entirely when the value is an empty string, which is the correct way to leave a Select field blank.

**Why `gemini-2.5-flash-lite` instead of a heavier model?**
All three Gemini calls (classify, resolve, edit) are JSON-structured and operate on short, well-scoped prompts. Flash Lite provides sub-second latency and sufficient accuracy for policy-domain text, keeping per-message costs minimal for a 24/7 Slack bot.
