#!/usr/bin/env python3
"""
Scholar Report
Given a list of researchers and a list of keywords, search free scholarly APIs
(OpenAlex, Semantic Scholar, arXiv), collect each author's most-cited
keyword-relevant papers plus a total hit count, verify titles/journals against
the live landing page, and emit a compilable LaTeX report (letterpaper, 1in
margins) with a per-author bulleted list and a combined hyperlinked bibliography.

Sources:
  - OpenAlex          free, no key, citation counts, author IDs, total counts
  - Semantic Scholar  free, no key, citation counts, author papers endpoint
  - arXiv             free, preprints (no citations; cross-matched to the above)

Example:
  python3 scholar_report.py authors.txt keywords.txt -n 5 -o report.tex \\
      --mailto you@example.org --cache .scholar_cache
"""

import os
import re
import sys
import json
import time
import html
import hashlib
import argparse
import difflib
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from dataclasses import dataclass, field, asdict

try:
    import requests
except ImportError:
    print("Error: requests not installed. Install with: pip install requests")
    sys.exit(1)


# ───────────────────────────────────────────────────── Constants ───
OPENALEX_BASE = "https://api.openalex.org"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
ARXIV_BASE = "http://export.arxiv.org/api/query"
DOI_RESOLVER = "https://doi.org/"

USER_AGENT = "scholar_report/1.0 (mailto:{mailto})"
TITLE_MATCH_THRESHOLD = 0.85   # difflib ratio for title verification
VENUE_MATCH_THRESHOLD = 0.60   # journals are noisier; looser threshold
MAX_BACKOFF = 120.0            # cap any single retry wait (seconds)
RETRY_BASE = 4.0              # first retry wait on HTTP 429/5xx (seconds)
MAX_RETRIES = 5              # number of attempts before giving up

# Module-level config set in main(); keeps helper signatures small.
CONFIG = {
    "cache": None,       # Path or None
    "mailto": "anon@example.org",
    "sleep": 1.0,
    "verify": True,
    "max_candidates": 20,
    "year_min": None,    # int or None
    "year_max": None,    # int or None
    "s2_api_key": None,  # Semantic Scholar API key (optional, from env)
}

# Keyword-location ranking: title hits outrank abstract hits.
LOCATION_PRIORITY = {"title": 2, "abstract": 1}


# ──────────────────────────────────────────────────── Data model ───
@dataclass
class Paper:
    """A single publication merged across sources."""
    title: str = ""
    authors: list = field(default_factory=list)
    author_orcids: list = field(default_factory=list)  # bare ORCIDs aligned to authors
    venue: str = ""
    year: object = None            # int or None
    doi: str = None
    url: str = ""
    citations: int = 0
    sources: set = field(default_factory=set)
    arxiv_id: str = None
    abstract: str = ""                  # used for keyword matching; not dumped to JSON
    matches: dict = field(default_factory=dict)   # keyword -> "title" | "abstract"
    match_location: str = None          # best location overall: "title" | "abstract"
    verify_status: str = "unverified"   # verified | mismatch | unverified | no-landing
    key: str = ""                       # citation key, assigned late

    def to_dict(self):
        d = asdict(self)
        d["sources"] = sorted(self.sources)
        d.pop("abstract", None)         # keep the (potentially long) abstract out of JSON
        d.pop("author_orcids", None)    # internal disambiguation aid; omit from JSON
        return d


# ───────────────────────────────────────────────── HTTP helpers ───
def _cache_path(kind, url):
    """Deterministic cache filename for a request."""
    cache = CONFIG["cache"]
    if cache is None:
        return None
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    return Path(cache) / f"{kind}_{h}.{'json' if kind == 'json' else 'txt'}"


def _read_cache(path):
    if path is None or not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return None


def _write_cache(path, text):
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except Exception as e:
        print(f"  Warning: could not write cache {path}: {e}")


def _next_wait(backoff, retry_after_header):
    """Compute a bounded retry wait (seconds).

    Honors a numeric Retry-After header only when sane (<= MAX_BACKOFF); absurd
    values (e.g. the observed '32759') are ignored. Never exceeds MAX_BACKOFF.
    """
    wait = backoff
    if retry_after_header and str(retry_after_header).strip().isdigit():
        ra = int(str(retry_after_header).strip())
        if ra <= MAX_BACKOFF:
            wait = max(wait, ra)
    return min(MAX_BACKOFF, wait)


# Per-host timestamp of the last issued request, for proactive throttling.
_LAST_REQUEST = {}


def _host_of(url):
    """Return the hostname of a URL (or '' if unparseable)."""
    return urllib.parse.urlsplit(url).hostname or ""


