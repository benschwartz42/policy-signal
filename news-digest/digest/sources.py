"""Source connectors. Authoritative-first: government and court sources are
primary, general news is the secondary net.

Contract for every connector: take a Topic, return a list[Article]; on any
failure, raise — the caller (ingest) logs and skips that source so one dead
source never aborts the run. Bing Search/News API was retired 2025-08-11 and is
intentionally absent.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import quote_plus

import requests

from .config import Topic
from .models import Article

log = logging.getLogger("digest.sources")

# Authority ranks (0-100): higher wins when collapsing the same story.
AUTHORITY = {
    "courtlistener": 90,
    "regulations_gov": 85,
    "federal_register": 80,
    "rss": 40,          # curated trade press
    "google_news": 30,  # broad net
}

_TIMEOUT = 20
_UA = {"User-Agent": "policy-signal/1.0 (+https://github.com/) digest-bot"}


def _query_terms(topic: Topic) -> str:
    """Build a search string from the topic name plus its keywords.

    Used by the structured government/court APIs, whose term search tolerates a
    longer phrase. NOT used for press search — see _press_query.
    """
    terms = [topic.name] + list(topic.keywords)
    # De-dup while preserving order.
    seen, out = set(), []
    for t in terms:
        tl = t.lower()
        if tl not in seen:
            seen.add(tl)
            out.append(t)
    return " ".join(out)


def _press_query(topic: Topic) -> str:
    """High-recall OR query for press/RSS search.

    Google News treats a long space-separated query as a near-AND search, which
    collapses recall and drops relevant articles that don't echo most terms. So
    we OR together quoted phrases: the topic name plus any *multi-word* keyword.
    Single-token keywords (e.g. 'IDR', 'arbitration', 'QPA') are deliberately
    left to the keyword prefilter / LLM stage rather than the search, to avoid
    flooding results with generic-term noise.
    """
    phrases = [topic.name] + [k for k in topic.keywords if " " in k.strip()]
    seen, out = set(), []
    for p in phrases:
        p = p.strip()
        if p and p.lower() not in seen:
            seen.add(p.lower())
            out.append(f'"{p}"')
    return " OR ".join(out)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(value, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# --- Government: Federal Register -------------------------------------------

def federal_register(topic: Topic, env: dict) -> list[Article]:
    url = "https://www.federalregister.gov/api/v1/documents.json"
    params = {
        "conditions[term]": _query_terms(topic),
        "order": "newest",
        "per_page": 20,
        "fields[]": ["title", "html_url", "publication_date", "abstract", "type"],
    }
    r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for d in r.json().get("results", []):
        out.append(Article(
            topic=topic.name,
            title=d.get("title", "").strip(),
            url=d.get("html_url", ""),
            source="Federal Register",
            authority=AUTHORITY["federal_register"],
            published=_parse_dt(d.get("publication_date")),
            snippet=(d.get("abstract") or "").strip(),
        ))
    return out


# --- Government: Regulations.gov (API key required) -------------------------

def regulations_gov(topic: Topic, env: dict) -> list[Article]:
    key = env.get("REGS_GOV_API_KEY")
    if not key:
        raise RuntimeError("REGS_GOV_API_KEY not set")
    url = "https://api.regulations.gov/v4/documents"
    params = {
        "filter[searchTerm]": _query_terms(topic),
        "sort": "-postedDate",
        "page[size]": 20,
        "api_key": key,
    }
    r = requests.get(url, params=params, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for d in r.json().get("data", []):
        attr = d.get("attributes", {})
        doc_id = d.get("id", "")
        out.append(Article(
            topic=topic.name,
            title=(attr.get("title") or "").strip(),
            url=f"https://www.regulations.gov/document/{doc_id}",
            source="Regulations.gov",
            authority=AUTHORITY["regulations_gov"],
            published=_parse_dt(attr.get("postedDate")),
            snippet=(attr.get("docketId") or "").strip(),
        ))
    return out


# --- Courts: CourtListener (token optional) ---------------------------------

def courtlistener(topic: Topic, env: dict) -> list[Article]:
    url = "https://www.courtlistener.com/api/rest/v4/search/"
    params = {"q": _query_terms(topic), "type": "o", "order_by": "dateFiled desc"}
    headers = dict(_UA)
    token = env.get("COURTLISTENER_TOKEN")
    if token:
        headers["Authorization"] = f"Token {token}"
    r = requests.get(url, params=params, headers=headers, timeout=_TIMEOUT)
    r.raise_for_status()
    out = []
    for d in r.json().get("results", [])[:20]:
        path = d.get("absolute_url", "")
        full = f"https://www.courtlistener.com{path}" if path.startswith("/") else path
        out.append(Article(
            topic=topic.name,
            title=(d.get("caseName") or d.get("caseNameFull") or "").strip(),
            url=full,
            source=f"CourtListener ({d.get('court_citation_string', 'court')})",
            authority=AUTHORITY["courtlistener"],
            published=_parse_dt(d.get("dateFiled")),
            snippet=(d.get("snippet") or "").strip(),
        ))
    return out


# --- Press: Google News RSS + generic RSS -----------------------------------

def _parse_feed(feed_url: str, topic: Topic, source_name: str, authority: int) -> list[Article]:
    import feedparser  # imported lazily; heavy-ish
    parsed = feedparser.parse(feed_url, request_headers=_UA)
    out = []
    for e in parsed.entries[:25]:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        out.append(Article(
            topic=topic.name,
            title=getattr(e, "title", "").strip(),
            url=getattr(e, "link", ""),
            source=getattr(getattr(e, "source", None), "title", source_name) or source_name,
            authority=authority,
            published=published,
            snippet=getattr(e, "summary", "").strip(),
        ))
    return out


def google_news(topic: Topic, env: dict) -> list[Article]:
    q = quote_plus(_press_query(topic))
    feed = f"https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
    return _parse_feed(feed, topic, "Google News", AUTHORITY["google_news"])


def rss(topic: Topic, env: dict) -> list[Article]:
    """Generic RSS: pulls each feed listed under the topic's `extra_rss`."""
    out: list[Article] = []
    for feed_url in topic.extra_rss:
        try:
            out.extend(_parse_feed(feed_url, topic, "RSS", AUTHORITY["rss"]))
        except Exception as exc:  # one bad feed shouldn't sink the rest
            log.warning("rss feed failed (%s): %s", feed_url, exc)
    return out


REGISTRY: dict[str, Callable[[Topic, dict], list[Article]]] = {
    "federal_register": federal_register,
    "regulations_gov": regulations_gov,
    "courtlistener": courtlistener,
    "google_news": google_news,
    "rss": rss,
}


def ingest(topics: list[Topic], source_ids: list[str], env: dict | None = None) -> list[Article]:
    """Query every enabled source for every topic. A failing source is logged
    and skipped, never fatal."""
    env = env or dict(os.environ)
    collected: list[Article] = []
    for source_id in source_ids:
        fn = REGISTRY.get(source_id)
        if fn is None:
            log.warning("unknown source '%s' — skipping", source_id)
            continue
        for topic in topics:
            try:
                items = fn(topic, env)
                log.info("%s/%s -> %d items", source_id, topic.name, len(items))
                collected.extend(items)
            except Exception as exc:
                log.warning("source '%s' failed for topic '%s': %s", source_id, topic.name, exc)
    return collected
