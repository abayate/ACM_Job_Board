"""Verify degree eligibility from original employer postings; stdlib only.

Unknown and failed lookups remain distinct from undergraduate eligibility.
Shared HTML/JSON-LD parsing and public ATS adapters use the same classifier.
"""

import html
import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlparse, urlunsplit

VERIFIED_TTL = 7 * 86400
RETRY_TTL = 3600
UNVERIFIED_TTL = 86400
_PHD = re.compile(r"\bph\.?\s?d\.?\b|\bdoctoral\b", re.I)
_BACH = re.compile(r"\b(?:bachelor(?:['’]?s)?|undergrad(?:uate)?|b\.?\s?[as]\.?|b\.?sc\.?)\b", re.I)
_GRAD = re.compile(r"\b(?:ph\.?\s?d\.?|doctoral|master['’]?s?|msc)\b", re.I)
_PREFERRED = re.compile(r"\b(?:preferred|preference|desired|desirable|bonus|nice.to.have)\b", re.I)
_OPTIONAL = re.compile(r"\b(?:not required|not necessary|optional|must not|do not need|no\s+[^.;\n]{0,50}\s+required)\b", re.I)
_REQUIRED = re.compile(r"\b(?:required|must|pursuing|enrolled|obtaining|working toward)\b", re.I)
_EXPERIENCE_ALTERNATIVE = re.compile(r"\b(?:or|and/or)\s+(?:equivalent\s+)?(?:practical\s+|professional\s+|relevant\s+|work\s+)?experience\b", re.I)
_REQUIRED_HEADING = re.compile(r"^(?:(?:minimum|basic|required|essential)\s+)?(?:qualifications|requirements|education(?: and experience)?|what you bring|what you(?:'|’)ll need)\s*:?$", re.I)
_PREFERRED_HEADING = re.compile(r"^(?:(?:preferred|desired|additional|bonus)\s+(?:qualifications|requirements|skills)|nice.to.have)\s*:?$", re.I)


