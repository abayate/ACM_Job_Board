#!/usr/bin/env python3
"""
ACM Job Radar - Kean University ACM chapter job bot.

Posts new CS/IT internships & new-grad roles to Discord via webhooks.
Runs on GitHub Actions; no server, no bot token, no paid API.

Data sources (community-maintained, updated many times per day):
  - SimplifyJobs/Summer20XX-Internships  (current cycle auto-discovered)
  - SimplifyJobs/New-Grad-Positions

Pipeline:
  1. Pull listings.json from each source
  2. Keep only active, US-based, CS/IT roles for a current or future term
  3. Diff against seen_jobs.json (committed back to this repo by the Action)
  4. Work out the degree level, route to one of three channels, and post

THREE CHANNELS (degree-first routing, see route()):
  bachelors  - internships a bachelor's student can take
  grad       - roles that REQUIRE an MS/PhD (internship or full-time alike)
  newgrad    - entry-level full-time roles open to bachelor's holders

  Note "grad" means grad-degree-REQUIRED, not grad-relevant. ~1,000 roles
  accept Bachelor's *and* Master's/PhD; those live in the bachelors/newgrad
  channels, because routing them to both would duplicate a third of the feed.
  Say so in the grad channel's topic or grad students will think it's their
  whole world.

Design notes worth knowing before you change anything:

  * FRESHNESS FLOOR (MAX_JOB_AGE_DAYS). Anything older than the floor is
    marked seen but never posted. This is what stops the channel from ever
    again grinding through a weeks-old backlog when GitHub drops scheduled
    runs. Without it the bot falls permanently behind and only ever shows
    students stale postings.

  * SELECT NEWEST, POST OLDEST. When a run has more jobs than
    MAX_POSTS_PER_RUN we *select* the newest ones, then *post* them
    oldest-first so the channel reads chronologically and date headers ascend.

  * SILENT BY DEFAULT. Every message carries Discord's silent flag, so members
    get zero push notifications no matter their personal settings. At ~100
    posts a day this is not optional - a channel that pings that often gets
    muted or left. Only a configured PING_ROLE_ID mention may be loud, and it
    is intentionally left unset.

  * NEVER GUESS A DEGREE. Degree level is resolved in tiers, and when nothing
    is confident the job is routed to the bachelors/newgrad channel and the
    embed says "not listed" rather than implying a level we did not verify.
    Measured on held-out data: title-only classifiers top out around 70%
    precision, i.e. wrong 3 times in 10, which is worse than saying nothing.
    See degree_level().

Backfill mode: set BACKFILL_DAYS to a number (or "all"), or BACKFILL_SINCE to
a YYYY-MM-DD date, to post every currently-open listing in that window even if
already marked seen. Trigger from the Actions tab: Run workflow.

Stdlib only - no pip installs, no API keys. Python 3.9+.
"""

from __future__ import annotations

import html
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
from urllib.parse import urlparse

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
DEGREE_CACHE_FILE = os.environ.get(
    "DEGREE_CACHE_FILE", os.path.join(ROOT, "degree_cache.json")
)


def _env_flag(name: str, default: str = "1") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        print(f"[config] {name}={raw!r} is not a number; using {default}")
        return default


_ONE = os.environ.get("DISCORD_WEBHOOK_URL")

# Three channels. A missing grad webhook is fine - see route().
WEBHOOKS = {
    "bachelors": os.environ.get("DISCORD_WEBHOOK_INTERNSHIPS") or _ONE,
    "grad": os.environ.get("DISCORD_WEBHOOK_GRAD") or None,
    "newgrad": os.environ.get("DISCORD_WEBHOOK_NEWGRAD") or _ONE,
}

BOT_NAME = os.environ.get("BOT_NAME", "ACM Job Radar")

# Optional role to mention. Left UNSET on purpose: the channels are silent so
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

US_ONLY = _env_flag("US_ONLY", "1")

# Simplify's category vocabulary:
#   Software | AI/ML/Data | Hardware | Product | Quant
CATEGORY_ALLOWLIST = {
    c.strip().lower()
    for c in os.environ.get("CATEGORY_ALLOWLIST", "Software,AI/ML/Data,Product").split(",")
    if c.strip()
}
CATEGORY_BLOCKLIST = {
    c.strip().lower()
    for c in os.environ.get("CATEGORY_BLOCKLIST", "").split(",")
    if c.strip()
}

