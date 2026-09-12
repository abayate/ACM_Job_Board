#!/usr/bin/env python3
"""
ACM Job Radar - Kean University ACM chapter job bot.

Posts new CS/IT internships & new-grad roles to Discord via webhooks.
Runs on GitHub Actions; no server, no bot token.

Data sources (community-maintained, updated many times per day):
  - SimplifyJobs/Summer20XX-Internships  (current cycle auto-discovered)
  - SimplifyJobs/New-Grad-Positions

Pipeline:
  1. Pull listings.json from each source
  2. Keep only active, US-based, CS/IT roles for a current or future term
  3. Diff against seen_jobs.json (committed back to this repo by the Action)
  4. Post what's new to Discord, freshest first, local roles prioritized

Design notes worth knowing before you change anything:

  * FRESHNESS FLOOR (MAX_JOB_AGE_DAYS). Anything older than the floor is
    marked seen but never posted. This is what stops the channel from ever
    again grinding through a weeks-old backlog when GitHub drops scheduled
    runs. Without it the bot falls permanently behind and only ever shows
    students stale postings.

  * SELECT NEWEST, POST OLDEST. When a run has more jobs than
    MAX_POSTS_PER_RUN we *select* the newest ones, then *post* them
    oldest-first so the channel reads chronologically top-to-bottom and date
    headers ascend correctly.

  * SILENT BY DEFAULT. Every message carries Discord's silent flag, so members
    get zero push notifications no matter their personal settings. This is
    deliberate - a channel that fires ~100 pings a day gets muted or left.
    Only a configured PING_ROLE_ID mention is allowed to be loud, and that is
    intentionally left unset.

Backfill mode: set BACKFILL_DAYS to a number (or "all"), or BACKFILL_SINCE to
a YYYY-MM-DD date, to post every currently-open listing in that window even if
already marked seen. Trigger from the Actions tab: Run workflow.

Stdlib only - no pip installs required. Python 3.9+.
"""

from __future__ import annotations

import itertools
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

# Windows consoles default to cp1252 and choke on the emoji in our log lines.
# Harmless on the Linux runner; makes local dry-runs actually work.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Configuration (all via environment variables - see README)
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.abspath(__file__))

SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(ROOT, "seen_jobs.json"))


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[config] {name}={raw!r} is not a number; using {default}")
        return default


# Webhooks: separate channels if you want, or one DISCORD_WEBHOOK_URL for both.
WEBHOOKS = {
    "internship": os.environ.get("DISCORD_WEBHOOK_INTERNSHIPS")
    or os.environ.get("DISCORD_WEBHOOK_URL"),
    "new_grad": os.environ.get("DISCORD_WEBHOOK_NEWGRAD")
    or os.environ.get("DISCORD_WEBHOOK_URL"),
}

BOT_NAME = os.environ.get("BOT_NAME", "ACM Job Radar")

# Optional role to mention. Left UNSET on purpose: the channel is silent so
# members never get push notifications. Only set this if you create an
# opt-in, self-assignable role - otherwise you are pinging everyone ~100x/day.
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()

MAX_POSTS_PER_RUN = _env_int("MAX_POSTS_PER_RUN", 40)
BOOTSTRAP_POST_COUNT = _env_int("BOOTSTRAP_POST_COUNT", 5)

# Freshness floor. Jobs older than this are retired (marked seen, not posted).
# 0 disables it. See the module docstring for why this matters.
MAX_JOB_AGE_DAYS = _env_int("MAX_JOB_AGE_DAYS", 7)

# Backfill: "" = off, "30" = last 30 days, "all" = every open listing.
BACKFILL_DAYS = os.environ.get("BACKFILL_DAYS", "").strip().lower()
# Backfill from an exact date instead, e.g. "2026-08-30". Takes precedence.
BACKFILL_SINCE = os.environ.get("BACKFILL_SINCE", "").strip()

# Silent messages: no push notifications for members. Keep this on.
SILENT = _env_flag("SILENT", "1")

# Big date header above the first postings of each new day
DATE_HEADERS = _env_flag("DATE_HEADERS", "1")

TIMEZONE = os.environ.get("TIMEZONE", "America/New_York")
try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo(TIMEZONE)
except Exception:
    LOCAL_TZ = timezone.utc

# --- Relevance filters -----------------------------------------------------

# Only post roles physically in the US (or US-remote). Drops London, Toronto,
# Bangalore, etc. Set US_ONLY=0 to allow anywhere.
US_ONLY = _env_flag("US_ONLY", "1")

