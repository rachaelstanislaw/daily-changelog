# Support Engineering — Changelog Bot

Automatically researches process changes across Confluence, Jira, Linear, and Slack,
then pushes new entries to the [Support Engineering Process Changelog](https://circleci.atlassian.net/wiki/spaces/CE/pages/8709898256).

Runs daily at **17:00 UTC** via a CircleCI scheduled pipeline.

---

## How it works

1. **Searches** four sources for the last 26 hours of activity:
   - Confluence CE space — recently modified pages
   - Jira SUP project — recently completed internal tasks
   - Linear SUPENG — recently completed issues
   - Slack `#custeng-support` and `#custeng-general` — process announcements

2. **Sends** all findings to Claude (Sonnet 4), which filters for genuine process/tooling/policy changes, de-duplicates across sources, and returns structured changelog entries.

3. **Inserts** new rows at the top of the full-width changelog table in Confluence and increments the page version.

---

## Setup

### 1. Create the CircleCI project

Connect this repo to CircleCI as a new project (set up project → use existing config).

### 2. Set environment variables

In **Project Settings → Environment Variables**, add all of the following:

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Claude API key |
| `CONFLUENCE_BASE_URL` | `https://circleci.atlassian.net` |
| `CONFLUENCE_EMAIL` | Atlassian account email for the bot user |
| `CONFLUENCE_API_TOKEN` | Atlassian API token ([create one here](https://id.atlassian.com/manage-profile/security/api-tokens)) |
| `JIRA_BASE_URL` | `https://circleci.atlassian.net` (same as Confluence) |
| `JIRA_EMAIL` | Same as `CONFLUENCE_EMAIL` |
| `JIRA_API_TOKEN` | Same as `CONFLUENCE_API_TOKEN` |
| `LINEAR_API_KEY` | Linear personal API key ([Settings → API](https://linear.app/settings/api)) |
| `SLACK_BOT_TOKEN` | Slack bot token — needs `search:read` scope (see below) |

### 3. Set up the Slack app

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From Scratch.
2. Name it `changelog-bot`, pick the CircleCI workspace.
3. Under **OAuth & Permissions → Scopes → Bot Token Scopes**, add `search:read`.
4. Install the app to the workspace.
5. Copy the **Bot User OAuth Token** (`xoxb-...`) into `SLACK_BOT_TOKEN`.

> **Note:** `search:read` searches **public channels only**. If important signals are in private channels, ask a Slack admin to add `search:read` for the user token instead, or promote those channels to public.

### 4. Atlassian bot account (recommended)

Rather than using a personal account, create a dedicated Atlassian account (e.g. `changelog-bot@circleci.com`) and grant it **Edit** access to the CE Confluence space and **Browse** access to the SUP Jira project. Use that account's email + API token in the env vars above.

---

## Running manually

To trigger a one-off run without waiting for the schedule:

```bash
# From the CircleCI UI: Pipelines → Trigger Pipeline → Branch: main

# Or locally (requires all env vars set in your shell):
pip install -r requirements.txt
python update_changelog.py
```

To look back further than the default 26 hours:

```bash
LOOKBACK_HOURS=72 python update_changelog.py
```

---

## Adjusting the schedule

The cron expression is in `.circleci/config.yml` under `daily-changelog-update`:

```yaml
cron: "0 17 * * *"   # 17:00 UTC daily
```

Change it to any valid cron expression. CircleCI uses UTC.

---

## What qualifies as a changelog entry

✅ Included:
- New or significantly updated Confluence process/policy/runbook pages
- New Zendesk macros, triggers, or workflow changes
- New Linear conventions or tooling setup
- Escalation path or SLA process changes
- Security advisory patterns or repeatable response playbooks
- Onboarding template changes that affect the whole team

❌ Excluded:
- Meeting notes
- Customer-specific ticket work
- Automated Wiz CVE tickets
- Product feature announcements (unless they change how Support handles tickets)

---

## Owner

Rachael (Senior Support Engineer) — ping her in `#custeng-support` with questions.