DROP_EXPIRED_TERMS = _env_flag("DROP_EXPIRED_TERMS", "1")
TERM_GRACE_DAYS = _env_int("TERM_GRACE_DAYS", 45)

LOCAL_STATES = [
    s.strip().upper()
    for s in os.environ.get("LOCAL_STATES", "NJ,NY,PA,CT,DE").split(",")
    if s.strip()
]
LOCAL_QUOTA_PCT = _env_int("LOCAL_QUOTA_PCT", 60)

PRUNE_AFTER_DAYS = _env_int("PRUNE_AFTER_DAYS", 45)

# Restrict posting to specific channels, e.g. "grad". Mainly for backfilling a
# newly-created channel without re-posting everything to the existing ones.
ONLY_CHANNELS = {
    c.strip().lower()
    for c in os.environ.get("ONLY_CHANNELS", "").split(",")
    if c.strip()
}

# --- Degree lookup ---------------------------------------------------------

# Read job descriptions from Greenhouse/Ashby/Lever public JSON endpoints to
# settle the degree level when the feed does not say. Free, no API key.
DEGREE_LOOKUP = _env_flag("DEGREE_LOOKUP", "1")
# Per-run budget, so a backfill can't fire thousands of HTTP requests.
MAX_DEGREE_LOOKUPS = _env_int("MAX_DEGREE_LOOKUPS", 40)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

DRY_RUN = _env_flag("DRY_RUN", "0")
DRY_RUN_SAVE = _env_flag("DRY_RUN_SAVE", "0")

FALLBACK_INTERNSHIP_REPO = "Summer2027-Internships"
NEW_GRAD_REPO = "New-Grad-Positions"
LISTINGS_PATH = ".github/scripts/listings.json"

EMBEDS_PER_MESSAGE = 5        # Discord allows 10, but 5 keeps us under char limits
SLEEP_BETWEEN_MESSAGES = 2.0  # webhooks sustain ~30 req/min; this stays under

SUPPRESS_NOTIFICATIONS = 1 << 12  # Discord message flag for silent delivery

CHANNELS = {
    "bachelors": {"label": "internship", "color": 0x57F287},        # green
    "grad": {"label": "MS/PhD role", "color": 0xEB459E},            # pink
    "newgrad": {"label": "new-grad role", "color": 0x5865F2},       # blurple
}
LOCAL_COLOR = 0xFEE75C  # gold - makes nearby roles pop in the feed

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
US_TOKENS = {
    "nyc", "sf", "la", "new york", "united states", "usa", "us",
    "remote in usa", "remote in us", "remote",
}

# Degree vocabulary as Simplify emits it.
UNDERGRAD_DEGREES = {"Bachelor's", "Associate's", "Bootcamp", "Incomplete", "Certificate"}
GRAD_DEGREES = {"Master's", "PhD", "MBA", "JD", "MD", "PharmD", "DO"}

_STATE_SUFFIX = re.compile(r",\s*([A-Z]{2})\s*$")
_TERM_RE = re.compile(r"(Spring|Summer|Fall|Winter)\s+(\d{4})", re.I)
_TERM_START = {"spring": (1, 15), "summer": (5, 15), "fall": (8, 15), "winter": (12, 1)}

# "PhD" in a title is the one title signal that is never wrong: measured at
# 100% precision on 1,731 held-out labelled roles. Everything wordier ("research
# scientist", ML terms) drops below 75% and is deliberately not used.
_PHD_TITLE = re.compile(r"\bph\.?\s?d\.?\b|\bdoctoral\b", re.I)