def _throttle(host):
    """Block until at least CONFIG['sleep'] has elapsed since the last call to host."""
    spacing = CONFIG["sleep"] or 0.0
    if spacing <= 0:
        return
    now = time.monotonic()
    last = _LAST_REQUEST.get(host)
    if last is not None:
        wait = spacing - (now - last)
        if wait > 0:
            time.sleep(wait)
    _LAST_REQUEST[host] = time.monotonic()


def _request(url, headers=None, retries=MAX_RETRIES):
    """GET with per-host throttling and retry/backoff on 429/5xx.

    Returns response text or None.
    """
    headers = headers or {}
    headers.setdefault("User-Agent", USER_AGENT.format(mailto=CONFIG["mailto"]))
    if "api.semanticscholar.org" in url and CONFIG.get("s2_api_key"):
        headers.setdefault("x-api-key", CONFIG["s2_api_key"])
    host = _host_of(url)
    backoff = min(MAX_BACKOFF, RETRY_BASE)   # start retry wait at RETRY_BASE
    for attempt in range(retries):
        _throttle(host)                      # proactively space calls per host
        try:
            resp = requests.get(url, headers=headers, timeout=30)
        except requests.RequestException as e:
            wait = min(MAX_BACKOFF, backoff)
            print(f"  Warning: request error from {host} ({e}); waiting {wait:.1f}s "
                  f"(retry {attempt + 1}/{retries})")
            time.sleep(wait)
            backoff = min(MAX_BACKOFF, backoff * 2)
            continue
        if resp.status_code == 200:
            return resp.text
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = _next_wait(backoff, resp.headers.get("Retry-After"))
            print(f"  Warning: HTTP {resp.status_code} from {host}; waiting {wait:.1f}s "
                  f"(retry {attempt + 1}/{retries})")
            time.sleep(wait)
            backoff = min(MAX_BACKOFF, backoff * 2)   # double after each 429/5xx
            continue
        # Other errors (404, 403, ...) are not retried.
        print(f"  Warning: HTTP {resp.status_code} from {host} for {url}")
        return None
    print(f"  Warning: giving up on {host} ({url})")
    return None


def http_get_json(url, headers=None):
    """Cached GET returning parsed JSON, or None."""
    path = _cache_path("json", url)
    cached = _read_cache(path)
    if cached is not None:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass
    text = _request(url, headers=headers)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"  Warning: non-JSON response from {url}")
        return None
    _write_cache(path, text)
    return data


def http_get_text(url, headers=None):
    """Cached GET returning raw text, or None."""
    path = _cache_path("text", url)
    cached = _read_cache(path)
    if cached is not None:
        return cached
    text = _request(url, headers=headers)
    if text is not None:
        _write_cache(path, text)
    return text


# ───────────────────────────────────────────── Text utilities ───
_PUNCT_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def normalize_title(title):
    """Lowercase, strip punctuation, collapse whitespace for matching/dedup."""
    if not title:
        return ""
    t = html.unescape(title).lower()
    t = _PUNCT_RE.sub(" ", t)
    t = _WS_RE.sub(" ", t).strip()
    return t


def normalize_doi(doi):
    """Strip URL prefixes and lowercase a DOI."""
    if not doi:
        return None
    d = doi.strip().lower()
    for pre in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pre):
            d = d[len(pre):]
    return d or None


def title_ratio(a, b):
    return difflib.SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def matches_keywords(text, keywords):
    """True if any keyword (case-insensitive substring) appears in text."""
    if not text:
        return False
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def classify_matches(paper, keywords):
    """Record where each keyword is found (title or abstract only).

    Sets paper.matches (keyword -> "title"|"abstract") and paper.match_location
    (best location overall, title preferred). Returns True if any keyword matched.
    """
    title_low = (paper.title or "").lower()
    abstract_low = (paper.abstract or "").lower()
    matches = {}
    for k in keywords:
        kl = k.lower()
        if kl in title_low:
            matches[k] = "title"
        elif kl in abstract_low:
            matches[k] = "abstract"
    paper.matches = matches
    if any(loc == "title" for loc in matches.values()):
        paper.match_location = "title"
    elif matches:
        paper.match_location = "abstract"
    else:
        paper.match_location = None
    return bool(matches)


def within_year_range(year):
    """True if year falls within [year_min, year_max]. Unknown year fails a set bound."""
    ymin, ymax = CONFIG["year_min"], CONFIG["year_max"]
    if ymin is None and ymax is None:
        return True
    if year is None:
        return False
    if ymin is not None and year < ymin:
        return False
    if ymax is not None and year > ymax:
        return False
    return True


def reconstruct_abstract(inv_index):
    """Rebuild abstract text from an OpenAlex abstract_inverted_index."""
    if not inv_index:
        return ""
    positions = []
    for word, idxs in inv_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


