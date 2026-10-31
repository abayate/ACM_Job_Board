# ACM Job Radar

Automated Discord job board for the **ACM chapter at Kean University**.

Posts new CS/IT internships and new-grad roles into Discord as rich embeds.
Runs entirely on GitHub Actions — no server, no hosting cost, no bot token,
nothing to keep alive.

```
SimplifyJobs feeds ──▶ filter (US · CS/IT · current term) ──▶ diff vs seen_jobs.json
                                                                      │
                                            resolve degree level ◀────┘
                                                      │
                    ┌─────────────────────────────────┼─────────────────────────────────┐
                    ▼                                 ▼                                 ▼
             #internships                        #grad-roles                     #new-grad
          bachelor's internships             MS/PhD *required*            entry-level full-time
               ~63/day                           ~9/day                        ~24/day
```

## Data sources

Community-maintained job boards, updated many times a day:

- [`SimplifyJobs/Summer20XX-Internships`](https://github.com/SimplifyJobs/Summer2027-Internships) — the current cycle is **auto-discovered**, so the annual repo rename needs no code change
- [`SimplifyJobs/New-Grad-Positions`](https://github.com/SimplifyJobs/New-Grad-Positions)

Both are read from `.github/scripts/listings.json` on the `dev` branch.

## What actually gets posted

Of ~6,700 open listings nationwide, roughly **3,900 (~100/day)** survive filtering:

| Filter | Default | What it does |
|---|---|---|
| `CATEGORY_ALLOWLIST` | `Software,AI/ML/Data,Product` | CS/IT roles only. Drops Quant (finance) and Hardware (EE). Full vocabulary: `Software`, `AI/ML/Data`, `Hardware`, `Product`, `Quant` |
| `US_ONLY` | `1` | Drops London / Toronto / Bangalore postings |
| `DROP_EXPIRED_TERMS` | `1` | No "Summer 2026" internships showing up in September 2026 |
| `LOCAL_STATES` | `NJ,NY,PA,CT,DE` | These get a 📍 marker, a gold embed, and priority when a run is over cap |

**There is no degree filter** — PhD and Master's roles are posted too, since
plenty of members are headed to grad school. They are *routed* to their own
channel. If the graduate webhook is not configured, graduate-only roles are
skipped rather than sent to internships or new-grad opportunities.

## Channels and degree routing

Routing is **degree-first**: a role requiring an MS/PhD goes to the grad
channel whether it is an internship or full-time. The alternative
(career-stage first) leaves grad students digging through the new-grad channel
for the full-time roles they qualify for.

> **`grad` means grad-degree-REQUIRED, not grad-relevant.** 1,012 open roles
> accept Bachelor's *and* Master's/PhD. Those stay in the bachelors/newgrad
> channels — routing them to both would duplicate a third of the feed every
> day. **Put this in the grad channel's topic**, or grad students will treat it
> as their whole feed and miss the ~1,000 roles they're eligible for.

Degree level is resolved from the best available evidence:

1. **Employer verification** (enabled by default): read the original
   job title and qualifications through public ATS APIs or employer pages. This takes precedence
   over incomplete or conflicting feed tags. Explicit undergraduate eligibility
   remains eligible for internships; a merely preferred PhD is not a requirement.
   A completed bachelor's prerequisite plus mandatory graduate enrollment is
   graduate-only. Explicit undergraduate and graduate application pathways are
   alternatives, rather than requirements every applicant must meet.
2. **Explicit PhD/doctoral feed titles**, excluding mixed undergraduate/graduate
   titles and pre-doctoral fellowships.
3. **Simplify degree tags**, including mixed degrees that accept Bachelor's.
4. **Legacy ATS lookup/cache**, with new Greenhouse/Ashby/Lever description
   lookups still disabled by default (`DEGREE_LOOKUP=0`).
5. **Unverified**: retain the existing internships/new-grad fallback, labeled
   “Degree requirement unverified — check the posting.” Unknown is not proof
   of undergraduate eligibility.

### Employer verification and limitations

A missing feed degree does **not** mean the employer omitted it. Meta job
`1126899676481179`, for example, has no degree tags or PhD marker in the feed
but explicitly requires a PhD on the original employer page. Historical
measurements against other ATS sites do not establish Meta coverage.

`EMPLOYER_LOOKUP=1` verifies listings selected for posting across employers,
even when feed labels exist. A shared classifier reads original titles,
explicit degree requirements, and minimum/preferred qualification sections:

| Source | Retrieval |
|---|---|
| Greenhouse | Public job JSON, including the original HTML description |
| Lever | Public posting JSON, preserving qualification section headings |
| Ashby | Public job-board JSON, selecting the exact requested job ID |
| SmartRecruiters | Public posting JSON and qualification sections |
| Workday | Public job-detail JSON |
| Other employer websites, including Meta | Original HTML, JobPosting JSON-LD, original title metadata and qualification sections |

There is no company allowlist. Public HTTPS employer URLs use the shared path;
provider adapters handle different response formats. Unsafe/local destinations
and redirects are rejected, and response size and request time are bounded.
Python's standard library is sufficient; no API key or paid service is required.
The original verified employer title is used in Discord. Generic research titles
alone never establish a degree. Preferred or optional degrees and explicit
experience alternatives do not establish graduate-only eligibility.
An explicit “PhD or equivalent experience” result overrides conflicting feed
tags and displays “Graduate degree or equivalent experience — check the posting.”
It does not claim that a bachelor's degree alone qualifies. Seniority and the
amount of equivalent experience are outside this degree-routing classifier.

Requests are limited by `MAX_EMPLOYER_LOOKUPS` (default 40 per run). Results in
`degree_cache.json` record the source URL, employer title, evidence, status,
check time and expiry. Verified results last seven days, unverified results one
day, and fetch failures one hour. Expired results retry **when the listing is
next evaluated**; this does not automatically revisit already-posted jobs.
Legacy `unknown` cache entries cannot suppress employer verification.
This release uses the `employer:v3:` cache namespace for both employer and
legacy classification. Earlier parser decisions are ignored and re-evaluated
when a listing is selected; deleting the state files is unnecessary.

Every selected listing gets an employer-verification attempt, subject to the
shared request budget and cache. JavaScript-only pages, blocked or expired
postings, pages with no usable evidence, non-HTTPS URLs, and exhausted request
budgets still use the fallback rules above. They remain explicitly unverified;
an attempted lookup is not a confirmed degree. A large backfill may need a
higher lookup budget. This change does not guarantee that all graduate-only
roles are excluded and does not move existing Discord posts.

## How it avoids going stale

This is the part that matters, and the part an earlier version got wrong.

The feeds add ~100 relevant listings a day. If the bot can't keep up, a backlog
forms — and if it posts *oldest-first*, students only ever see the stalest end
of that queue. That is exactly what happened: the bot ran a permanent ~12-day
lag with a 2,500-job backlog that grew ~73 jobs/day.

Three mechanisms prevent it now:

1. **Freshness floor** (`MAX_JOB_AGE_DAYS`, default `7`). Anything older is
   marked seen and *never posted*. A backlog can't accumulate even if GitHub
   drops scheduled runs for days.
2. **Select newest, post oldest.** When a run exceeds `MAX_POSTS_PER_RUN`, the
   *freshest* jobs are selected, then posted oldest-first so the channel still
   reads chronologically and date headers ascend.
3. **Real headroom.** 40 posts/run against ~100 new jobs/day is ~2x capacity
   even on a bad day for GitHub's scheduler.

> **Don't set the cron below `*/30`.** GitHub deprioritises high-frequency
> schedules on public repos. A `*/15` cron was landing ~6 runs/day, not 96 —
> which is what let the backlog form in the first place.

## Notifications

**Every message is sent silently** (Discord's `SUPPRESS_NOTIFICATIONS` flag,
`4096`). Members get no push or desktop notification regardless of their
personal settings; the channel just quietly fills up. At ~100 posts/day this is
not optional — a channel that pings that often gets muted or left.

`PING_ROLE_ID` is **intentionally unset**. Only set it if you first create an
*opt-in, self-assignable* role — otherwise you are pinging the whole server
~100 times a day.

## Setup

### 1. Discord webhooks

For each channel: **Edit Channel → Integrations → Webhooks → New Webhook**,
then **Copy Webhook URL**.

Set the channels to view-only for members (deny *Send Messages* for
`@everyone`) — webhooks bypass this and post fine.

### 2. GitHub secrets

**Settings → Secrets and variables → Actions → Secrets**:

| Secret | Purpose |
|---|---|
| `DISCORD_WEBHOOK_INTERNSHIPS` | Bachelor's internships channel |
| `DISCORD_WEBHOOK_GRAD` | MS/PhD-required channel. **Optional** — if unset, graduate-only roles are skipped and marked seen |
| `DISCORD_WEBHOOK_NEWGRAD` | New-grad full-time channel |
| `DISCORD_WEBHOOK_URL` | Optional fallback used for internships + new-grad if those two are unset |

That's the entire required setup. Everything else has a working default.

After adding a previously missing graduate webhook, use a backfill with
`only_channels: grad` to recover skipped roles within the selected date window.

## Configuration

Tuning knobs live in `env:` in [`.github/workflows/main.yml`](.github/workflows/main.yml).

| Variable | Default | Purpose |
|---|---|---|
| `CATEGORY_ALLOWLIST` | `Software,AI/ML/Data,Product` | Categories to post |
| `CATEGORY_BLOCKLIST` | *(empty)* | Categories to always skip |
| `US_ONLY` | `1` | US / US-remote roles only |
| `DROP_EXPIRED_TERMS` | `1` | Skip past academic terms |
| `TERM_GRACE_DAYS` | `45` | How long after a term starts it still counts |
| `MAX_JOB_AGE_DAYS` | `7` | Freshness floor; `0` disables |
| `MAX_POSTS_PER_RUN` | `40` | Anti-flood cap per run |
| `LOCAL_STATES` | `NJ,NY,PA,CT,DE` | Marked 📍 and prioritized |
| `LOCAL_QUOTA_PCT` | `60` | Max share of an over-cap run given to local roles, so out-of-state roles aren't starved |
| `SILENT` | `1` | Suppress push notifications. **Keep on.** |
| `DATE_HEADERS` | `1` | Big 📅 banner at each day boundary |
| `TIMEZONE` | `America/New_York` | Which local day a job falls under |
| `PRUNE_AFTER_DAYS` | `45` | Forget closed jobs to bound state-file growth |
| `DEGREE_LOOKUP` | `0` | Legacy Greenhouse/Ashby/Lever description lookups |
| `EMPLOYER_LOOKUP` | `1` | Verify original employer titles and qualifications across the feed |
| `MAX_EMPLOYER_LOOKUPS` | `40` | Maximum fresh employer-page requests per run |
| `MAX_DEGREE_LOOKUPS` | `40` | Per-run HTTP budget for tier 3 |
| `ONLY_CHANNELS` | *(empty)* | Post to these channels only, e.g. `grad`. For backfilling a new channel |
| `BOOTSTRAP_POST_COUNT` | `5` | Posts per category on a first-ever run |
| `PING_ROLE_ID` | *(unset)* | Role to @mention. See **Notifications**. |

## Running it manually

**Actions → ACM Job Radar → Run workflow.** Four inputs:

- **`backfill_days`** — post every open job from the last N days (a number, or `all`), even ones already posted
- **`backfill_since`** — same, but from an exact `YYYY-MM-DD` (overrides the days box)
- **`only_channels`** — restrict posting to one channel, e.g. `grad`. Use this to populate a newly created channel without re-posting to the existing ones
- **`dry_run`** — log what *would* be posted and send nothing to Discord

Backfills post oldest → newest with a date header per day, so the archive reads
as dated sections. Budget ~2 seconds per 5 jobs (~10 min for 1,500).

### Testing locally

```bash
python3 -m unittest -v                            # offline regression/integration tests
DRY_RUN=1 python3 job_bot.py                        # what would post right now
DRY_RUN=1 BACKFILL_SINCE=2026-08-30 python3 job_bot.py
```

The bot is stdlib only, Python 3.9+. Tests also run without dependencies.
To measure statement and branch coverage, install the development-only
`coverage` package and run:

```bash
python3 -m coverage run --branch --source=job_bot,employer_degree -m unittest -v
python3 -m coverage report --fail-under=90
python3 scripts/replay_routing_audit.py             # frozen, offline audit replay
```

CI tests Python 3.9, 3.11 and 3.13 and enforces at least 90% combined statement
and branch coverage. The posting workflow uses Python 3.11 and runs the offline
suite before it can post. If tests fail, posting and the state-commit step are
skipped. Offline tests mock network delivery; live employer retrieval and live
Discord acceptance must be reported separately.

The suite includes all 120 recorded audit cases, with 119 clear references
scored and one ambiguous case excluded. This sample guided development, so a
perfect replay is a regression result, **not overall real-world accuracy**.
See [RELEASE.md](RELEASE.md) for deployment and recovery instructions.

## State

`seen_jobs.json` is the bot's memory, committed back to the repo after every
run. It maps job ID → `{company, title, first_seen}`, plus a `_headers` key
tracking the last date header announced per channel.

Jobs enter seen state **as each Discord message succeeds**, and state is saved
in a `finally` block if a later delivery raises. First runs and backfills no
longer pre-mark selected, unseen jobs as delivered. Recent unsent jobs remain
available to a later normal run; older jobs still obey the freshness floor.
Closed jobs are pruned after `PRUNE_AFTER_DAYS`.

An abrupt process kill, failure to save/push state, or a Discord response lost
after accepting a message can still cause duplicates. Delivery is not exactly
once. A backfill deliberately re-posts already-seen jobs, so retrying the same
backfill may repeat successful messages from that window.

To force a clean slate, delete `seen_jobs.json` — the next run bootstraps:
seeds everything as seen and posts only the freshest `BOOTSTRAP_POST_COUNT` per
category, instead of flooding the channel with thousands of old listings.

## Troubleshooting

| Symptom | Cause |
|---|---|
| Old jobs being posted | Check `[retire]` and `[queue]` in the run log. A large `[queue]` means inflow exceeds capacity — raise `MAX_POSTS_PER_RUN`. |
| Nothing posting | `[fetch]` counts at 0 means the upstream `dev` branch or `listings.json` path moved. |
| Runs far less often than the cron | Normal. GitHub throttles scheduled workflows on public repos; the freshness floor is what makes this harmless. |
| Everything posts twice | Two workflow files, or `seen_jobs.json` failing to commit. Check the *Save seen-jobs state* step. |
| Grad channel empty | Expected at ~9/day. Run a backfill with `only_channels: grad` to populate it. |
| A role is in the wrong channel | Check the title and `🎓 Degree` field. Verified employer evidence overrides feed tags. "Degree requirement unverified" means the bot could not confirm eligibility; the employer may still state a requirement. Existing Discord messages are not moved by routing changes. |
