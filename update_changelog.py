#!/usr/bin/env python3
"""
Support Engineering Process Changelog — Daily Auto-Update Bot

Searches Confluence, Linear, and Slack for the last 24 hours of
process-relevant changes, then uses Claude to synthesize changelog entries
and pushes them to the Confluence page.

Required environment variables:
  ANTHROPIC_API_KEY       — Claude API key
  CONFLUENCE_BASE_URL     — e.g. https://circleci.atlassian.net
  CONFLUENCE_EMAIL        — Atlassian account email
  CONFLUENCE_API_TOKEN    — Atlassian API token
  LINEAR_API_KEY          — Linear personal API key
  SLACK_BOT_TOKEN         — Slack bot token with search:read scope
"""

import os
import sys
import json
import logging
import datetime
import re
import time
from typing import Optional

import requests
import anthropic

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

CONFLUENCE_BASE_URL = os.environ["CONFLUENCE_BASE_URL"].rstrip("/")
CONFLUENCE_EMAIL = os.environ["CONFLUENCE_EMAIL"]
CONFLUENCE_API_TOKEN = os.environ["CONFLUENCE_API_TOKEN"]
CONFLUENCE_PAGE_ID = "8709898256"
CONFLUENCE_SPACE = "CE"

LINEAR_API_KEY = os.environ["LINEAR_API_KEY"]

SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_CHANNELS = [
    {"name": "custeng-support", "id": "C03CF3S7P"},
    {"name": "custeng-general"},
]

# Private channels to poll directly via conversations.history (bot must be invited)
SLACK_PRIVATE_CHANNELS = [
    {"name": "motivators", "id": "G17SY9W5Q"},
]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
CLAUDE_MODEL = "claude-sonnet-4-6"

# How far back to look (hours). Default 26h to give a small overlap buffer.
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "26"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def since_date() -> str:
    """Return ISO date string for LOOKBACK_HOURS ago."""
    d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    return d.strftime("%Y-%m-%d")


def since_iso() -> str:
    """Return full ISO timestamp for LOOKBACK_HOURS ago."""
    d = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def today_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def today_human() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%B %-d, %Y")


# ---------------------------------------------------------------------------
# Source: Confluence
# ---------------------------------------------------------------------------