# ────────────────────────────────────────── Author resolution ───
def _orcid_id(orcid_url):
    """Strip an ORCID URL to its bare id (or '' if none)."""
    if not orcid_url:
        return ""
    return orcid_url.rstrip("/").rsplit("/", 1)[-1]


def name_matches(display, last, first):
    """True if `display` ('Given ... Family') matches lastname + first initial."""
    parts = (display or "").split()
    if not parts:
        return False
    cand_family = parts[-1].casefold()
    cand_given = parts[0]
    if cand_family != last.casefold():
        return False
    if not first:
        return True
    return bool(cand_given) and cand_given[0].casefold() == first[0].casefold()


def author_on_paper(paper, last, first, resolved_orcid=""):
    """Authorship harness: is the queried author really on this paper?

    Requires an author matching lastname + first initial. When an ORCID is known
    for both the queried author and the matching authorship, the ORCID decides:
    equal -> confirmed keep; different -> a same-name different person (skip that
    authorship). A name match with no conflicting ORCID is a (weaker) keep.
    """
    orcids = paper.author_orcids or [""] * len(paper.authors)
    weak_keep = False
    for name, oid in zip(paper.authors, orcids):
        if not name_matches(name, last, first):
            continue
        if resolved_orcid and oid:
            if oid == resolved_orcid:
                return True            # ORCID-confirmed same person
            continue                   # same name+initial but different ORCID
        weak_keep = True               # name+initial match, no ORCID conflict
    return weak_keep


def _author_institution(cand):
    """Best-effort last-known institution name from an OpenAlex author record."""
    insts = cand.get("last_known_institutions") or []
    if insts and insts[0].get("display_name"):
        return insts[0]["display_name"]
    inst = cand.get("last_known_institution") or {}
    return inst.get("display_name", "") or ""


def _author_concepts(cand, limit=4):
    return ", ".join(c.get("display_name", "")
                     for c in (cand.get("x_concepts") or [])[:limit])


def prompt_author_choice(name, candidates):
    """Show a numbered menu and return the chosen candidate index (or None to skip)."""
    print(f"\n  Multiple authors match '{name}'. Choose one:")
    for i, c in enumerate(candidates, 1):
        orcid = _orcid_id(c.get("orcid")) or "no ORCID"
        inst = _author_institution(c) or "unknown institution"
        print(f"    [{i}] {c.get('display_name')} — {orcid} — "
              f"{c.get('works_count', 0)} works — {inst}")
        concepts = _author_concepts(c)
        if concepts:
            print(f"        concepts: {concepts}")
    while True:
        try:
            raw = input(f"  Select author [1-{len(candidates)}], 0 to skip: ").strip()
        except EOFError:
            return None
        if raw.isdigit():
            n = int(raw)
            if n == 0:
                return None
            if 1 <= n <= len(candidates):
                return n - 1
        print("  Invalid choice; try again.")


def resolve_author(name, keywords):
    """Resolve a name to one OpenAlex author (id, display_name, orcid).

    Requires a lastname + first-initial match. With several matches, prompts the
    user (interactive) or auto-picks the top-ranked candidate (non-interactive).
    """
    last, first = split_name(name)
    query = f"{first} {last}".strip()
    url = (f"{OPENALEX_BASE}/authors?search={urllib.parse.quote(query)}"
           f"&per-page=25&mailto={urllib.parse.quote(CONFIG['mailto'])}")
    data = http_get_json(url)
    if not data or not data.get("results"):
        print(f"  Warning: no OpenAlex author found for '{name}'")
        return None, None, None

    matches = [c for c in data["results"]
               if name_matches(c.get("display_name", ""), last, first)]
    if not matches:
        print(f"  Warning: no name match (lastname+initial) for '{name}'")
        return None, None, None

    def score(cand):
        disp_parts = (cand.get("display_name") or "").split()
        given = disp_parts[0] if disp_parts else ""
        full_first = 1 if (first and given.casefold() == first.casefold()) else 0
        has_orcid = 1 if _orcid_id(cand.get("orcid")) else 0
        concepts = " ".join(c.get("display_name", "")
                            for c in cand.get("x_concepts", []))
        kw = 1 if matches_keywords(concepts, keywords) else 0
        # Prefer exact first name, then an ORCID, then topical fit, then prolificacy.
        return (full_first, has_orcid, kw, cand.get("works_count", 0))
    matches.sort(key=score, reverse=True)

    if len(matches) == 1:
        chosen = matches[0]
    elif sys.stdin.isatty():
        idx = prompt_author_choice(name, matches)
        if idx is None:
            print(f"  Skipped '{name}' (no selection).")
            return None, None, None
        chosen = matches[idx]
    else:
        chosen = matches[0]
        print(f"  Warning: {len(matches)} authors match '{name}'; "
              f"auto-selected {chosen.get('display_name')} "
              f"(ORCID {_orcid_id(chosen.get('orcid')) or 'none'}) — "
              f"non-interactive run.")

    return chosen.get("id"), chosen.get("display_name"), _orcid_id(chosen.get("orcid"))