# Degree words only count inside a sentence that is actually stating a
# requirement. Without this guard, "MS" matches inside "LLMs" and boilerplate
# HTML, which is what dragged a naive version down to 59% accuracy.
_DEGREE_CTX = re.compile(
    r"\b(pursu\w+|enrolled|degree|candidate|qualif\w+|require\w*|seeking"
    r"|working toward|studying|graduating)\b",
    re.I,
)
_BACH_RE = re.compile(
    r"\b(bachelor'?s?|undergraduate degree|b\.s\.|bs/ms|associate'?s degree)\b", re.I
)
_GRAD_RE = re.compile(r"\b(ph\.?d\.?|doctoral|master'?s|m\.s\.|msc)\b", re.I)

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def http_text(url: str, headers: dict | None = None, timeout: int = 30, retries: int = 4) -> str:
    req = urllib.request.Request(
        url, headers={"User-Agent": "acm-job-radar", **(headers or {})}
    )
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # HTTPError subclasses URLError, so without this a dead posting
            # (404) would burn the whole retry budget sleeping. Only 429 and
            # 5xx are worth retrying.
            if e.code != 429 and e.code < 500:
                raise
            last_err = e
            if attempt == retries - 1:
                break
            wait = 2 ** attempt
            print(f"[http] HTTP {e.code} - retry {attempt + 1}/{retries - 1} in {wait}s")
            time.sleep(wait)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            # GitHub's raw CDN resets connections fairly often; a bare retry
            # fixes it. Previously this crashed the whole run.
            last_err = e
            if attempt == retries - 1:
                break
            wait = 2 ** attempt
            print(f"[http] fetch failed ({e}) - retry {attempt + 1}/{retries - 1} in {wait}s")
            time.sleep(wait)
    raise last_err


def http_json(url: str, headers: dict | None = None, timeout: int = 30, retries: int = 4):
    return json.loads(http_text(url, headers, timeout, retries))


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
    data = http_json(raw_listings_url(repo), timeout=60)
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
                "degrees": j.get("degrees") or [],
                "category": cat,
                "sponsorship": j.get("sponsorship"),
                "date_posted": j.get("date_posted") or j.get("date_updated") or 0,
                "is_local": any(is_local_location(x) for x in locations),
                "source_name": f"SimplifyJobs/{repo}",
            }
        )
    n_local = sum(1 for j in jobs if j["is_local"])
    print(f"[fetch] {repo}: {len(data)} listings -> {len(jobs)} relevant ({n_local} local)")
    return jobs


# ---------------------------------------------------------------------------
# Degree resolution
# ---------------------------------------------------------------------------


def degree_from_feed(job: dict):
    """Tier 1: Simplify's own degrees list. Authoritative when present."""
    dg = set(job.get("degrees") or [])
    if not dg:
        return None
    if dg & UNDERGRAD_DEGREES:
        return "bach"
    if dg & GRAD_DEGREES:
        return "grad"
    return None


def degree_from_title(job: dict):
    """Tier 2: only the PhD signal, which measured 100% precision."""
    return "grad" if _PHD_TITLE.search(job["title"]) else None


def ats_endpoint(url: str):
    """Map a posting URL to a free, keyless JSON endpoint.

    Greenhouse, Ashby and Lever all publish their job boards openly. Together
    they cover ~40% of the listings whose degree the feed omits; everything
    else abstains rather than guessing.
    """
    try:
        p = urlparse(url)
    except Exception:
        return None
    host = p.netloc.replace("www.", "").lower()
    parts = [x for x in p.path.split("/") if x]
    if "ashbyhq.com" in host and len(parts) >= 2:
        return ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}", parts[1])
    if "lever.co" in host and len(parts) >= 2:
        return ("lever", f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", None)
    if "greenhouse.io" in host and len(parts) >= 3:
        return ("gh", f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[-1]}", None)
    return None


def _strip(text: str) -> str:
    for _ in range(3):  # these feeds are double-escaped surprisingly often
        text = html.unescape(text)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))


def fetch_description(url: str, boards: dict):
    """Return plain-text job description, or None. Never raises."""
    ep = ats_endpoint(url)
    if not ep:
        return None
    kind, api, token = ep
    try:
        if kind == "ashby":
            if api not in boards:  # one fetch serves every role at that company
                boards[api] = http_json(api, timeout=20, retries=2)
            hit = [x for x in boards[api].get("jobs", []) if x.get("id") == token]
            return _strip(hit[0].get("descriptionPlain", "")) if hit else None
        d = http_json(api, timeout=20, retries=2)
        if kind == "lever":
            return _strip((d.get("descriptionPlain") or d.get("description") or "")
                          + " " + json.dumps(d.get("lists", [])))
        return _strip(d.get("content", ""))
    except Exception:
        return None  # dead posting, 404, schema drift - abstain