# Categories to post. Simplify's vocabulary is:
#   Software | AI/ML/Data | Hardware | Product | Quant
# Default keeps the CS/IT ones. Add "Hardware" if your chapter has computer
# engineering students; add "Quant" for quantitative finance roles.
CATEGORY_ALLOWLIST = {
    c.strip().lower()
    for c in os.environ.get("CATEGORY_ALLOWLIST", "Software,AI/ML/Data,Product").split(",")
    if c.strip()
}

# Legacy escape hatch, still honoured: categories to always skip.
CATEGORY_BLOCKLIST = {
    c.strip().lower()
    for c in os.environ.get("CATEGORY_BLOCKLIST", "").split(",")
    if c.strip()
}

# Drop internships whose term already started well in the past (e.g. a
# "Summer 2026" listing surfacing in September 2026).
DROP_EXPIRED_TERMS = _env_flag("DROP_EXPIRED_TERMS", "1")
TERM_GRACE_DAYS = _env_int("TERM_GRACE_DAYS", 45)

# Roles in these states get a marker and priority when a run is over cap.
LOCAL_STATES = [
    s.strip().upper()
    for s in os.environ.get("LOCAL_STATES", "NJ,NY,PA,CT,DE").split(",")
    if s.strip()
]

# Share of an over-cap run reserved for local roles. The remainder is kept for
# out-of-state postings so big-tech roles never get starved out by a local
# backlog; unused slots spill over either way. See select_to_post().
LOCAL_QUOTA_PCT = _env_int("LOCAL_QUOTA_PCT", 60)

# Drop seen-entries for jobs that closed this long ago, to stop the state file
# growing without bound. 0 disables pruning.
PRUNE_AFTER_DAYS = _env_int("PRUNE_AFTER_DAYS", 45)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

DRY_RUN = _env_flag("DRY_RUN", "0")
DRY_RUN_SAVE = _env_flag("DRY_RUN_SAVE", "0")

# Fallback if GitHub API discovery is unavailable. Discovery normally handles
# the annual SimplifyJobs repo rename on its own.
FALLBACK_INTERNSHIP_REPO = "Summer2027-Internships"

NEW_GRAD_REPO = "New-Grad-Positions"
LISTINGS_PATH = ".github/scripts/listings.json"

EMBEDS_PER_MESSAGE = 5        # Discord allows 10, but 5 keeps us under char limits
SLEEP_BETWEEN_MESSAGES = 2.0  # webhooks sustain ~30 req/min; this stays under

SUPPRESS_NOTIFICATIONS = 1 << 12  # Discord message flag for silent delivery

KIND_META = {
    "internship": {"label": "internship", "color": 0x57F287},   # green
    "new_grad": {"label": "new-grad role", "color": 0x5865F2},  # blurple
}
LOCAL_COLOR = 0xFEE75C  # gold - makes nearby roles pop in the feed

# Simplify emits both short and long category names for the same thing.
CATEGORY_DISPLAY = {
    "software engineering": "Software",
    "data science, ai & machine learning": "AI/ML/Data",
    "hardware engineering": "Hardware",
    "product management": "Product",
    "quantitative finance": "Quant",
}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC", "PR", "VI", "GU",
}

# Location strings Simplify uses that mean "US" without a state suffix.
US_TOKENS = {
    "nyc", "sf", "la", "new york", "united states", "usa", "us",
    "remote in usa", "remote in us", "remote",
}

_STATE_SUFFIX = re.compile(r",\s*([A-Z]{2})\s*$")
_TERM_RE = re.compile(r"(Spring|Summer|Fall|Winter)\s+(\d{4})", re.I)

# Roughly when each academic term starts, for the expired-term check.
_TERM_START = {"spring": (1, 15), "summer": (5, 15), "fall": (8, 15), "winter": (12, 1)}

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def http_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(
        url, headers={"User-Agent": "acm-job-radar", **(headers or {})}
    )
    last_err = None
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # GitHub's raw CDN resets connections fairly often; a bare retry
            # fixes it. Previously this crashed the whole run.
            last_err = e
            wait = 2 ** attempt
            print(f"[http] fetch failed ({e}) - retry {attempt + 1}/3 in {wait}s")
            time.sleep(wait)
    raise last_err


