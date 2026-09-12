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
channel rather than dropped.

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

Degree level is resolved in tiers, most confident first:

| Tier | Source | Coverage | Confidence |
|---|---|---|---|
| 1 | Simplify's `degrees` field | ~84% | authoritative |
| 2 | `phd`/`doctoral` in the title | ~10% of the rest | 100% precision on 1,731 held-out roles |
| 3 | *(disabled)* read the posting via free Greenhouse/Ashby/Lever JSON | ~0% | see below |
| 4 | **Abstain** → bachelors/newgrad, embed reads *"Not listed"* | ~16% of all listings | makes no claim |

**Tier 4 is the point.** Title-only classifiers measure ~70% precision — wrong
3 times in 10 — because 108 distinct titles appear in the data as *both*
grad-required and bachelor's-ok. "Software engineer intern" is literally both.
No amount of cleverness recovers information that isn't there, so the bot says
"not listed" instead of guessing.

### Why tier 3 is off (a useful negative result)

Reading the job description to settle the degree sounds obviously right, and it
validated beautifully: **27/27 correct** on real postings.

That validation was measured on the wrong population. Those were listings that
*have* a `degrees` field — which are, by construction, postings that state a
degree. The jobs tier 3 would actually run against are the ones with **no**
`degrees` field, and Simplify's field is empty *precisely because the posting
never said*. Measured on that population: **only 2 of 45 descriptions mention a
degree at all.** Real resolution rate ≈ 0–4%.

So it is off by default (`DEGREE_LOOKUP=0`) and the bot makes zero HTTP calls
for degrees. The code is kept and tested in case upstream coverage changes.

*If you add a classifier here, validate it on listings with no `degrees` field
— not on the labelled ones.*

The two failure modes aren't symmetric, which is why abstaining defaults to the
bachelor's channel: a grad-only role appearing there costs one wasted click,
while a bachelor's-eligible role hidden in the grad channel is never seen at
all.

Tier 3 lookups are budgeted per run (`MAX_DEGREE_LOOKUPS`) and cached in
`degree_cache.json`, so this costs roughly **6 HTTP calls a day and no API
key**.

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
| `DISCORD_WEBHOOK_GRAD` | MS/PhD-required channel. **Optional** — if unset, those roles stay in the other two channels |
| `DISCORD_WEBHOOK_NEWGRAD` | New-grad full-time channel |
| `DISCORD_WEBHOOK_URL` | Optional fallback used for internships + new-grad if those two are unset |

That's the entire required setup. Everything else has a working default.

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
| `DEGREE_LOOKUP` | `1` | Read postings to settle degree level. `0` = tiers 1–2 only |
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
DRY_RUN=1 python3 job_bot.py                        # what would post right now
DRY_RUN=1 BACKFILL_SINCE=2026-08-30 python3 job_bot.py
```

Stdlib only, Python 3.9+. No `pip install` required.

## State

`seen_jobs.json` is the bot's memory, committed back to the repo after every
run. It maps job ID → `{company, title, first_seen}`, plus a `_headers` key
tracking the last date header announced per channel.

Jobs are marked seen **as each Discord message succeeds**, and state is saved
in a `finally` block. A crash mid-batch therefore can't duplicate posts — the
undelivered jobs simply go out on the next run. Closed jobs are pruned after
`PRUNE_AFTER_DAYS` so the file doesn't grow without bound.

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
| A role is in the wrong channel | Check the `🎓 Degree` field. "Not listed" means the posting never stated one — the bot abstained rather than guessed. |
