# 🚨 SWE Internship Alert Bot

Automated bot that watches for **Summer 2027 software-engineering internships**
and emails you a nicely formatted digest whenever *new* ones are posted. Runs
free on **GitHub Actions** every 4 hours.

It watches two kinds of sources:

1. **Community internship-tracker repos** (SimplifyJobs, speedyapply, vanshb03,
   zshah101) — read via the raw README, parsing their job tables.
2. **Direct company career feeds** for your priority companies — hitting the
   public Greenhouse / Lever / Workday JSON endpoints (no scraping).

New jobs are diffed against a committed `seen_jobs.json` so you're only ever
emailed a posting **once**. If nothing's new, it sends nothing.

---

## Quick start (TL;DR)

1. Create a **Gmail app password** (below).
2. Fork/push this repo to your GitHub account.
3. Add 3 **repo secrets**: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`, `RECIPIENT_EMAIL`.
4. Enable Actions, run the workflow once with **Seed = true** (so you don't get
   a flood of existing jobs).
5. Done — you'll get an email whenever a genuinely new role appears.

---

## 1. Create a Gmail app password

Gmail won't let a script log in with your normal password. You need a 16-char
**app password** (requires 2-Step Verification to be on):

1. Turn on 2-Step Verification: <https://myaccount.google.com/signing-in-verification>
2. Go to **App passwords**: <https://myaccount.google.com/apppasswords>
3. Type a name like `internship-bot` and click **Create**.
4. Copy the 16-character password it shows (e.g. `abcd efgh ijkl mnop`).
   Use it **without spaces** → `abcdefghijklmnop`.

You'll paste this into the `GMAIL_APP_PASSWORD` secret. It only grants SMTP send
access and can be revoked anytime from that same page.

---

## 2. Add the GitHub secrets

In your repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add these three:

| Secret name          | Value                                             |
| -------------------- | ------------------------------------------------- |
| `GMAIL_ADDRESS`      | the Gmail you generated the app password for      |
| `GMAIL_APP_PASSWORD` | the 16-char app password (no spaces)              |
| `RECIPIENT_EMAIL`    | where alerts should go (can be the same Gmail)    |

---

## 3. Push the repo & enable Actions

```bash
git init
git add .
git commit -m "initial internship alert bot"
git branch -M main
git remote add origin https://github.com/<you>/internship-alert-bot.git
git push -u origin main
```

Then in the repo, open the **Actions** tab and click **"I understand my
workflows, enable them"** if prompted.

> The workflow needs permission to commit `seen_jobs.json` back. That's already
> granted via `permissions: contents: write` in the workflow file, but also
> confirm **Settings → Actions → General → Workflow permissions** is set to
> **"Read and write permissions."**

---

## 4. Seed the state (important — do this first!)

Before the first real run, seed the "already seen" list so you don't get one
giant email with every currently-open internship:

- **Actions** tab → **internship-alert** → **Run workflow**
- Set **Seed** = `true` → **Run workflow**

This records everything currently posted as seen and sends **no email**. From
then on, only *new* postings trigger an alert. (If you skip this, the bot
auto-seeds on its very first run anyway — but running it manually is cleaner.)

The scheduled runs then take over automatically (every 4 hours). You can also
hit **Run workflow** anytime for a manual check (leave Seed/Dry run unchecked).

---

## 5. Test locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# See what WOULD be emailed, without sending anything or touching state.
# Also writes a full HTML preview to email_preview.html you can open in a browser.
python main.py --dry-run
```

To do a real end-to-end send test from your machine, export the same secrets
first:

```bash
export GMAIL_ADDRESS="you@gmail.com"
export GMAIL_APP_PASSWORD="abcdefghijklmnop"
export RECIPIENT_EMAIL="you@gmail.com"
python main.py            # sends a real email if there are new jobs
```

> Note: because `seen_jobs.json` starts empty, your **first** local run will
> seed (record everything, send nothing). Run it again to see diff behavior, or
> use `--dry-run` which ignores state entirely.

