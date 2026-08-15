# ACM Job Radar 📡

Auto-posts new CS/IT **internships**, **new-grad**, and **early-career** roles into your ACM Discord — every 30 minutes, forever, for free.

No server. No hosting bill. No bot token to babysit. It runs entirely on GitHub Actions and posts through Discord webhooks, so it keeps working even after every current officer graduates.

## How it works

```
GitHub Actions (every 30 min)
        │
        ├─► pulls listings.json from SimplifyJobs job boards
        │     • Summer20XX-Internships  (current cycle auto-discovered)
        │     • New-Grad-Positions
        │
        ├─► diffs against seen_jobs.json (committed in this repo)
        │
        ├─► posts anything new to Discord as rich embeds
        │
        └─► commits updated seen_jobs.json back
```

The SimplifyJobs boards are the community-maintained lists (originally Pitt CSC) that most tech job-drop Discords run on — thousands of active tech postings, updated many times a day, covering software, AI/ML/data, hardware, quant, and product roles.

## Setup (~10 minutes)

### 1. Create the Discord webhook(s)

In your ACM server: **channel → ⚙️ Edit Channel → Integrations → Webhooks → New Webhook** → name it, then **Copy Webhook URL**.

- Want internships and new-grad roles in **separate channels**? Make two webhooks (one per channel).
- One combined `#job-board` channel? One webhook is fine.

⚠️ Treat webhook URLs like passwords — anyone who has one can post to your channel. They only ever go in GitHub Secrets (step 3), never in code.

### 2. Create the GitHub repo

Create a new repo (under your ACM org account, ideally, so it survives officer turnover) and add these files, keeping the folder structure:

```
job_bot.py
seen_jobs.json            (created automatically on first run)
.github/
  └── workflows/
      └── job-bot.yml
README.md
```

Easiest way: upload the zip contents, or `git init` locally and push. The `.github/workflows/` path must be exact or GitHub won't see the workflow.

### 3. Add the webhook(s) as secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

- Two channels: add `DISCORD_WEBHOOK_INTERNSHIPS` and `DISCORD_WEBHOOK_NEWGRAD`
- One channel: add just `DISCORD_WEBHOOK_URL`

### 4. Run it once manually

Repo → **Actions** tab → enable workflows if prompted → select **ACM Job Radar** → **Run workflow**.

The first run "bootstraps": it marks all ~4,700 currently-active listings as already-seen (so your channel doesn't get nuked with years of backlog) and posts only the 5 freshest per category so you can confirm it works.

### 5. Done

It now runs every 30 minutes automatically. From here on, only *newly added* postings get posted, oldest-first, max 15 per run (extras queue up for the next run — nothing is lost).

## Configuration (all optional)

Set secrets in **Settings → Secrets and variables → Actions → Secrets**, and variables under the **Variables** tab.

| Name | Type | Default | What it does |
|---|---|---|---|
| `DISCORD_WEBHOOK_INTERNSHIPS` | secret | — | Webhook for internship posts |
| `DISCORD_WEBHOOK_NEWGRAD` | secret | — | Webhook for new-grad posts |
| `DISCORD_WEBHOOK_URL` | secret | — | Fallback webhook used for both |
| `PING_ROLE_ID` | variable | off | Discord role ID to @mention when jobs drop (right-click role → Copy Role ID, with Developer Mode on) |
| `MAX_POSTS_PER_RUN` | env in yml | 15 | Anti-flood cap per 30-min run |
| `BOOTSTRAP_POST_COUNT` | env in yml | 5 | Posts per category on the very first run |
| `CATEGORY_BLOCKLIST` | env in yml | none | Skip categories, e.g. `Quant,Product`. Categories: Software, AI/ML/Data, Hardware, Quant, Product |
| `BOT_NAME` | env in yml | ACM Job Radar 📡 | Display name on posts |

To change the schedule, edit the `cron` line in `.github/workflows/job-bot.yml` (`*/30 * * * *` = every 30 min).

## Testing locally

```bash
DRY_RUN=1 python3 job_bot.py
```

Prints what would be posted without touching Discord or saving state. No dependencies needed — pure Python standard library.

## Maintenance & officer-handoff notes

- **Annual repo rename? Handled.** SimplifyJobs creates a new `Summer20XX-Internships` repo each cycle. The bot auto-discovers the newest one via the GitHub API on every run, so nobody has to update anything each year. (There's a hardcoded fallback in `job_bot.py` if the API is ever unreachable.)
- **"Scheduled workflow disabled due to inactivity"** — GitHub pauses schedules on repos with no activity for 60 days, but the bot's own state commits count as activity, so it self-sustains. If it ever *does* get paused (e.g., the bot erred for 60+ days), just hit re-enable in the Actions tab.
- **One post per listing.** Reposts/date-bumps of a listing the bot already posted are ignored by design.
- **Duplicate posts after a config change?** Delete `seen_jobs.json` only if you want a fresh bootstrap — otherwise leave it alone; it's the bot's memory.
- **Be a good citizen.** These boards are community-maintained. Every-30-min polling is polite; don't crank it to every minute. If your members find dead links or missing roles, contribute fixes upstream at [SimplifyJobs on GitHub](https://github.com/SimplifyJobs).

## Extending it later

- **More sources:** add another entry in `fetch_source()`-style — any JSON feed of jobs works (e.g., a Google Form + Sheet where members submit local/campus postings).
- **Slash commands / search** (`/jobs search google`): that requires a real hosted bot (discord.py + somewhere to run 24/7). This design is push-only on purpose — zero hosting is what keeps it alive long-term.

---

Built for the ACM chapter. Job data courtesy of the [SimplifyJobs](https://github.com/SimplifyJobs) community boards. 💚