def discover_internship_repo() -> str:
    """Find the newest SimplifyJobs Summer20XX-Internships repo so the bot
    survives the annual repo rename without any code changes."""
    headers = {"Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    try:
        repos = http_json(
            "https://api.github.com/orgs/SimplifyJobs/repos?per_page=100", headers=headers
        )
        years = {}
        for r in repos:
            m = re.fullmatch(r"Summer(\d{4})-Internships", r.get("name", ""))
            if m:
                years[int(m.group(1))] = r["name"]
        if years:
            repo = years[max(years)]
            print(f"[discover] internship repo: SimplifyJobs/{repo}")
            return repo
    except Exception as e:  # rate limit, network, schema change - fall back
        print(f"[discover] GitHub API unavailable ({e}); using fallback repo")
    return FALLBACK_INTERNSHIP_REPO


def raw_listings_url(repo: str) -> str:
    return f"https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/{LISTINGS_PATH}"


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def clean_text(s: str) -> str:
    return (s or "").replace("</br>", ", ").replace("<br>", ", ").strip()


def display_category(raw: str | None) -> str | None:
    if not raw:
        return None
    return CATEGORY_DISPLAY.get(raw.strip().lower(), raw.strip())


def is_us_location(loc: str) -> bool:
    s = (loc or "").strip()
    if not s:
        return False
    if s.lower() in US_TOKENS:
        return True
    m = _STATE_SUFFIX.search(s)
    return bool(m and m.group(1) in US_STATES)


def is_local_location(loc: str) -> bool:
    s = (loc or "").strip()
    if s.lower() in ("nyc", "new york"):
        return "NY" in LOCAL_STATES
    m = _STATE_SUFFIX.search(s)
    return bool(m and m.group(1) in LOCAL_STATES)


def term_is_current(terms, today: date) -> bool:
    """False only when every term on a listing started well in the past.

    Unparseable or missing terms pass - we would rather show an odd listing
    than silently swallow a good one.
    """
    real = [t for t in terms if t and t.strip().upper() != "N/A"]
    if not real:
        return True
    cutoff = today - timedelta(days=TERM_GRACE_DAYS)
    for t in real:
        m = _TERM_RE.match(t.strip())
        if not m:
            return True
        season, year = m.group(1).lower(), int(m.group(2))
        month, day = _TERM_START[season]
        if date(year, month, day) >= cutoff:
            return True
    return False


def fetch_source(kind: str, repo: str, today: date, stats: dict):
    url = raw_listings_url(repo)
    data = http_json(url, timeout=60)
    jobs = []
    for j in data:
        if not (j.get("active") and j.get("is_visible", True)):
            continue
        if not j.get("id") or not j.get("url"):
            continue

        cat = display_category(j.get("category"))
        cat_l = (cat or "").lower()
        if CATEGORY_ALLOWLIST and cat_l not in CATEGORY_ALLOWLIST:
            stats["category"] += 1
            continue
        if cat_l in CATEGORY_BLOCKLIST:
            stats["category"] += 1
            continue

        locations = [clean_text(x) for x in (j.get("locations") or []) if clean_text(x)]
        if US_ONLY and not any(is_us_location(x) for x in locations):
            stats["non_us"] += 1
            continue

        terms = j.get("terms") or []
        if DROP_EXPIRED_TERMS and not term_is_current(terms, today):
            stats["expired_term"] += 1
            continue

        jobs.append(
            {
                "id": j["id"],
                "kind": kind,
                "company": clean_text(j.get("company_name", "Unknown")),
                "title": clean_text(j.get("title", "Unknown role")),
                "url": j["url"],
                "locations": locations,
                "terms": terms,
                "category": cat,
                "sponsorship": j.get("sponsorship"),
                "date_posted": j.get("date_posted") or j.get("date_updated") or 0,
                "is_local": any(is_local_location(x) for x in locations),
                "source_name": f"SimplifyJobs/{repo}",
            }
        )
    kept_local = sum(1 for j in jobs if j["is_local"])
    print(f"[fetch] {repo}: {len(data)} listings -> {len(jobs)} relevant ({kept_local} local)")
    return jobs


# ---------------------------------------------------------------------------
# Seen-state  (also stores "_headers": last date-header posted per channel)
# ---------------------------------------------------------------------------


def load_seen() -> dict:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict) -> None:
    if DRY_RUN and not DRY_RUN_SAVE:
        print(f"[dry-run] skipped saving state ({len(seen)} entries)")
        return
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0, sort_keys=True)
        f.write("\n")
    os.replace(tmp, SEEN_FILE)  # atomic: a killed run cannot leave a corrupt file
    print(f"[state] saved {len(seen)} entries -> {SEEN_FILE}")