---

## Customizing — everything lives in `config.yaml`

You never need to edit code to change what's watched.

### Add / remove a tracker repo

Edit the `github_repos:` list. The `repo` field is the only thing to change if a
new season's repo appears:

```yaml
- name: "SimplifyJobs"
  repo: "SimplifyJobs/Summer2027-Internships"
  fallback_repo: "SimplifyJobs/Summer2026-Internships"   # used automatically on 404
  branch: "dev"
  files: ["README.md"]
```

### Add / remove a company feed

Edit the `companies:` list. You need the ATS **type** and its identifier:

```yaml
- name: "DoorDash"
  type: "greenhouse"      # greenhouse | lever | workday | ashby | smartrecruiters
  token: "doordash"       # the board slug
  enabled: true
```

**How to find the identifier:**

- **Greenhouse** — job board URL looks like `boards.greenhouse.io/<token>`.
  Test: `https://boards-api.greenhouse.io/v1/boards/<token>/jobs`
- **Lever** — board URL looks like `jobs.lever.co/<token>`.
  Test: `https://api.lever.co/v0/postings/<token>?mode=json`
- **Workday** — the careers URL looks like
  `https://<tenant>.wdN.myworkdayjobs.com/<site>`. Fill in `host`, `tenant`,
  and `site`:
  ```yaml
  - name: "Capital One"
    type: "workday"
    host: "capitalone.wd1.myworkdayjobs.com"
    tenant: "capitalone"
    site: "Capital_One"
    enabled: true
  ```
- **Ashby** — board URL looks like `jobs.ashbyhq.com/<token>`.
  Test: `https://api.ashbyhq.com/posting-api/job-board/<token>`
- **SmartRecruiters** — board URL looks like `jobs.smartrecruiters.com/<token>`.
  Test: `https://api.smartrecruiters.com/v1/companies/<token>/postings?limit=1`
  Optional `country:` (2-letter, default `us`) narrows the server-side search.
  ⚠️ This API returns an empty result set — **not** a 404 — for an unknown
  company identifier, so a typo looks exactly like "no jobs right now". If a
  SmartRecruiters feed reports 0 rows, check `totalFound` with no `q` filter
  before assuming the board is just quiet.

Set `enabled: false` to keep a company's config but skip it. Big companies with
custom career systems (Amazon, Google, Meta, Apple, Netflix, etc.) are included
but disabled by default — the tracker repos already surface their internships.
If a feed's token is wrong or the endpoint is down, it's logged and skipped —
one broken feed never stops the run.

### Reading a Slack channel

The bot can also harvest job links out of Slack channels you're already in.
This is **off by default** and needs three things set up in the workspace:

**1. Create a Slack app and get a bot token**

- Go to <https://api.slack.com/apps> → **Create New App** → *From scratch*, and
  pick the workspace with the jobs channel.
- **OAuth & Permissions** → *Scopes* → *Bot Token Scopes*, add:
  - `channels:history` — read public channels
  - `groups:history` — only if the channel is private
- **Install to Workspace** at the top of that page, then copy the
  **Bot User OAuth Token** (starts with `xoxb-`).

> If you're not a workspace admin, installing the app will raise an approval
> request that an admin has to accept. There's no way around this — Slack has no
> read API that works without an installed app.

**2. Add the token as a secret**

Add `SLACK_BOT_TOKEN` alongside your other GitHub secrets (same place as
`GMAIL_APP_PASSWORD`). For local runs, export it in your shell.

**3. Invite the bot and list the channels**

In each channel you want read: `/invite @your-bot-name`. The bot can only see
channels it has joined.

Then fill in `config.yaml`:

```yaml
slack:
  enabled: true
  lookback_hours: 48        # how far back to read on each run
  channels:
    - name: "internships"   # label only, used in the email's source line
      id: "C01ABCDEFGH"     # the real channel ID — see below
      company_hint: ""      # fallback company when the link doesn't reveal one
```