def _openalex_journal(work):
    """Extract a journal/venue name from an OpenAlex work."""
    loc = work.get("primary_location") or {}
    src = loc.get("source") or {}
    if src.get("display_name"):
        return src["display_name"]
    host = work.get("host_venue") or {}
    return host.get("display_name", "") or ""


def openalex_works(author_id, keywords):
    """Fetch keyword-filtered works for an author.

    Returns (papers, total_hits). total_hits is OpenAlex meta.count.
    """
    if not author_id:
        return [], 0
    aid = author_id.rsplit("/", 1)[-1]
    search = " OR ".join(keywords)
    filt = f"author.id:{aid},default.search:{search}"
    year_filt = _openalex_year_filter()
    if year_filt:
        filt += "," + year_filt
    url = (f"{OPENALEX_BASE}/works?filter={urllib.parse.quote(filt)}"
           f"&sort=cited_by_count:desc&per-page=50"
           f"&mailto={urllib.parse.quote(CONFIG['mailto'])}")
    data = http_get_json(url)
    if not data:
        return [], 0
    total = (data.get("meta") or {}).get("count", 0)
    papers = []
    for w in data.get("results", []):
        title = w.get("display_name") or ""
        if not title:
            continue
        pairs = [(a.get("author", {}).get("display_name", ""),
                  _orcid_id(a.get("author", {}).get("orcid")))
                 for a in w.get("authorships", [])]
        pairs = [(n, o) for (n, o) in pairs if n]
        authors = [n for n, _ in pairs]
        author_orcids = [o for _, o in pairs]
        doi = normalize_doi(w.get("doi"))
        url_best = (DOI_RESOLVER + doi) if doi else (w.get("id") or "")
        arxiv_id = _extract_arxiv_id(w.get("ids", {}).get("openalex", ""), w)
        papers.append(Paper(
            title=title,
            authors=authors,
            author_orcids=author_orcids,
            venue=_openalex_journal(w),
            year=w.get("publication_year"),
            doi=doi,
            url=url_best,
            citations=w.get("cited_by_count", 0) or 0,
            sources={"openalex"},
            arxiv_id=arxiv_id,
            abstract=reconstruct_abstract(w.get("abstract_inverted_index")),
        ))
    return papers, total


def _openalex_year_filter():
    """Build the OpenAlex publication_year filter clause, or '' if unbounded."""
    ymin, ymax = CONFIG["year_min"], CONFIG["year_max"]
    if ymin is not None and ymax is not None:
        return f"publication_year:{ymin}-{ymax}"
    if ymin is not None:
        return f"publication_year:>{ymin - 1}"
    if ymax is not None:
        return f"publication_year:<{ymax + 1}"
    return ""


def _extract_arxiv_id(_, work):
    """Pull an arXiv id from an OpenAlex work's locations, if any."""
    for loc in work.get("locations", []) or []:
        landing = (loc.get("landing_page_url") or "")
        m = re.search(r"arxiv\.org/abs/([0-9]+\.[0-9]+)", landing, re.I)
        if m:
            return m.group(1)
    return None


# ───────────────────────────────────────── Semantic Scholar ───
def s2_find_author(name, keywords, orcid=""):
    """Resolve a name to an S2 author id requiring a name match; prefer ORCID."""
    last, first = split_name(name)
    query = f"{first} {last}".strip()
    url = (f"{S2_BASE}/author/search?query={urllib.parse.quote(query)}"
           f"&fields=name,externalIds,paperCount&limit=10")
    data = http_get_json(url)
    if not data or not data.get("data"):
        return None, None
    cands = [c for c in data["data"]
             if name_matches(c.get("name", ""), last, first)]
    if not cands:
        return None, None
    if orcid:
        for c in cands:
            if (c.get("externalIds") or {}).get("ORCID") == orcid:
                return c.get("authorId"), c.get("name")
    best = max(cands, key=lambda c: c.get("paperCount", 0))
    return best.get("authorId"), best.get("name")


def s2_works(author_id, keywords):
    """Fetch an S2 author's papers (keyword filtering is centralized post-merge)."""
    if not author_id:
        return []
    fields = "title,abstract,year,venue,citationCount,externalIds,url,authors"
    url = (f"{S2_BASE}/author/{author_id}/papers"
           f"?fields={fields}&limit=100")
    data = http_get_json(url)
    if not data:
        return []
    papers = []
    for p in data.get("data", []):
        title = p.get("title") or ""
        if not title:
            continue
        ext = p.get("externalIds") or {}
        doi = normalize_doi(ext.get("DOI"))
        arxiv_id = ext.get("ArXiv")
        url_best = (DOI_RESOLVER + doi) if doi else (p.get("url") or "")
        authors = [a.get("name", "") for a in (p.get("authors") or [])]
        papers.append(Paper(
            title=title,
            authors=[a for a in authors if a],
            venue=p.get("venue") or "",
            year=p.get("year"),
            doi=doi,
            url=url_best,
            citations=p.get("citationCount", 0) or 0,
            sources={"s2"},
            arxiv_id=arxiv_id,
            abstract=p.get("abstract") or "",
        ))
    return papers