def search_confluence() -> list[dict]:
    """Return recently modified CE pages that may represent process changes."""
    log.info("Searching Confluence (CE space, last %d hours)...", LOOKBACK_HOURS)
    cql = (
        f'space = "{CONFLUENCE_SPACE}" AND type = page '
        f'AND lastModified >= "{since_date()}" '
        f'ORDER BY lastModified DESC'
    )
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/search"
    params = {
        "cql": cql,
        "limit": 50,
        "expand": "version,body.storage,metadata.labels",
    }
    resp = requests.get(
        url,
        params=params,
        auth=(CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    results = resp.json().get("results", [])
    log.info("  Found %d Confluence pages", len(results))

    pages = []
    for r in results:
        body_text = r.get("body", {}).get("storage", {}).get("value", "")
        plain = re.sub(r"<[^>]+>", " ", body_text)
        plain = re.sub(r"\s+", " ", plain).strip()[:2000]
        pages.append({
            "source": "confluence",
            "title": r["title"],
            "url": f"{CONFLUENCE_BASE_URL}/wiki{r['_links']['webui']}",
            "last_modified": r["version"]["when"],
            "excerpt": plain,
        })
    return pages


# ---------------------------------------------------------------------------
# Source: Linear
# ---------------------------------------------------------------------------

def search_linear() -> list[dict]:
    """Return recently completed SUPENG issues."""
    log.info("Searching Linear SUPENG (last %d hours)...", LOOKBACK_HOURS)
    since = since_iso()
    query = f"""
    query {{
      issues(
        filter: {{
          team: {{ key: {{ eq: "SUPENG" }} }}
          completedAt: {{ gte: "{since}" }}
        }}
        orderBy: updatedAt
        first: 30
      ) {{
        nodes {{
          identifier
          title
          description
          url
          state {{ name }}
          completedAt
          labels {{ nodes {{ name }} }}
        }}
      }}
    }}
    """
    resp = requests.post(
        "https://api.linear.app/graphql",
        json={"query": query},
        headers={"Authorization": LINEAR_API_KEY, "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    nodes = resp.json().get("data", {}).get("issues", {}).get("nodes", [])
    log.info("  Found %d Linear issues", len(nodes))

    results = []
    for n in nodes:
        results.append({
            "source": "linear",
            "identifier": n["identifier"],
            "title": n["title"],
            "url": n["url"],
            "state": n["state"]["name"],
            "completed_at": n.get("completedAt"),
            "labels": [l["name"] for l in n.get("labels", {}).get("nodes", [])],
            "description": (n.get("description") or "")[:1000],
        })
    return results


# ---------------------------------------------------------------------------
# Source: Slack
# ---------------------------------------------------------------------------

def search_slack() -> list[dict]:
    """Search key Slack channels for process announcements."""
    log.info("Searching Slack channels (last %d hours)...", LOOKBACK_HOURS)
    after = since_date()

    patterns = [
        f"process change in:custeng-support after:{after}",
        f"heads up in:custeng-support after:{after}",
        f"FYI in:custeng-support after:{after}",
        f"going forward in:custeng-support after:{after}",
        f"reminder in:custeng-support after:{after}",
        f"process change in:custeng-general after:{after}",
        f"heads up in:custeng-general after:{after}",
        f"FYI in:custeng-general after:{after}",
    ]

    seen_ts = set()
    results = []

    for query in patterns:
        try:
            resp = requests.get(
                "https://slack.com/api/search.messages",
                params={"query": query, "count": 10},
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.warning("  Slack search error for '%s': %s", query, data.get("error"))
                continue
            matches = data.get("messages", {}).get("matches", [])
            for m in matches:
                ts = m.get("ts")
                if ts in seen_ts:
                    continue
                seen_ts.add(ts)
                results.append({
                    "source": "slack",
                    "channel": m.get("channel", {}).get("name", "unknown"),
                    "user": m.get("username", "unknown"),
                    "text": m.get("text", "")[:800],
                    "ts": ts,
                    "permalink": m.get("permalink", ""),
                })
            time.sleep(1.1)
        except Exception as e:
            log.warning("  Slack search failed for '%s': %s", query, e)

    log.info("  Found %d unique Slack messages", len(results))
    return results


def poll_private_slack_channels() -> list[dict]:
    """Poll private channels directly via conversations.history (requires bot invite)."""
    if not SLACK_PRIVATE_CHANNELS:
        return []

    log.info("Polling %d private Slack channel(s)...", len(SLACK_PRIVATE_CHANNELS))
    oldest_ts = str(
        (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=LOOKBACK_HOURS)).timestamp()
    )

    results = []
    for channel in SLACK_PRIVATE_CHANNELS:
        try:
            resp = requests.get(
                "https://slack.com/api/conversations.history",
                params={
                    "channel": channel["id"],
                    "oldest": oldest_ts,
                    "limit": 50,
                },
                headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                log.warning(
                    "  Private channel poll error for #%s: %s",
                    channel["name"],
                    data.get("error"),
                )
                continue
            messages = data.get("messages", [])
            log.info("  Found %d messages in #%s", len(messages), channel["name"])
            for m in messages:
                if m.get("subtype"):
                    continue
                results.append({
                    "source": "slack_private",
                    "channel": channel["name"],
                    "user": m.get("user", "unknown"),
                    "text": m.get("text", "")[:800],
                    "ts": m.get("ts"),
                    "permalink": f"https://circleci.slack.com/archives/{channel['id']}/p{m.get('ts', '').replace('.', '')}",
                })
        except Exception as e:
            log.warning("  Failed to poll #%s: %s", channel["name"], e)

    return results


# ---------------------------------------------------------------------------
# Claude: synthesize entries
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are the Support Engineering changelog bot for CircleCI.
Your job is to review raw research data from Confluence, Linear, and Slack,
and produce new changelog entries for the Support Engineering Process Changelog.

RULES:
- Only create entries for genuine process/tooling/policy/docs changes that affect how the support team works.
- Skip: customer tickets, meeting notes, Wiz CVE auto-tickets, product feature announcements (unless they change how Support handles tickets).
- De-duplicate: if multiple sources describe the same change, produce ONE entry.
- If something surfaced in Slack but has no formal doc yet, still create an entry — use the Slack permalink as the source and add "No formal doc yet — update this entry when one is created." to the summary.
- If something is unresolved/in-progress, add "Not fully resolved — update this entry when a decision is made." to the summary.

OUTPUT FORMAT:
Return a JSON array (no markdown fences, no preamble). Each object must have:
{
  "date_iso": "YYYY-MM-DD",
  "date_human": "Month D, YYYY",
  "category": "Process" | "Tooling" | "Docs" | "Policy" | "On-call",
  "category_color": "blue" | "purple" | "green" | "red" | "yellow",
  "title": "Bold one-line title",
  "summary": "2-5 sentences. What changed, why it matters, any action required.",
  "source_url": "https://...",
  "source_label": "Short link label e.g. Confluence, Linear SUPENG-45, Slack thread",
  "added_by": "Name or Team"
}

If there are no qualifying changes, return an empty array: []
"""


def synthesize_entries(raw_data: dict) -> list[dict]:
    """Send all research to Claude and get back structured changelog entries."""
    log.info("Asking Claude to synthesize changelog entries...")
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    user_message = f"""Today's date: {today_human()} ({today_iso()})
Research window: last {LOOKBACK_HOURS} hours

=== CONFLUENCE PAGES ===
{json.dumps(raw_data['confluence'], indent=2)}

=== LINEAR ISSUES ===
{json.dumps(raw_data['linear'], indent=2)}

=== SLACK MESSAGES (public channels) ===
{json.dumps(raw_data['slack'], indent=2)}

=== SLACK MESSAGES (private: #motivators) ===
{json.dumps(raw_data['slack_private'], indent=2)}

Review all of the above and return a JSON array of changelog entries following the rules in your system prompt.
"""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    text = response.content[0].text.strip()
    text = re.sub(r"^```[a-z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    entries = json.loads(text)
    log.info("  Claude returned %d entries", len(entries))
    return entries


# ---------------------------------------------------------------------------
# Build HTML rows
# ---------------------------------------------------------------------------

def build_table_rows(entries: list[dict]) -> str:
    """Convert entry dicts to Confluence storage-format HTML table rows."""
    rows = []
    for e in entries:
        row = f"""<tr>
  <td><p><time datetime="{e['date_iso']}">{e['date_human']}</time></p></td>
  <td><p><span data-type="status" data-color="{e['category_color']}">{e['category']}</span></p></td>
  <td><p><strong>{e['title']}</strong></p></td>
  <td><p>{e['summary']}</p></td>
  <td><p><a href="{e['source_url']}">{e['source_label']}</a></p></td>
  <td><p>{e['added_by']}</p></td>
</tr>"""
        rows.append(row)
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# Confluence: fetch + update
# ---------------------------------------------------------------------------

def get_confluence_page() -> dict:
    """Fetch the current page content and version."""
    log.info("Fetching current Confluence page (ID %s)...", CONFLUENCE_PAGE_ID)
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{CONFLUENCE_PAGE_ID}"
    resp = requests.get(
        url,
        params={"expand": "body.storage,version"},
        auth=(CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def update_confluence_page(page: dict, new_rows_html: str, entry_count: int) -> None:
    """Insert new rows at the top of the changelog table and push the update."""
    current_body = page["body"]["storage"]["value"]
    current_version = page["version"]["number"]

    tbody_pattern = re.compile(r"(<table[^>]*data-layout=\"full-width\"[^>]*>.*?<tbody>)", re.DOTALL)
    match = tbody_pattern.search(current_body)
    if not match:
        raise ValueError(
            "Could not find full-width table <tbody> in page. "
            "Page structure may have changed — manual review required."
        )

    insert_pos = match.end()
    new_body = current_body[:insert_pos] + "\n" + new_rows_html + "\n" + current_body[insert_pos:]

    version_message = (
        f"Auto-update: added {entry_count} entr{'y' if entry_count == 1 else 'ies'} "
        f"for {today_human()}"
    )

    payload = {
        "version": {"number": current_version + 1, "message": version_message},
        "title": page["title"],
        "type": "page",
        "body": {
            "storage": {
                "value": new_body,
                "representation": "storage",
            }
        },
    }

    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/{CONFLUENCE_PAGE_ID}"
    log.info(
        "Pushing update to Confluence (version %d → %d, message: %s)...",
        current_version,
        current_version + 1,
        version_message,
    )
    resp = requests.put(
        url,
        json=payload,
        auth=(CONFLUENCE_EMAIL, CONFLUENCE_API_TOKEN),
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    log.info("  ✓ Confluence page updated successfully.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== Changelog bot starting (lookback: %d hours) ===", LOOKBACK_HOURS)

    # 1. Research
    confluence_pages = search_confluence()
    linear_issues = search_linear()
    slack_messages = search_slack()
    slack_private = poll_private_slack_channels()

    raw_data = {
        "confluence": confluence_pages,
        "linear": linear_issues,
        "slack": slack_messages,
        "slack_private": slack_private,
    }

    total_signals = sum(len(v) for v in raw_data.values())
    log.info(
        "Research complete: %d total signals (%d Confluence, %d Linear, %d Slack public, %d Slack private)",
        total_signals,
        len(confluence_pages),
        len(linear_issues),
        len(slack_messages),
        len(slack_private),
    )

    # 2. Synthesize
    entries = synthesize_entries(raw_data)

    if not entries:
        log.info("No qualifying changelog entries found. Nothing to push. Done.")
        return

    # 3. Build HTML
    new_rows = build_table_rows(entries)

    # 4. Fetch current page
    page = get_confluence_page()

    # 5. Push update
    update_confluence_page(page, new_rows, len(entries))

    log.info(
        "=== Done. %d entr%s added to the changelog. ===",
        len(entries),
        "y" if len(entries) == 1 else "ies",
    )


if __name__ == "__main__":
    main()