def canonical_url(url):
    """Normalize public HTTPS job URLs without discarding meaningful queries."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            pass
        match = re.fullmatch(r"/(?:jobs|profile/job_details)/(\d+)/?", parsed.path)
        if host in {"metacareers.com", "www.metacareers.com"} and match:
            return f"https://www.metacareers.com/profile/job_details/{match[1]}/"
        return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))
    except ValueError:
        pass
    return None


def _check_public_destination(url):
    canonical = canonical_url(url)
    if not canonical:
        raise ValueError("not a public HTTPS destination")
    addresses = socket.getaddrinfo(urlparse(canonical).hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError("destination does not resolve to public addresses")


class _PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_public_destination(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_public_text(url, timeout=15, retries=2):
    """Bounded public-page reads; validate redirects as well as initial URLs."""
    _check_public_destination(url)
    opener = urllib.request.build_opener(_PublicRedirect())
    request = urllib.request.Request(url, headers={"User-Agent": "acm-job-radar", "Accept": "application/json,text/html"})
    for attempt in range(retries):
        try:
            with opener.open(request, timeout=timeout) as response:
                data = response.read(4 * 1024 * 1024 + 1)
                if len(data) > 4 * 1024 * 1024:
                    raise ValueError("employer response exceeds size limit")
                return data.decode("utf-8", "replace")
        except (OSError, urllib.error.URLError) as error:
            if isinstance(error, urllib.error.HTTPError) and error.code != 429 and error.code < 500:
                raise
            if attempt + 1 == retries:
                raise
            time.sleep(1)


def source_request(url):
    """Select a public ATS endpoint, or the original HTML page for any employer."""
    parsed = urlparse(url)
    host = parsed.hostname
    parts = [quote(unquote(p), safe="") for p in parsed.path.split("/") if p]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"} and len(parts) >= 3 and parts[1] == "jobs":
        return "greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{parts[0]}/jobs/{parts[2]}", None
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(parts) >= 2:
        api = "api.eu.lever.co" if host == "jobs.eu.lever.co" else "api.lever.co"
        return "lever", f"https://{api}/v0/postings/{parts[0]}/{parts[1]}", None
    if host == "jobs.ashbyhq.com" and len(parts) >= 2:
        return "ashby", f"https://api.ashbyhq.com/posting-api/job-board/{parts[0]}", parts[1]
    if host == "jobs.smartrecruiters.com" and len(parts) >= 2:
        job_id = re.match(r"\d+", parts[1])
        if job_id:
            return "smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{job_id[0]}", None
    if host and host.endswith(".myworkdayjobs.com") and "job" in parts:
        index = parts.index("job")
        if index >= 1 and len(parts) > index + 1:
            tenant = host.split(".")[0]
            return "workday", f"https://{host}/wday/cxs/{tenant}/{parts[index-1]}/job/" + "/".join(parts[index+1:]), None
    return "html", url, None


class _JobPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.scripts = []
        self.current = None
        self.h1 = []
        self.in_h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("property") == "og:title":
            self.title = attrs.get("content", "")
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.current = []
        if tag == "h1":
            self.in_h1 = True

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)
        if self.in_h1:
            self.h1.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append("".join(self.current))
            self.current = None
        if tag == "h1":
            self.in_h1 = False


class _Text(HTMLParser):
    """Preserve section and list boundaries while ignoring scripts/navigation."""
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "nav", "footer"}:
            self.hidden += 1
        if not self.hidden and tag in {"p", "li", "br", "div", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "nav", "footer"}:
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in {"p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _clauses(markup, *, qualification_field=False):
    if not isinstance(markup, str):
        return []
    parser = _Text()
    parser.feed(html.unescape(markup))
    text = "".join(parser.parts)
    result = []
    section = None
    for line in re.split(r"\n|\u00a0", text):
        line = line.strip()
        if not line:
            continue
        if _PREFERRED_HEADING.fullmatch(line):
            section = "preferred"
            continue
        if _REQUIRED_HEADING.fullmatch(line):
            section = "required"
            continue
        # Explicit non-qualification headings end a requirement section.
        if re.fullmatch(r"(?:about .+|responsibilities|benefits|what you(?:'|’)ll do|compensation|equal employment opportunity)\s*:?", line, re.I):
            section = None
            continue
        for clause in re.split(r";|(?<=[.!?])\s+(?=[A-Z])", line):
            if _PREFERRED.search(clause) or _OPTIONAL.search(clause) or section == "preferred":
                continue
            if section == "required" or _REQUIRED.search(clause) or qualification_field:
                result.append(clause.strip())
    return result


def _records(value):
    if isinstance(value, list):
        for item in value:
            yield from _records(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
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
    title = record.get("title") or parser.title or "".join(parser.h1)
    if not isinstance(title, str):
        title = ""
    if len(records) > 1:
        return "", None, "multiple job records; cannot identify the requested posting"
    # Structured qualifications and HTML minimum/preferred sections share rules.
    # Explicit requirement wording is still needed outside a recognized section.
    required = _clauses(record.get("qualifications", ""))
    required += _clauses(record.get("educationRequirements", ""), qualification_field=True)
    required += _clauses(record.get("description", "") or markup)
    return _classify(title, required)


def _classify(title, required):
    bachelor_clauses = [c for c in required if _BACH.search(c)]
    graduate_clauses = [c for c in required if _GRAD.search(c) and not _BACH.search(c)
                       and not _EXPERIENCE_ALTERNATIVE.search(c)]
    if graduate_clauses:
        return title, "grad", graduate_clauses[0].strip()[:600]
    if bachelor_clauses:
        return title, "bach", bachelor_clauses[0].strip()[:600]
    if any(_GRAD.search(c) and _EXPERIENCE_ALTERNATIVE.search(c) for c in required):
        return title, None, "graduate degree has an experience alternative"
    level = title_degree(title)
    if level:
        return title, level, "employer title explicitly specifies " + ("undergraduate eligibility" if level == "bach" else "PhD/doctoral")
    return title, None, "no explicit degree requirement verified"


def title_degree(title):
    """Only explicit eligibility markers count, including on fallback feed titles."""
    if _PREFERRED.search(title) or _OPTIONAL.search(title):
        return None
    if _BACH.search(title):
        return "bach"
    if _PHD.search(title) and not re.search(r"\bpre[-\s]?doctoral\b", title, re.I):
        return "grad"
    return None


def parse_response(kind, raw, token=None):
    if kind == "html":
        return parse_page(raw)
    data = json.loads(raw)
    if kind == "greenhouse":
        title, description = data.get("title", ""), data.get("content", "")
    elif kind == "lever":
        title = data.get("text", "")
        sections = "".join("<h2>" + html.escape(item.get("text", "")) + "</h2>" + item.get("content", "")
                           for item in data.get("lists", []))
        description = (data.get("description", "") or data.get("descriptionPlain", "")) + sections
    elif kind == "ashby":
        hits = [job for job in data.get("jobs", []) if job.get("id") == token]
        data = hits[0] if len(hits) == 1 else {}
        title = data.get("title", "")
        description = data.get("descriptionHtml", "") or data.get("descriptionPlain", "")
    elif kind == "smartrecruiters":
        title = data.get("name", "")
        sections = data.get("jobAd", {}).get("sections", {})
        description = "".join("<h2>" + html.escape(section.get("title", "")) + "</h2>" + section.get("text", "")
                              for section in sections.values() if isinstance(section, dict))
    elif kind == "workday":
        data = data.get("jobPostingInfo", {})
        title, description = data.get("title", ""), data.get("jobDescription", "")
    else:
        raise ValueError("unsupported response format")
    return _classify(title, _clauses(description))


def lookup(url, cache, fetch_text=None, *, now=None, allow_fetch=True):
    canonical = canonical_url(url)
    if not canonical:
        return None
    now = time.time() if now is None else now
    key = "employer:v2:" + canonical
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("expires_at", 0) > now:
        return {**cached, "fetched": False}
    result = {"degree": None, "status": "deferred", "title": "", "url": canonical,
              "checked_at": now, "evidence": "lookup budget exhausted"}
    if not allow_fetch:
        return {**result, "fetched": False}
    try:
        kind, endpoint, token = source_request(canonical)
        fetch_text = fetch_text or fetch_public_text
        title, degree, evidence = parse_response(kind, fetch_text(endpoint, timeout=15, retries=2), token)
        result.update(title=title, degree=degree, evidence=evidence,
                      status="verified" if degree else "unverified", provider=kind)
        ttl = VERIFIED_TTL if degree else UNVERIFIED_TTL
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        result.update(status="fetch_failed", evidence="employer page could not be read")
        ttl = RETRY_TTL
    result["expires_at"] = now + ttl
    cache[key] = result
    return {**result, "fetched": True}
