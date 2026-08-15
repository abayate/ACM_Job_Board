#!/usr/bin/env python3
"""
ACM Job Radar 📡
Posts new tech internships & new-grad roles to your Discord server.

Data sources (community-maintained, updated many times per day):
  • SimplifyJobs/Summer20XX-Internships  (current cycle auto-discovered)
  • SimplifyJobs/New-Grad-Positions

How it works:
  1. Pull listings.json from each source
  2. Diff against seen_jobs.json (committed back to this repo by the Action)
  3. Post anything new to Discord via webhook embeds

Backfill mode: set BACKFILL_DAYS to a number (or "all") to post every
currently-open listing from that window, even ones already marked seen.
Trigger it from the Actions tab: Run workflow → fill in the backfill box.

Stdlib only — no pip installs required. Python 3.9+.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration (all via environment variables — see README)
# ---------------------------------------------------------------------------

ROOT = os.path.dirname(os.path.abspath(__file__))

SEEN_FILE = os.environ.get("SEEN_FILE", os.path.join(ROOT, "seen_jobs.json"))

# Webhooks: separate channels if you want, or one DISCORD_WEBHOOK_URL for both.
WEBHOOKS = {
    "internship": os.environ.get("DISCORD_WEBHOOK_INTERNSHIPS")
    or os.environ.get("DISCORD_WEBHOOK_URL"),
    "new_grad": os.environ.get("DISCORD_WEBHOOK_NEWGRAD")
    or os.environ.get("DISCORD_WEBHOOK_URL"),
}

BOT_NAME = os.environ.get("BOT_NAME", "ACM Job Radar 📡")
PING_ROLE_ID = os.environ.get("PING_ROLE_ID", "").strip()  # optional role to @mention

MAX_POSTS_PER_RUN = int(os.environ.get("MAX_POSTS_PER_RUN", "15"))
BOOTSTRAP_POST_COUNT = int(os.environ.get("BOOTSTRAP_POST_COUNT", "5"))

# Backfill: "" = off (normal run), "30" = last 30 days, "all" = everything open
BACKFILL_DAYS = os.environ.get("BACKFILL_DAYS", "").strip().lower()

# Comma-separated category names to skip, e.g. "Quant,Product"
CATEGORY_BLOCKLIST = {
    c.strip().lower()
    for c in os.environ.get("CATEGORY_BLOCKLIST", "").split(",")
    if c.strip()
}

# Optional: lets repo auto-discovery use authenticated GitHub API calls
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
DRY_RUN_SAVE = os.environ.get("DRY_RUN_SAVE", "").lower() in ("1", "true", "yes")

# Fallback if GitHub API discovery is unavailable (update yearly if you want,
# but discovery normally handles new cycles automatically).
FALLBACK_INTERNSHIP_REPO = "Summer2027-Internships"

NEW_GRAD_REPO = "New-Grad-Positions"
LISTINGS_PATH = ".github/scripts/listings.json"

EMBEDS_PER_MESSAGE = 5        # Discord allows 10, but 5 keeps us under char limits
SLEEP_BETWEEN_MESSAGES = 2.0  # seconds; webhooks sustain ~30 req/min, this stays under

KIND_META = {
    "internship": {"label": "internship", "color": 0x57F287},   # green
    "new_grad": {"label": "new-grad role", "color": 0x5865F2},  # blurple
}

CATEGORY_DISPLAY = {
    "software engineering": "Software",
    "data science, ai & machine learning": "AI/ML/Data",
    "hardware engineering": "Hardware",
    "product management": "Product",
}

# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def http_json(url: str, headers: dict | None = None, timeout: int = 30):
    req = urllib.request.Request(url, headers={"User-Agent": "acm-job-radar", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
    except Exception as e:  # rate limit, network, schema change — fall back
        print(f"[discover] GitHub API unavailable ({e}); using fallback repo")
    return FALLBACK_INTERNSHIP_REPO


def raw_listings_url(repo: str) -> str:
    return f"https://raw.githubusercontent.com/SimplifyJobs/{repo}/dev/{LISTINGS_PATH}"


# ---------------------------------------------------------------------------
# Fetch + filter listings
# ---------------------------------------------------------------------------


def clean_text(s: str) -> str:
    return (s or "").replace("</br>", ", ").replace("<br>", ", ").strip()


def display_category(raw: str | None) -> str | None:
    if not raw:
        return None
    return CATEGORY_DISPLAY.get(raw.strip().lower(), raw.strip())


def fetch_source(kind: str, repo: str) -> list[dict]:
    url = raw_listings_url(repo)
    data = http_json(url, timeout=60)
    jobs = []
    for j in data:
        if not (j.get("active") and j.get("is_visible", True)):
            continue
        if not j.get("id") or not j.get("url"):
            continue
        cat = display_category(j.get("category"))
        if cat and cat.lower() in CATEGORY_BLOCKLIST:
            continue
        jobs.append(
            {
                "id": j["id"],
                "kind": kind,
                "company": clean_text(j.get("company_name", "Unknown")),
                "title": clean_text(j.get("title", "Unknown role")),
                "url": j["url"],
                "locations": [clean_text(x) for x in (j.get("locations") or []) if clean_text(x)],
                "terms": j.get("terms") or [],
                "category": cat,
                "sponsorship": j.get("sponsorship"),
                "date_posted": j.get("date_posted") or j.get("date_updated") or 0,
                "source_name": f"SimplifyJobs/{repo}",
            }
        )
    print(f"[fetch] {repo}: {len(data)} listings, {len(jobs)} active after filters")
    return jobs


# ---------------------------------------------------------------------------
# Seen-state
# ---------------------------------------------------------------------------


def load_seen() -> dict:
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict) -> None:
    if DRY_RUN and not DRY_RUN_SAVE:
        print(f"[dry-run] skipped saving state ({len(seen)} ids)")
        return
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen, f, indent=0, sort_keys=True)
        f.write("\n")
    print(f"[state] saved {len(seen)} seen ids -> {SEEN_FILE}")


def seen_entry(job: dict) -> dict:
    return {
        "company": job["company"],
        "title": job["title"],
        "first_seen": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# ---------------------------------------------------------------------------
# Discord
# ---------------------------------------------------------------------------


def make_embed(job: dict) -> dict:
    meta = KIND_META[job["kind"]]

    locs = job["locations"]
    loc_str = ", ".join(locs[:6]) + (f" (+{len(locs) - 6} more)" if len(locs) > 6 else "")

    fields = [{"name": "📍 Location", "value": (loc_str or "—")[:1024], "inline": True}]
    if job["terms"]:
        fields.append({"name": "🗓️ Term", "value": ", ".join(job["terms"])[:1024], "inline": True})
    if job["category"]:
        fields.append({"name": "🏷️ Category", "value": job["category"][:1024], "inline": True})
    spons = (job.get("sponsorship") or "").strip()
    if spons and spons.lower() != "other":
        fields.append({"name": "🛂 Sponsorship", "value": spons[:1024], "inline": True})

    embed = {
        "title": f"{job['company']} — {job['title']}"[:256],
        "url": job["url"],
        "color": meta["color"],
        "fields": fields,
        "footer": {"text": f"via {job['source_name']}"},
    }
    if job["date_posted"]:
        embed["timestamp"] = datetime.fromtimestamp(
            job["date_posted"], tz=timezone.utc
        ).isoformat()
    return embed


def post_to_webhook(webhook: str, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook + ("&" if "?" in webhook else "?") + "wait=true",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "acm-job-radar"},
        method="POST",
    )
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=30):
                return
        except urllib.error.HTTPError as e:
            if e.code == 429:  # rate limited — Discord tells us how long to wait
                try:
                    retry_after = float(json.loads(e.read().decode()).get("retry_after", 3))
                except Exception:
                    retry_after = 3.0
                print(f"[discord] rate limited, retrying in {retry_after:.1f}s")
                time.sleep(retry_after + 0.5)
                continue
            raise
    raise RuntimeError("Discord kept rate-limiting after 6 attempts")


def send_jobs(kind: str, jobs: list[dict], backfill: bool = False) -> None:
    """Post a batch of jobs of one kind to its channel, oldest first."""
    if not jobs:
        return
    meta = KIND_META[kind]
    webhook = WEBHOOKS[kind]
    jobs = sorted(jobs, key=lambda j: j["date_posted"])

    plural = "s" if len(jobs) != 1 else ""
    if backfill:
        summary = f"📦 **Backfill: {len(jobs)} currently-open {meta['label']}{plural}** (oldest → newest)"
    else:
        summary = f"🚀 **{len(jobs)} new {meta['label']}{plural} just dropped!**"
    if PING_ROLE_ID:
        summary = f"<@&{PING_ROLE_ID}> {summary}"

    total_chunks = math.ceil(len(jobs) / EMBEDS_PER_MESSAGE)
    for i in range(0, len(jobs), EMBEDS_PER_MESSAGE):
        chunk = jobs[i : i + EMBEDS_PER_MESSAGE]
        payload = {
            "username": BOT_NAME,
            "embeds": [make_embed(j) for j in chunk],
            "allowed_mentions": {"parse": [], "roles": [PING_ROLE_ID] if PING_ROLE_ID else []},
        }
        if i == 0:
            payload["content"] = summary

        if DRY_RUN:
            print(f"[dry-run] would POST to {kind} webhook:")
            for j in chunk:
                print(f"    • {j['company']} — {j['title']}")
        else:
            post_to_webhook(webhook, payload)
            chunk_no = i // EMBEDS_PER_MESSAGE + 1
            print(f"[discord] {kind}: message {chunk_no}/{total_chunks} sent ({len(chunk)} jobs)")
            time.sleep(SLEEP_BETWEEN_MESSAGES)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    # Make sure we have somewhere to post before doing any work
    missing = [k for k, v in WEBHOOKS.items() if not v]
    if missing and not DRY_RUN:
        print(
            "ERROR: no webhook configured for: "
            + ", ".join(missing)
            + "\nSet DISCORD_WEBHOOK_INTERNSHIPS / DISCORD_WEBHOOK_NEWGRAD "
            "(or a single DISCORD_WEBHOOK_URL for both) as GitHub Actions secrets."
        )
        return 1

    # Validate backfill input before doing any work
    backfill_cutoff = None
    if BACKFILL_DAYS:
        if BACKFILL_DAYS == "all":
            backfill_cutoff = 0.0
        else:
            try:
                backfill_cutoff = time.time() - int(BACKFILL_DAYS) * 86400
            except ValueError:
                print(f"ERROR: backfill must be a number of days or 'all', got '{BACKFILL_DAYS}'")
                return 1

    internship_repo = discover_internship_repo()
    all_jobs = fetch_source("internship", internship_repo) + fetch_source(
        "new_grad", NEW_GRAD_REPO
    )

    bootstrap = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    new_jobs = [j for j in all_jobs if j["id"] not in seen]

    if backfill_cutoff is not None:
        # Backfill: post every currently-open listing in the window, even ones
        # already marked seen. One-time catch-up; normal runs resume after.
        to_post = [j for j in all_jobs if j["date_posted"] >= backfill_cutoff]
        for j in all_jobs:
            seen.setdefault(j["id"], seen_entry(j))
        est_min = math.ceil(len(to_post) / EMBEDS_PER_MESSAGE) * SLEEP_BETWEEN_MESSAGES / 60
        print(
            f"[backfill] window={BACKFILL_DAYS} -> posting {len(to_post)} of "
            f"{len(all_jobs)} open listings (~{est_min:.0f} min)"
        )
    elif bootstrap:
        # First ever run: don't flood the channel with 4,000 old posts.
        # Mark everything as seen, then post just the freshest few per kind
        # so the channel has something to show.
        print(f"[bootstrap] first run — seeding {len(all_jobs)} listings as seen")
        for j in all_jobs:
            seen[j["id"]] = seen_entry(j)
        to_post = []
        for kind in KIND_META:
            freshest = sorted(
                (j for j in new_jobs if j["kind"] == kind),
                key=lambda j: j["date_posted"],
                reverse=True,
            )[:BOOTSTRAP_POST_COUNT]
            to_post.extend(freshest)
    else:
        # Steady state: post oldest-first, capped per run. Anything past the
        # cap stays unseen and gets posted on the next run (no jobs lost).
        new_jobs.sort(key=lambda j: j["date_posted"])
        to_post = new_jobs[:MAX_POSTS_PER_RUN]
        for j in to_post:
            seen[j["id"]] = seen_entry(j)
        if len(new_jobs) > len(to_post):
            print(f"[queue] {len(new_jobs) - len(to_post)} more queued for next run")
        print(f"[run] {len(new_jobs)} new, posting {len(to_post)}")

    for kind in KIND_META:
        send_jobs(
            kind,
            [j for j in to_post if j["kind"] == kind],
            backfill=backfill_cutoff is not None,
        )

    save_seen(seen)
    print("[done]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