# ───────────────────────────────────────────────────── arXiv ───
_ATOM = "{http://www.w3.org/2005/Atom}"


def arxiv_search(name, keywords):
    """Search arXiv for an author's keyword-relevant preprints (no citations)."""
    last, first = split_name(name)
    au = f"{last}_{first}".replace(" ", "_") if first else last
    kw_clause = " OR ".join(f'all:"{k}"' for k in keywords)
    search_q = f'au:"{last}" AND ({kw_clause})'
    url = (f"{ARXIV_BASE}?search_query={urllib.parse.quote(search_q)}"
           f"&sortBy=relevance&max_results=50")
    text = http_get_text(url)
    if not text:
        return []
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        print(f"  Warning: arXiv XML parse error: {e}")
        return []
    papers = []
    for entry in root.findall(f"{_ATOM}entry"):
        title_el = entry.find(f"{_ATOM}title")
        title = (title_el.text or "").strip() if title_el is not None else ""
        if not title:
            continue
        # Confirm the named author (lastname + first initial) really appears.
        names = [ (a.find(f"{_ATOM}name").text or "")
                  for a in entry.findall(f"{_ATOM}author")
                  if a.find(f"{_ATOM}name") is not None ]
        if not any(name_matches(n, last, first) for n in names):
            continue
        id_el = entry.find(f"{_ATOM}id")
        abs_url = (id_el.text or "").strip() if id_el is not None else ""
        m = re.search(r"abs/([0-9]+\.[0-9]+)", abs_url)
        arxiv_id = m.group(1) if m else None
        published = entry.find(f"{_ATOM}published")
        year = None
        if published is not None and published.text:
            year = int(published.text[:4])
        summary_el = entry.find(f"{_ATOM}summary")
        abstract = ""
        if summary_el is not None and summary_el.text:
            abstract = re.sub(r"\s+", " ", summary_el.text).strip()
        papers.append(Paper(
            title=title,
            authors=names,
            venue="arXiv preprint",
            year=year,
            doi=None,
            url=abs_url,
            citations=0,
            sources={"arxiv"},
            arxiv_id=arxiv_id,
            abstract=abstract,
        ))
    return papers


# ───────────────────────────────────────────── Merge & dedup ───
def _absorb(into, other):
    """Merge `other`'s fields into `into` (keeping the strongest available value)."""
    into.sources |= other.sources
    into.citations = max(into.citations, other.citations)
    if not into.doi and other.doi:
        into.doi = other.doi
        into.url = DOI_RESOLVER + other.doi
    if not into.url and other.url:
        into.url = other.url
    if not into.arxiv_id and other.arxiv_id:
        into.arxiv_id = other.arxiv_id
    if not into.venue and other.venue:
        into.venue = other.venue
    if not into.year and other.year:
        into.year = other.year
    if len(other.abstract) > len(into.abstract):
        into.abstract = other.abstract
    # Keep authors + their ORCIDs as a unit; prefer the source with more ORCID
    # info, then the longer author list (so OpenAlex's ORCID list wins over arXiv).
    def info_score(p):
        return (sum(1 for o in p.author_orcids if o), len(p.authors))
    if info_score(other) > info_score(into):
        into.authors = other.authors
        into.author_orcids = other.author_orcids


def merge_papers(papers):
    """Merge papers across sources by DOI, then reconcile no-DOI dups by title."""
    merged = {}
    for p in papers:
        key = p.doi if p.doi else f"title::{normalize_title(p.title)}"
        if not key or key == "title::":
            continue
        if key in merged:
            _absorb(merged[key], p)
        else:
            merged[key] = p
    # Second pass: fold title-keyed (no-DOI, e.g. arXiv) entries into a DOI'd
    # paper sharing the same normalized title, so published + preprint don't dup.
    by_title = {normalize_title(p.title): p for p in merged.values() if p.doi}
    for key in list(merged.keys()):
        if not key.startswith("title::"):
            continue
        ntitle = key[len("title::"):]
        target = by_title.get(ntitle)
        if target is not None:
            _absorb(target, merged.pop(key))
    return list(merged.values())


# ──────────────────────────────────────────── Verification ───
_META_RE = re.compile(
    r'<meta[^>]+name=["\'](citation_title|citation_journal_title|'
    r'citation_conference_title|dc\.title)["\'][^>]+content=["\']([^"\']+)["\']',
    re.I)
_TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def verify_paper(paper):
    """Fetch the landing page and confirm title/journal match the metadata.

    Sets paper.verify_status to one of: verified, mismatch, no-landing.
    Returns True if the paper should be kept (verified), False otherwise.
    """
    landing = (DOI_RESOLVER + paper.doi) if paper.doi else paper.url
    if not landing:
        paper.verify_status = "no-landing"
        return False
    text = http_get_text(landing)
    if not text:
        paper.verify_status = "no-landing"
        return False

    page_title = ""
    page_journal = ""
    for name, content in _META_RE.findall(text):
        content = html.unescape(content).strip()
        if name.lower() in ("citation_title", "dc.title") and not page_title:
            page_title = content
        elif "journal" in name.lower() or "conference" in name.lower():
            if not page_journal:
                page_journal = content
    if not page_title:
        m = _TITLE_TAG_RE.search(text)
        if m:
            page_title = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()

    if not page_title:
        # Could not extract anything to compare against.
        paper.verify_status = "no-landing"
        return False

    if title_ratio(paper.title, page_title) < TITLE_MATCH_THRESHOLD:
        paper.verify_status = "mismatch"
        return False

    # Journal check only when both sides have a value (many landing pages omit it).
    if paper.venue and page_journal:
        if difflib.SequenceMatcher(
                None, normalize_title(paper.venue),
                normalize_title(page_journal)).ratio() < VENUE_MATCH_THRESHOLD:
            paper.verify_status = "mismatch"
            return False

    paper.verify_status = "verified"
    return True


def _rank_key(paper):
    """Sort key: title hits before abstract hits, then by citation count."""
    return (LOCATION_PRIORITY.get(paper.match_location, 0), paper.citations)


def select_top_n(papers, n):
    """Rank by match location then citations; return top-n kept (verifying as we go)."""
    ranked = sorted(papers, key=_rank_key, reverse=True)
    if not CONFIG["verify"]:
        for p in ranked[:n]:
            p.verify_status = "unverified"
        return ranked[:n], []
    kept, dropped = [], []
    for p in ranked[:CONFIG["max_candidates"]]:
        if len(kept) >= n:
            break
        ok = verify_paper(p)
        status = p.verify_status
        title_short = (p.title[:55] + "…") if len(p.title) > 55 else p.title
        print(f"    [{status:10s}] {(p.match_location or '-'):8s} "
              f"{p.citations:>5} cites  {title_short}")
        if ok:
            kept.append(p)
        else:
            dropped.append(p)
    return kept, dropped


# ──────────────────────────────────────────────── LaTeX out ───
_LATEX_SPECIAL = {
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
    "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}", "\\": r"\textbackslash{}",
}


def latex_escape(text):
    """Escape LaTeX special characters in free text."""
    if text is None:
        return ""
    out = []
    for ch in str(text):
        out.append(_LATEX_SPECIAL.get(ch, ch))
    return "".join(out)


def make_key(paper, used):
    """Stable, unique bibliography key for a paper."""
    last = "anon"
    if paper.authors:
        last = re.sub(r"[^A-Za-z]", "", paper.authors[0].split()[-1]) or "anon"
    year = paper.year or "0000"
    h = hashlib.sha1(normalize_title(paper.title).encode("utf-8")).hexdigest()[:4]
    key = f"{last}{year}{h}"
    while key in used:
        key += "x"
    used.add(key)
    return key


def author_block(name, orcid, total_hits, papers):
    """LaTeX for one author's section."""
    last, first = split_name(name)
    heading = latex_escape(f"{last}, {first}".strip().rstrip(","))
    lines = [f"\\section*{{{heading}}}"]
    if orcid:
        lines.append(f"\\noindent ORCID: \\href{{https://orcid.org/{orcid}}}"
                     f"{{{latex_escape(orcid)}}}.\\par")
    lines.append(f"\\noindent Total keyword-relevant hits (OpenAlex): "
                 f"\\textbf{{{total_hits}}}.\\par\\medskip")
    if not papers:
        lines.append("\\emph{No verified papers found.}\\par")
        return "\n".join(lines)
    lines.append("\\begin{itemize}[leftmargin=1.5em]")
    for p in papers:
        flag = "" if p.verify_status == "verified" else "$^{\\dagger}$"
        venue = f" \\emph{{{latex_escape(p.venue)}}}" if p.venue else ""
        year = f" ({p.year})" if p.year else ""
        link = (f" \\href{{{p.url}}}{{[link]}}" if p.url else "")
        matched = ", ".join(f"\\emph{{{latex_escape(k)}}} ({loc})"
                            for k, loc in p.matches.items())
        matched = f" Matched: {matched}." if matched else ""
        lines.append(
            f"  \\item \\textbf{{{latex_escape(p.title)}}}{flag}.{venue}{year} "
            f"Citations: {p.citations}.{matched}{link}~\\cite{{{p.key}}}")
    lines.append("\\end{itemize}")
    return "\n".join(lines)