def degree_from_description(text: str):
    """Tier 3: read the posting itself. Abstains unless it is confident.

    Only sentences that both mention a degree AND read like a requirement are
    considered. If any of them allow a bachelor's, the role is undergrad-open;
    if they only ever name graduate degrees, it is grad-required; otherwise we
    return None and the caller falls back to 'not listed'.
    """
    if not text or len(text) < 200:
        return None
    # Deliberately NOT splitting on ":" - "Required qualifications: PhD in ML"
    # must stay one chunk, or the requirement word and the degree word land in
    # different pieces and the whole thing abstains.
    sents = [s for s in re.split(r"(?<=[.;])\s+|\n|•", text) if len(s) < 400]
    ctx = [
        s for s in sents
        if _DEGREE_CTX.search(s) and (_BACH_RE.search(s) or _GRAD_RE.search(s))
    ]
    if not ctx:
        return None
    if any(_BACH_RE.search(s) for s in ctx):
        return "bach"
    if any(_GRAD_RE.search(s) for s in ctx):
        return "grad"
    return None


def resolve_degrees(jobs, cache: dict) -> dict:
    """Attach job["degree"] ("grad" | "bach" | None) and job["degree_src"]."""
    counts = {"feed": 0, "title": 0, "posting": 0, "cached": 0, "unknown": 0}
    boards: dict = {}
    budget = MAX_DEGREE_LOOKUPS if DEGREE_LOOKUP else 0
    for j in jobs:
        lvl, src = degree_from_feed(j), "feed"
        if lvl is None:
            lvl, src = degree_from_title(j), "title"
        if lvl is None and j["id"] in cache:
            v = cache[j["id"]]
            lvl, src = (v if v != "unknown" else None), "cached"
        elif lvl is None and budget > 0:
            budget -= 1
            lvl = degree_from_description(fetch_description(j["url"], boards))
            cache[j["id"]] = lvl or "unknown"
            src = "posting"
        if lvl is None:
            src = "unknown"
        j["degree"], j["degree_src"] = lvl, src
        counts[src] = counts.get(src, 0) + 1
    used = MAX_DEGREE_LOOKUPS - budget if DEGREE_LOOKUP else 0
    print(
        f"[degree] feed={counts['feed']} title={counts['title']} "
        f"posting={counts['posting']} cached={counts['cached']} "
        f"not-listed={counts['unknown']} ({used} lookups)"
    )
    return counts


def route(job: dict) -> str:
    """Pick a channel. Degree wins over internship-vs-full-time.

    Falls back to the two-channel layout when no grad webhook is configured,
    so adding the third channel is a pure config change.
    """
    if job["degree"] == "grad" and WEBHOOKS.get("grad"):
        return "grad"
    return "bachelors" if job["kind"] == "internship" else "newgrad"


# ---------------------------------------------------------------------------
# Seen-state  (also stores "_headers": last date-header posted per channel)
# ---------------------------------------------------------------------------


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def load_seen() -> dict:
    seen = _load_json(SEEN_FILE, {})
    # Migrate v3 header keys (by source) to v4 keys (by channel).
    h = seen.get("_headers")
    if isinstance(h, dict):
        if "internship" in h:
            h.setdefault("bachelors", h.pop("internship"))
        if "new_grad" in h:
            h.setdefault("newgrad", h.pop("new_grad"))
    return seen


def _save_json(path, data, label):
    if DRY_RUN and not DRY_RUN_SAVE:
        print(f"[dry-run] skipped saving {label} ({len(data)} entries)")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=0, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)  # atomic: a killed run cannot leave a corrupt file
    print(f"[state] saved {len(data)} {label} -> {path}")


