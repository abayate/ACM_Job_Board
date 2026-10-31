"""Verify Meta degree eligibility from public employer evidence; stdlib only.

Unknown and failed lookups remain distinct from undergraduate eligibility.
Only an allowlisted employer and numeric job-detail paths are fetched.
"""

import html
import json
import re
import time
from html.parser import HTMLParser
from urllib.parse import urlparse

VERIFIED_TTL = 7 * 86400
RETRY_TTL = 3600
UNVERIFIED_TTL = 86400
_PHD = re.compile(r"\bph\.?\s?d\.?\b|\bdoctoral\b", re.I)
_BACH = re.compile(r"\b(?:bachelor(?:['’]?s)?|undergrad(?:uate)?|b\.?\s?[as]\.?|b\.?sc\.?)\b", re.I)
_GRAD = re.compile(r"\b(?:ph\.?\s?d\.?|doctoral|master['’]?s?|msc)\b", re.I)
_PREFERRED = re.compile(r"\b(?:preferred|preference|bonus|nice.to.have)\b", re.I)
_OPTIONAL = re.compile(r"\b(?:not required|not necessary|optional|must not|do not need|no\s+[^.;\n]{0,50}\s+required)\b", re.I)
_REQUIRED = re.compile(r"\b(?:required|must|pursuing|enrolled|obtaining|working toward)\b", re.I)


def canonical_url(url):
    """Accept both Meta URL forms while rejecting other hosts and routes."""
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc.lower() not in {"metacareers.com", "www.metacareers.com"}:
            return None
        match = re.fullmatch(r"/(?:jobs|profile/job_details)/(\d+)/?", parsed.path)
        if match:
            return f"https://www.metacareers.com/profile/job_details/{match[1]}/"
    except ValueError:
        pass
    return None


class _JobPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.scripts = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content", "")
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.current = []

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None


def _records(value):
    if isinstance(value, list):
        for item in value:
            yield from _records(item)
    elif isinstance(value, dict):
        if value.get("@type") == "JobPosting":
            yield value
        yield from _records(value.get("@graph", []))


def parse_page(markup):
    parser = _JobPage()
    parser.feed(markup)
    records = []
    for script in parser.scripts:
        try:
            records.extend(_records(json.loads(script)))
        except (ValueError, TypeError):
            continue
    record = records[0] if len(records) == 1 else {}
    title = record.get("title") or parser.title
    if not isinstance(title, str):
        title = ""
    qualifications = record.get("qualifications", "")
    if not isinstance(qualifications, str):
        qualifications = ""
    # Preserve bullet/paragraph boundaries. Meta combines minimum and preferred
    # qualifications in JSON-LD, so only explicit requirement clauses count.
    qualifications = html.unescape(re.sub(r"<[^>]+>", "\n", qualifications))
    clauses = re.split(r"\n|\u00a0|;|(?<=[.!?])\s+(?=[A-Z])", qualifications)
    required = [c for c in clauses if _REQUIRED.search(c) and not _PREFERRED.search(c) and not _OPTIONAL.search(c)]
    bachelor_clauses = [c for c in required if _BACH.search(c)]
    graduate_clauses = [c for c in required if _GRAD.search(c)]
    if bachelor_clauses:
        return title, "bach", bachelor_clauses[0].strip()[:600]
    if graduate_clauses:
        return title, "grad", graduate_clauses[0].strip()[:600]
    if not _PREFERRED.search(title) and not _OPTIONAL.search(title):
        if _BACH.search(title):
            return title, "bach", "employer title explicitly includes undergraduate degrees"
        if _PHD.search(title) and not re.search(r"\bpre[-\s]?doctoral\b", title, re.I):
            return title, "grad", "employer title explicitly specifies PhD/doctoral"
    return title, None, "no explicit degree requirement verified"


def lookup(url, cache, fetch_text, *, now=None, allow_fetch=True):
    canonical = canonical_url(url)
    if not canonical:
        return None
    now = time.time() if now is None else now
    key = "employer:v1:" + canonical
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("expires_at", 0) > now:
        return {**cached, "fetched": False}
    result = {"degree": None, "status": "deferred", "title": "", "url": canonical,
              "checked_at": now, "evidence": "lookup budget exhausted"}
    if not allow_fetch:
        return {**result, "fetched": False}
    try:
        title, degree, evidence = parse_page(fetch_text(canonical, timeout=15, retries=2))
        result.update(title=title, degree=degree, evidence=evidence,
                      status="verified" if degree else "unverified")
        ttl = VERIFIED_TTL if degree else UNVERIFIED_TTL
    except (OSError, ValueError, TypeError):
        result.update(status="fetch_failed", evidence="employer page could not be read")
        ttl = RETRY_TTL
    result["expires_at"] = now + ttl
    cache[key] = result
    return {**result, "fetched": True}