To get a channel **ID**: open the channel, click its name at the top, and scroll
to the bottom of the *About* tab — it's the `C…` string, not the `#name`.

**What it does and doesn't do.** Slack messages are prose, not structured feeds,
so this source is deliberately conservative. It pulls every link out of recent
messages, derives the company from the link itself (Greenhouse, Lever, Ashby,
SmartRecruiters, and Workday URLs are all recognized), and requires an
internship signal in the message before emitting anything. Chat noise, GIFs,
Google Docs links, and join messages are dropped. Links it can't make sense of
are discarded rather than guessed at — so the failure mode is a missed posting,
never a junk alert. Everything it does emit still goes through the same
`config.yaml` filters as every other source.

One rough edge: companies derived from Workday URLs come out as the tenant slug
(`capitalone` → `Capitalone`), which won't match a `priority_companies:` entry
written as `Capital One`. Those jobs still get emailed, just not pinned to the
⭐ section.

### Change priority companies, keywords, or the recipient

- `priority_companies:` — companies pinned to the ⭐ section at the top of the email.
- `include_keywords:` / `exclude_keywords:` — title matching.
- `target_season_keywords:` / `reject_season_keywords:` — season filter.
- `email.recipient_override:` — set to override the `RECIPIENT_EMAIL` secret.

To alert more than one address, comma-separate them in either the
`RECIPIENT_EMAIL` secret or `email.recipient_override`
(e.g. `you@gmail.com, friend@gmail.com`). Everyone is on the same `To:` line,
so all recipients can see each other's addresses.

---

## How filtering works

A job is kept only if **all** of these hold:

- Title contains a SWE-intern keyword and **none** of the exclude keywords
  (PhD, hardware, electrical, firmware, manager, …).
- Location matches US / Remote-US (state names, abbreviations, or major cities
  are recognized; blank locations pass, since many feeds omit them).
- Season is Summer 2027, **or** the posting doesn't state a season but was
  posted within `recent_days` (default 30).
- Graduation window (when parseable) isn't clearly before Dec 2027.

---

## Deduplication & state

- Each job gets a stable ID: `company + normalized-title + url-hash`.
- `seen_jobs.json` stores every ID already emailed.
- Each run: fetch → filter → diff against `seen_jobs.json` → email only the new
  ones → commit the updated `seen_jobs.json` back from the Actions workflow.
- No new jobs → no email.

---

## Debugging failed runs

Open the **Actions** tab → the failed run → the **Run alert bot** step. The bot
logs every source, how many rows it parsed, how many passed filtering, and the
final diff count. Common issues:

- **No email but you expected one** → check the diff count in the log; the job
  may already be in `seen_jobs.json`, or filtered out (check the filter line).
- **SMTP auth error** → the app password is wrong/expired, or 2-Step isn't on.
- **A company logs "feed error … skipping"** → its ATS token changed; update it
  in `config.yaml` (the run still succeeds for everything else).
- **Push of `seen_jobs.json` fails** → set Workflow permissions to read/write
  (step 3).

---

## Project layout

```
config.yaml                 all tunable targets/keywords/recipient
main.py                     entry point / orchestration (--dry-run, --seed)
requirements.txt            requests, PyYAML
seen_jobs.json              dedupe state (committed by the workflow)
bot/
  config.py                 config load + logging
  models.py                 Job dataclass + stable IDs
  http.py                   requests wrapper with graceful failure
  filters.py                title / season / location / grad filtering
  state.py                  load/save/diff seen_jobs.json
  emailer.py                subject + mobile-friendly HTML + SMTP send
  sources/
    github_repos.py         raw-README markdown-table parser
    ats.py                  Greenhouse / Lever / Workday / Ashby /
                            SmartRecruiters readers
    slack.py                Slack channel link harvester (off by default)
.github/workflows/alert.yml GitHub Actions: cron every 4h + manual dispatch
```

---

## Requirements

Python 3.11+ and two small libraries (`requests`, `PyYAML`). No databases, no
frameworks, no paid services.