def seen_entry(job: dict) -> dict:
    return {
        "company": job["company"],
        "title": job["title"],
        "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


def prune_seen(seen: dict, live_ids, today: date) -> int:
    """Forget jobs that have closed and been gone a while, so the state file
    (rewritten in full on every commit) does not grow without bound."""
    if not PRUNE_AFTER_DAYS:
        return 0
    cutoff = (today - timedelta(days=PRUNE_AFTER_DAYS)).isoformat()
    dead = [
        k for k, v in seen.items()
        if k != "_headers" and k not in live_ids and isinstance(v, dict)
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
    return f"# \U0001F4C5 {day.strftime('%A, %B')} {day.day}, {day.year}"


def degree_display(job: dict) -> str:
    """What we actually know, never more. See the module docstring."""
    if job["degree_src"] == "feed":
        return ", ".join(job["degrees"])
    if job["degree"] == "grad":
        return "MS/PhD required"
    if job["degree"] == "bach":
        return "Bachelor's OK"
    return "Not listed — check the posting"


def make_embed(job: dict, channel: str) -> dict:
    locs = job["locations"]
    loc_str = ", ".join(locs[:6]) + (f" (+{len(locs) - 6} more)" if len(locs) > 6 else "")

    fields = [{"name": "\U0001F4CD Location", "value": (loc_str or "-")[:1024], "inline": True}]
    if job["terms"]:
        fields.append({"name": "\U0001F5D3️ Term",
                       "value": ", ".join(job["terms"])[:1024], "inline": True})
    fields.append({"name": "\U0001F393 Degree", "value": degree_display(job)[:1024],
                   "inline": True})
    if job["category"]:
        fields.append({"name": "\U0001F3F7️ Category",
                       "value": job["category"][:1024], "inline": True})
    spons = (job.get("sponsorship") or "").strip()
    if spons and spons.lower() != "other":
        fields.append({"name": "\U0001F6C2 Sponsorship", "value": spons[:1024], "inline": True})

    marker = "\U0001F4CD " if job["is_local"] else ""
    footer = f"via {job['source_name']}"
    if job["is_local"]:
        footer = "\U0001F4CD Near campus · " + footer
    # In the grad channel, flag whether it's an internship or a full-time role,
    # since that channel deliberately mixes both.
    if channel == "grad":
        footer = ("Internship" if job["kind"] == "internship" else "Full-time") + " · " + footer

    embed = {
        "title": f"{marker}{job['company']} — {job['title']}"[:256],
        "url": job["url"],
        "color": LOCAL_COLOR if job["is_local"] else CHANNELS[channel]["color"],
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
            url, data=body,
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


def send_jobs(channel: str, jobs, seen: dict, headers_state: dict, backfill: bool = False) -> None:
    """Post a batch to one channel, oldest first, with a big date header above
    the first postings of each new day.

    Jobs are marked seen as each message lands, so a crash mid-batch can never
    cause duplicates on the next run.
    """
    if not jobs:
        return
    meta = CHANNELS[channel]
    webhook = WEBHOOKS[channel]
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
    total = len(chunks)

    last_header = headers_state.get(channel, "")
    first_message = True
    sent = 0
    prev_day = None

    for day, chunk in chunks:
        day_iso = day.isoformat()
        # Header on the first chunk of each new day. Guard the epoch bucket
        # (jobs with no date) and days we already headed in an earlier run.
        want_header = (
            DATE_HEADERS and day != prev_day and day.year > 1971 and day_iso > last_header
        )
        if want_header:
            last_header = day_iso
        prev_day = day

        content = []
        if first_message:
            content.append(summary)
        if want_header:
            content.append(header_text(day))

        loud = first_message and bool(PING_ROLE_ID)
        payload = {
            "username": BOT_NAME,
            "embeds": [make_embed(j, channel) for j in chunk],
            "allowed_mentions": {"parse": [], "roles": [PING_ROLE_ID] if PING_ROLE_ID else []},
        }
        if content:
            payload["content"] = "\n".join(content)
        if SILENT and not loud:
            payload["flags"] = SUPPRESS_NOTIFICATIONS

        if DRY_RUN:
            print(f"[dry-run] {channel} message {sent + 1}/{total}:")
            if payload.get("content"):
                print("    | " + payload["content"].replace("\n", " / "))
            for j in chunk:
                tag = "[LOCAL]" if j["is_local"] else "       "
                print(f"    {tag} {j['company']} - {j['title'][:44]} [{degree_display(j)}]")
        else:
            post_to_webhook(webhook, payload)
            print(f"[discord] {channel}: {sent + 1}/{total} sent ({len(chunk)} jobs)")

        # Only now is the message durably in the channel.
        for j in chunk:
            seen[j["id"]] = seen_entry(j)
        headers_state[channel] = max(last_header, headers_state.get(channel, ""))

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
    over, so the cap is always filled.
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
    required = ["bachelors", "newgrad"]  # grad is optional; route() falls back
    missing = [k for k in required if not WEBHOOKS.get(k)]
    if missing and not DRY_RUN:
        print(
            "ERROR: no webhook configured for: " + ", ".join(missing)
            + "\nSet DISCORD_WEBHOOK_INTERNSHIPS / DISCORD_WEBHOOK_NEWGRAD "
            "(or a single DISCORD_WEBHOOK_URL for both) as GitHub Actions secrets."
        )
        return 1
    if not WEBHOOKS.get("grad"):
        print("[config] no DISCORD_WEBHOOK_GRAD - MS/PhD roles stay in the other channels")

    today = datetime.now(tz=LOCAL_TZ).date()

    backfill_cutoff = None
    if BACKFILL_SINCE:
        try:
            backfill_cutoff = datetime.strptime(
                BACKFILL_SINCE, "%Y-%m-%d"
            ).replace(tzinfo=LOCAL_TZ).timestamp()
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
        f"silent={SILENT} degree_lookup={DEGREE_LOOKUP}"
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
    degree_cache = _load_json(DEGREE_CACHE_FILE, {})
    new_jobs = [j for j in all_jobs if j["id"] not in seen]

    if backfill_cutoff is not None:
        to_post = [j for j in all_jobs if j["date_posted"] >= backfill_cutoff]
        for j in all_jobs:
            seen.setdefault(j["id"], seen_entry(j))
        est = math.ceil(len(to_post) / EMBEDS_PER_MESSAGE) * SLEEP_BETWEEN_MESSAGES / 60
        print(
            f"[backfill] window={BACKFILL_SINCE or BACKFILL_DAYS} -> posting "
            f"{len(to_post)} of {len(all_jobs)} open listings (~{est:.0f} min)"
        )
    elif bootstrap:
        print(f"[bootstrap] first run - seeding {len(all_jobs)} listings as seen")
        for j in all_jobs:
            seen[j["id"]] = seen_entry(j)
        to_post = []
        for kind in ("internship", "new_grad"):
            to_post.extend(
                sorted((j for j in new_jobs if j["kind"] == kind),
                       key=lambda j: j["date_posted"], reverse=True)[:BOOTSTRAP_POST_COUNT]
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
                print(f"[retire] {len(stale)} listings older than {MAX_JOB_AGE_DAYS}d - not posting")
            new_jobs = [j for j in new_jobs if j["date_posted"] >= floor]

        to_post = select_to_post(new_jobs, MAX_POSTS_PER_RUN)
        held = len(new_jobs) - len(to_post)
        if held:
            print(f"[queue] {held} held for next run (cap {MAX_POSTS_PER_RUN})")
        print(f"[run] {len(new_jobs)} new, posting {len(to_post)}")

    # Degree is resolved only for what we're about to post, which keeps the
    # HTTP lookups to a handful per run instead of thousands.
    resolve_degrees(to_post, degree_cache)
    for j in to_post:
        j["channel"] = route(j)
    if to_post:
        by_ch = {c: sum(1 for j in to_post if j["channel"] == c) for c in CHANNELS}
        print("[route] " + "  ".join(f"{c}={n}" for c, n in by_ch.items()))
    if ONLY_CHANNELS:
        print(f"[route] restricted to: {sorted(ONLY_CHANNELS)}")

    try:
        for channel in CHANNELS:
            if not WEBHOOKS.get(channel):
                continue
            if ONLY_CHANNELS and channel not in ONLY_CHANNELS:
                continue
            send_jobs(
                channel,
                [j for j in to_post if j["channel"] == channel],
                seen, headers_state,
                backfill=backfill_cutoff is not None,
            )
    finally:
        # Always persist what actually went out, even if Discord died halfway.
        prune_seen(seen, {j["id"] for j in all_jobs}, today)
        _save_json(SEEN_FILE, seen, "seen entries")
        _save_json(DEGREE_CACHE_FILE, degree_cache, "degree lookups")

    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