def seen_entry(job: dict) -> dict:
    return {
        "company": job["company"],
        "title": job["title"],
        "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def prune_seen(seen: dict, live_ids, today: date) -> int:
    """Forget jobs that have closed and been gone a while.

    Keeps seen_jobs.json from growing forever (it is rewritten in full on
    every commit). We only drop entries old enough that a re-listing would be
    a genuinely different posting.
    """
    if not PRUNE_AFTER_DAYS:
        return 0
    cutoff = (today - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    dead = [
        k
        for k, v in seen.items()
        if k != "_headers"
        and k not in live_ids
        and isinstance(v, dict)
        and v.get("first_seen", "9999") < cutoff
    ]
    for k in dead:
        del seen[k]
    if dead:
        print(f"[prune] dropped {len(dead)} closed jobs older than {PRUNE_AFTER_DAYS}d")
    return len(dead)


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def local_day(ts: float) -> date:
    return datetime.fromtimestamp(ts or 0, tz=LOCAL_TZ).date()


def header_text(day: date) -> str:
    # "# " renders as a huge header in Discord messages
    return f"# \U0001F4C5 {day.strftime('%A, %B')} {day.day}, {day.year}"


def make_embed(job: dict) -> dict:
    meta = KIND_META[job["kind"]]

    locs = job["locations"]
    loc_str = ", ".join(locs[:6]) + (f" (+{len(locs) - 6} more)" if len(locs) > 6 else "")

    fields = [{"name": "\U0001F4CD Location", "value": (loc_str or "-")[:1024], "inline": True}]
    if job["terms"]:
        fields.append(
            {"name": "\U0001F5D3️ Term", "value": ", ".join(job["terms"])[:1024], "inline": True}
        )
    if job["category"]:
        fields.append(
            {"name": "\U0001F3F7️ Category", "value": job["category"][:1024], "inline": True}
        )
    spons = (job.get("sponsorship") or "").strip()
    if spons and spons.lower() != "other":
        fields.append({"name": "\U0001F6C2 Sponsorship", "value": spons[:1024], "inline": True})

    marker = "\U0001F4CD " if job["is_local"] else ""
    footer = f"via {job['source_name']}"
    if job["is_local"]:
        footer = "\U0001F4CD Near campus · " + footer

    embed = {
        "title": f"{marker}{job['company']} — {job['title']}"[:256],
        "url": job["url"],
        "color": LOCAL_COLOR if job["is_local"] else meta["color"],
        "fields": fields,
        "footer": {"text": footer},
    }
    if job["date_posted"]:
        embed["timestamp"] = datetime.fromtimestamp(
            job["date_posted"], tz=timezone.utc
        ).isoformat()
    return embed


def post_to_webhook(webhook: str, payload: dict) -> None:
    """POST one message, retrying rate limits, transient 5xx and network blips.

    Anything that escapes this kills the run, so the list of what we retry
    matters: a bare URLError used to abort mid-batch, and because state was
    only saved at the very end, every job already posted got posted again on
    the next run.
    """
    body = json.dumps(payload).encode("utf-8")
    url = webhook + ("&" if "?" in webhook else "?") + "wait=true"
    for attempt in range(6):
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json", "User-Agent": "acm-job-radar"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # Discord tells us exactly how long to wait
                try:
                    retry_after = float(json.loads(e.read().decode()).get("retry_after", 3))
                except Exception:
                    retry_after = 3.0
                print(f"[discord] rate limited, retrying in {retry_after:.1f}s")
                time.sleep(retry_after + 0.5)
                continue
            if 500 <= e.code < 600:
                wait = 2 ** attempt
                print(f"[discord] server error {e.code}, retrying in {wait}s")
                time.sleep(wait)
                continue
            raise  # a 4xx that is not 429 is our bug - surface it
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            wait = 2 ** attempt
            print(f"[discord] network error ({e}), retrying in {wait}s")
            time.sleep(wait)
    raise RuntimeError("Discord unreachable after 6 attempts")


def send_jobs(kind: str, jobs, seen: dict, headers_state: dict, backfill: bool = False) -> None:
    """Post a batch of one kind to its channel, oldest first, with a big date
    header above the first postings of each new day.

    Jobs are marked seen as each message lands, so a crash mid-batch can never
    cause duplicates on the next run.
    """
    if not jobs:
        return
    meta = KIND_META[kind]
    webhook = WEBHOOKS[kind]
    jobs = sorted(jobs, key=lambda j: j["date_posted"])

    plural = "s" if len(jobs) != 1 else ""
    n_local = sum(1 for j in jobs if j["is_local"])
    if backfill:
        summary = f"\U0001F4E6 **Backfill: {len(jobs)} open {meta['label']}{plural}** (oldest → newest)"
    else:
        summary = f"\U0001F680 **{len(jobs)} new {meta['label']}{plural}**"
    if n_local:
        summary += f" · \U0001F4CD {n_local} near campus"
    if PING_ROLE_ID:
        summary = f"<@&{PING_ROLE_ID}> {summary}"

    # Flatten to (day, chunk) up front so the progress counter is honest.
    chunks = []
    for day, group in itertools.groupby(jobs, key=lambda j: local_day(j["date_posted"])):
        day_jobs = list(group)
        for i in range(0, len(day_jobs), EMBEDS_PER_MESSAGE):
            chunks.append((day, day_jobs[i : i + EMBEDS_PER_MESSAGE]))
    total_chunks = len(chunks)

    last_header = headers_state.get(kind, "")
    first_message = True
    sent = 0
    prev_day = None

    for day, chunk in chunks:
        day_iso = day.isoformat()
        # Header on the first chunk of each new day. Guard against the epoch
        # bucket (jobs with no date) and against re-announcing a day we have
        # already headed in an earlier run.
        new_day = day != prev_day
        want_header = DATE_HEADERS and new_day and day.year > 1971 and day_iso > last_header
        if want_header:
            last_header = day_iso
        prev_day = day

        content_parts = []
        if first_message:
            content_parts.append(summary)
        if want_header:
            content_parts.append(header_text(day))

        loud = first_message and bool(PING_ROLE_ID)
        payload = {
            "username": BOT_NAME,
            "embeds": [make_embed(j) for j in chunk],
            "allowed_mentions": {
                "parse": [],
                "roles": [PING_ROLE_ID] if PING_ROLE_ID else [],
            },
        }
        if content_parts:
            payload["content"] = "\n".join(content_parts)
        if SILENT and not loud:
            payload["flags"] = SUPPRESS_NOTIFICATIONS

        if DRY_RUN:
            print(f"[dry-run] {kind} message {sent + 1}/{total_chunks}:")
            if payload.get("content"):
                print("    | " + payload["content"].replace("\n", " / "))
            for j in chunk:
                tag = "[LOCAL]" if j["is_local"] else "       "
                print(f"    {tag} {j['company']} - {j['title'][:52]}")
        else:
            post_to_webhook(webhook, payload)
            print(f"[discord] {kind}: {sent + 1}/{total_chunks} sent ({len(chunk)} jobs)")

        # Only now is the message durably in the channel.
        for j in chunk:
            seen[j["id"]] = seen_entry(j)
        headers_state[kind] = max(last_header, headers_state.get(kind, ""))

        sent += 1
        first_message = False
        if not DRY_RUN:
            time.sleep(SLEEP_BETWEEN_MESSAGES)


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


def select_to_post(new_jobs, cap: int):
    """Pick at most `cap` jobs: local roles favoured, then the freshest others.

    Selecting newest-first is the whole point - the old bot selected
    oldest-first, so with any backlog students only ever saw the stalest
    postings.

    Local roles get first call on LOCAL_QUOTA_PCT of the slots rather than all
    of them. Letting them take everything starves out-of-state roles whenever
    there is a backlog, and plenty of students want the big-tech postings that
    come with relocation and travel stipends. Unused slots on either side spill
    over to the other, so the cap is always filled.
    """
    if cap <= 0 or len(new_jobs) <= cap:
        return list(new_jobs)
    by_new = lambda j: j["date_posted"]  # noqa: E731
    local = sorted((j for j in new_jobs if j["is_local"]), key=by_new, reverse=True)
    rest = sorted((j for j in new_jobs if not j["is_local"]), key=by_new, reverse=True)

    quota = max(1, cap * LOCAL_QUOTA_PCT // 100)
    picked = local[:quota]
    picked += rest[: cap - len(picked)]
    if len(picked) < cap:  # not enough non-local to fill - give the rest back
        picked += local[quota : quota + (cap - len(picked))]
    return picked


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    missing = [k for k, v in WEBHOOKS.items() if not v]
    if missing and not DRY_RUN:
        print(
            "ERROR: no webhook configured for: "
            + ", ".join(missing)
            + "\nSet DISCORD_WEBHOOK_INTERNSHIPS / DISCORD_WEBHOOK_NEWGRAD "
            "(or a single DISCORD_WEBHOOK_URL for both) as GitHub Actions secrets."
        )
        return 1

    today = datetime.now(tz=LOCAL_TZ).date()

    # Resolve the backfill window before doing any work.
    backfill_cutoff = None
    if BACKFILL_SINCE:
        try:
            d = datetime.strptime(BACKFILL_SINCE, "%Y-%m-%d").replace(tzinfo=LOCAL_TZ)
            backfill_cutoff = d.timestamp()
        except ValueError:
            print(f"ERROR: BACKFILL_SINCE must be YYYY-MM-DD, got '{BACKFILL_SINCE}'")
            return 1
    elif BACKFILL_DAYS:
        if BACKFILL_DAYS == "all":
            backfill_cutoff = 0.0
        else:
            try:
                backfill_cutoff = time.time() - int(BACKFILL_DAYS) * 86400
            except ValueError:
                print(f"ERROR: backfill must be a number of days or 'all', got '{BACKFILL_DAYS}'")
                return 1

    print(
        f"[config] categories={sorted(CATEGORY_ALLOWLIST)} us_only={US_ONLY} "
        f"max_age={MAX_JOB_AGE_DAYS}d cap={MAX_POSTS_PER_RUN} local={LOCAL_STATES} "
        f"silent={SILENT}"
    )

    stats = {"category": 0, "non_us": 0, "expired_term": 0}
    internship_repo = discover_internship_repo()
    all_jobs = fetch_source("internship", internship_repo, today, stats) + fetch_source(
        "new_grad", NEW_GRAD_REPO, today, stats
    )
    print(
        f"[filter] skipped {stats['category']} off-topic, {stats['non_us']} non-US, "
        f"{stats['expired_term']} expired-term listings"
    )
    if not all_jobs:
        print("ERROR: no listings survived filtering - refusing to touch state")
        return 1

    bootstrap = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    headers_state = seen.setdefault("_headers", {})
    new_jobs = [j for j in all_jobs if j["id"] not in seen]

    if backfill_cutoff is not None:
        to_post = [j for j in all_jobs if j["date_posted"] >= backfill_cutoff]
        # Everything open counts as announced once the backfill finishes, so
        # steady state resumes from a clean slate.
        for j in all_jobs:
            seen.setdefault(j["id"], seen_entry(j))
        est = math.ceil(len(to_post) / EMBEDS_PER_MESSAGE) * SLEEP_BETWEEN_MESSAGES / 60
        window = BACKFILL_SINCE or BACKFILL_DAYS
        print(
            f"[backfill] window={window} -> posting {len(to_post)} of "
            f"{len(all_jobs)} open listings (~{est:.0f} min)"
        )
    elif bootstrap:
        print(f"[bootstrap] first run - seeding {len(all_jobs)} listings as seen")
        for j in all_jobs:
            seen[j["id"]] = seen_entry(j)
        to_post = []
        for kind in KIND_META:
            to_post.extend(
                sorted(
                    (j for j in new_jobs if j["kind"] == kind),
                    key=lambda j: j["date_posted"],
                    reverse=True,
                )[:BOOTSTRAP_POST_COUNT]
            )
    else:
        # Retire anything past the freshness floor: mark it seen, never post
        # it. This is the guard that stops a stale backlog from rebuilding.
        if MAX_JOB_AGE_DAYS:
            floor = time.time() - MAX_JOB_AGE_DAYS * 86400
            stale = [j for j in new_jobs if j["date_posted"] < floor]
            for j in stale:
                seen[j["id"]] = seen_entry(j)
            if stale:
                print(
                    f"[retire] {len(stale)} listings older than {MAX_JOB_AGE_DAYS}d - not posting"
                )
            new_jobs = [j for j in new_jobs if j["date_posted"] >= floor]

        to_post = select_to_post(new_jobs, MAX_POSTS_PER_RUN)
        held = len(new_jobs) - len(to_post)
        if held:
            print(f"[queue] {held} held for next run (cap {MAX_POSTS_PER_RUN})")
        print(f"[run] {len(new_jobs)} new, posting {len(to_post)}")

    try:
        for kind in KIND_META:
            send_jobs(
                kind,
                [j for j in to_post if j["kind"] == kind],
                seen,
                headers_state,
                backfill=backfill_cutoff is not None,
            )
    finally:
        # Always persist what actually went out, even if Discord died halfway.
        prune_seen(seen, {j["id"] for j in all_jobs}, today)
        save_seen(seen)

    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