def bib_block(papers):
    """Combined thebibliography of every unique paper."""
    lines = ["\\begin{thebibliography}{999}"]
    for p in papers:
        authors = latex_escape(", ".join(p.authors[:8]))
        if len(p.authors) > 8:
            authors += " et al."
        venue = latex_escape(p.venue) if p.venue else ""
        year = f" ({p.year})" if p.year else ""
        parts = [authors] if authors else []
        parts.append(f"\\emph{{{latex_escape(p.title)}}}")
        if venue:
            parts.append(venue)
        ref = ". ".join(s for s in parts if s) + year + "."
        if p.url:
            ref += f" \\href{{{p.url}}}{{{latex_escape(p.url)}}}"
        lines.append(f"\\bibitem{{{p.key}}} {ref}")
    lines.append("\\end{thebibliography}")
    return "\n".join(lines)


def year_range_str():
    """Human-readable publication-year range ('all' or 'min--max')."""
    ymin, ymax = CONFIG["year_min"], CONFIG["year_max"]
    if ymin is None and ymax is None:
        return "all"
    if ymin is not None and ymax is not None:
        return f"{ymin}--{ymax}"
    if ymin is not None:
        return f"{ymin}--present"
    return f"up to {ymax}"


def build_latex(results, all_papers, nresults, keywords):
    """Assemble the full LaTeX document string."""
    preamble = r"""\documentclass[letterpaper,11pt]{article}
\usepackage[letterpaper,margin=1in]{geometry}
\usepackage[T1]{fontenc}
\usepackage{enumitem}
\usepackage[hidelinks]{hyperref}
\title{Citation Report by Author and Keyword}
\date{\today}
\begin{document}
\maketitle
"""
    kw_list = ", ".join(latex_escape(k) for k in keywords)
    header = (
        f"\\noindent\\textbf{{Keywords:}} {kw_list}.\\par\n"
        f"\\noindent\\textbf{{Publication years:}} {year_range_str()}.\\par\n"
        f"\\noindent Per author, papers are ranked with title keyword-hits before "
        f"abstract keyword-hits, then by citation count. "
        f"Papers marked with $\\dagger$ could not be fully verified against their "
        f"landing page (title/journal mismatch or page unavailable).\\par\\medskip\n")
    body = [preamble, header]
    for name, info in results:
        body.append(author_block(name, info.get("orcid", ""),
                                 info["total_hits"], info["papers"]))
        body.append("")
    body.append("\\bigskip")
    body.append(bib_block(all_papers))
    body.append("\\end{document}")
    return "\n".join(body)


# ──────────────────────────────────────────────────── Helpers ───
def split_name(name):
    """Split 'Lastname Firstname' → (last, first). First may be ''."""
    parts = name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def read_lines(path):
    """Read non-empty, non-comment lines from a file."""
    p = Path(path)
    if not p.exists():
        print(f"Error: input file not found: {path}")
        sys.exit(1)
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            out.append(line)
    return out


# ──────────────────────────────────────────────────────── Main ───
def process_author(name, keywords, nresults):
    """Run the full pipeline for one author. Returns an info dict."""
    print(f"\n=== {name} ===")

    oa_id, oa_name, orcid = resolve_author(name, keywords)
    print(f"  OpenAlex author: {oa_name or 'NOT FOUND'} "
          f"(ORCID {orcid or 'none'}) ({oa_id or '-'})")
    oa_papers, total_hits = openalex_works(oa_id, keywords)
    print(f"  OpenAlex works: {len(oa_papers)} (total hits: {total_hits})")

    s2_id, s2_name = s2_find_author(name, keywords, orcid)
    s2_papers = s2_works(s2_id, keywords)
    print(f"  Semantic Scholar: {s2_name or 'NOT FOUND'} -> {len(s2_papers)} papers")

    ax_papers = arxiv_search(name, keywords)
    print(f"  arXiv: {len(ax_papers)} preprints")

    merged = merge_papers(oa_papers + s2_papers + ax_papers)
    print(f"  Merged unique papers: {len(merged)}")

    # Authorship harness: drop papers not actually by this author (lastname +
    # first initial, with ORCID as tie-breaker when available).
    last, first = split_name(name)
    checked = [p for p in merged if author_on_paper(p, last, first, orcid)]
    n_spurious = len(merged) - len(checked)
    if n_spurious:
        print(f"  Authorship check: dropped {n_spurious} wrong-author paper(s)")

    # Insist on a title/abstract keyword match, and apply the year-range filter.
    eligible = []
    for p in checked:
        if classify_matches(p, keywords) and within_year_range(p.year):
            eligible.append(p)
    print(f"  Eligible (matched keyword + in year range): {len(eligible)}")

    kept, dropped = select_top_n(eligible, nresults)
    print(f"  Kept {len(kept)} / dropped {len(dropped)}")

    return {
        "canonical_name": oa_name or s2_name or name,
        "orcid": orcid or "",
        "total_hits": total_hits if total_hits else len(merged),
        "spurious_dropped": n_spurious,
        "papers": kept,
        "dropped": dropped,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Search OpenAlex/Semantic Scholar/arXiv per author+keywords "
                    "and emit a LaTeX citation report.")
    parser.add_argument("authors", help="file: one 'Lastname Firstname' per line")
    parser.add_argument("keywords", help="file: one keyword/phrase per line")
    parser.add_argument("-n", "--nresults", type=int, default=5,
                        help="most-cited papers to keep per author (default 5)")
    parser.add_argument("-o", "--output", default="scholar_report.tex",
                        help="output .tex path (default scholar_report.tex)")
    parser.add_argument("--mailto", default="leyffer@anl.gov",
                        help="email for OpenAlex polite pool")
    parser.add_argument("--cache", default=None,
                        help="directory for cached API/HTTP responses")
    parser.add_argument("--year-min", type=int, default=None,
                        help="earliest publication year to include (default: all)")
    parser.add_argument("--year-max", type=int, default=None,
                        help="latest publication year to include (default: all)")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip landing-page title/journal verification")
    parser.add_argument("--max-candidates", type=int, default=20,
                        help="top-ranked papers to verify before settling on N")
    parser.add_argument("--sleep", type=float, default=1.0,
                        help="minimum seconds between requests to the same host "
                             "(per-host throttle; raise to avoid 429s)")
    args = parser.parse_args()

    CONFIG["cache"] = args.cache
    CONFIG["mailto"] = args.mailto
    CONFIG["sleep"] = args.sleep
    CONFIG["verify"] = not args.no_verify
    CONFIG["max_candidates"] = args.max_candidates
    CONFIG["year_min"] = args.year_min
    CONFIG["year_max"] = args.year_max
    CONFIG["s2_api_key"] = os.environ.get("S2_API_KEY")

    authors = read_lines(args.authors)
    keywords = read_lines(args.keywords)
    if not authors:
        print("Error: no authors found in input.")
        sys.exit(1)
    if not keywords:
        print("Error: no keywords found in input.")
        sys.exit(1)

    print(f"Authors: {len(authors)} | Keywords: {len(keywords)} | "
          f"N={args.nresults} | years={year_range_str()} | verify={CONFIG['verify']}")
    print(f"Semantic Scholar API key: "
          f"{'detected' if CONFIG['s2_api_key'] else 'not set'} | "
          f"OpenAlex polite pool: {CONFIG['mailto'] or 'none'} | "
          f"per-host spacing: {CONFIG['sleep']:.1f}s")

    results = []
    for name in authors:
        info = process_author(name, keywords, args.nresults)
        results.append((name, info))

    # Combined, deduplicated bibliography across all authors.
    all_kept = merge_papers([p for _, info in results for p in info["papers"]])
    used_keys = set()
    keymap = {}
    for p in all_kept:
        p.key = make_key(p, used_keys)
        keymap[id(p)] = p
    # Re-point each author's papers to the merged objects (so \cite keys line up).
    index = {(p.doi or normalize_title(p.title)): p for p in all_kept}
    for _, info in results:
        relinked = []
        for p in info["papers"]:
            k = p.doi or normalize_title(p.title)
            relinked.append(index.get(k, p))
        info["papers"] = relinked

    tex = build_latex(results, all_kept, args.nresults, keywords)
    out_path = Path(args.output)
    out_path.write_text(tex, encoding="utf-8")
    print(f"\nWrote LaTeX: {out_path}")

    # Sidecar JSON for reproducibility.
    json_path = out_path.with_suffix(".json")
    dump = {
        "keywords": keywords,
        "nresults": args.nresults,
        "year_range": {"min": args.year_min, "max": args.year_max},
        "authors": [
            {
                "input_name": name,
                "canonical_name": info["canonical_name"],
                "orcid": info.get("orcid", ""),
                "total_hits": info["total_hits"],
                "spurious_dropped": info.get("spurious_dropped", 0),
                "papers": [p.to_dict() for p in info["papers"]],
                "dropped": [p.to_dict() for p in info["dropped"]],
            }
            for name, info in results
        ],
    }
    json_path.write_text(json.dumps(dump, indent=2), encoding="utf-8")
    print(f"Wrote JSON:  {json_path}")
    print("\nCompile with: pdflatex (run twice) " + str(out_path))


if __name__ == "__main__":
    main()
