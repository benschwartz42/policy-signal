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
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import quote_plus, urljoin, urlsplit

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
# A normal browser User-Agent: many sites reject obvious bot agents but serve
# their public pages/feeds fine to a browser. (Sites behind a WAF still 403.)
_UA = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml,*/*;q=0.8",
}


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

def _entries_to_articles(parsed, topic_name: str, source_name: str, authority: int) -> list[Article]:
    out = []
    for e in parsed.entries[:25]:
        published = None
        if getattr(e, "published_parsed", None):
            published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        out.append(Article(
            topic=topic_name,
            title=getattr(e, "title", "").strip(),
            url=getattr(e, "link", ""),
            source=getattr(getattr(e, "source", None), "title", source_name) or source_name,
            authority=authority,
            published=published,
            snippet=getattr(e, "summary", "").strip(),
        ))
    return out


def _parse_feed(feed_url: str, topic: Topic, source_name: str, authority: int) -> list[Article]:
    import feedparser  # imported lazily; heavy-ish
    parsed = feedparser.parse(feed_url, request_headers=_UA)
    return _entries_to_articles(parsed, topic.name, source_name, authority)


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


# --- Custom sources: any feed OR any website -------------------------------

class _PageParser(HTMLParser):
    """Collects <a href> links (with text) and any <link> feed auto-discovery
    hrefs from a page, using only the stdlib."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []   # (href, anchor text)
        self.feeds: list[str] = []               # discovered rss/atom hrefs
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a" and d.get("href"):
            self._href, self._text = d["href"], []
        elif tag == "link" and d.get("href"):
            typ = (d.get("type") or "").lower()
            rel = (d.get("rel") or "").lower()
            if "rss" in typ or "atom" in typ or ("alternate" in rel and "xml" in typ):
                self.feeds.append(d["href"])

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href, self._text = None, []


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _same_site(a: str, b: str) -> bool:
    ha, hb = _host(a), _host(b)
    return bool(ha) and (ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha))


def _article_candidates(links: list[tuple[str, str]], base_url: str) -> list[tuple[str, str]]:
    """Filter raw anchors to plausible article links: same site, substantive
    anchor text, de-duplicated. Returns (text, absolute_url)."""
    seen, out = set(), []
    for href, text in links:
        absu = urljoin(base_url, href).split("#")[0]
        if not absu.startswith("http") or not _same_site(absu, base_url):
            continue
        words = len(text.split())
        if words < 3 or words > 40 or absu in seen:
            continue
        seen.add(absu)
        out.append((text, absu))
        if len(out) >= 80:
            break
    return out


_EXTRACT_SYSTEM = (
    "You are given links scraped from a web page. Return ONLY the ones that are "
    "individual news articles, press releases, or policy/document items — NOT "
    "navigation, section/category/tag pages, logins, or ads. Respond with ONLY "
    'JSON: {"articles": [{"title": "...", "url": "..."}]}. Use the exact url '
    "from the list; never invent a url."
)


def _llm_pick_articles(candidates, page_url, client, model) -> list[dict]:
    from .relevance import _extract_json  # shared tolerant JSON parser

    listing = "\n".join(f"- {t} | {u}" for t, u in candidates)
    msg = client.messages.create(
        model=model, max_tokens=1500, system=_EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": f"PAGE: {page_url}\nLINKS:\n{listing}"}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    valid = {u: t for t, u in candidates}
    out = []
    for a in _extract_json(text).get("articles", []) or []:
        url = str(a.get("url", "")).split("#")[0]
        if url in valid:
            out.append({"title": (a.get("title") or valid[url]).strip(), "url": url})
    return out


def ingest_custom_source(url: str, name: str, topics: list[Topic],
                         client=None, model: str = "") -> tuple[list[Article], str]:
    """Resolve a custom source URL three ways, in order: a real feed,
    an auto-discovered feed, or scraped+LLM-extracted article links."""
    import feedparser

    # 1. The URL is itself a feed.
    parsed = feedparser.parse(url, request_headers=_UA)
    if parsed.entries:
        label = name or getattr(parsed.feed, "title", "") or _host(url)
        arts = [a for t in topics for a in _entries_to_articles(parsed, t.name, label, AUTHORITY["rss"])]
        return arts, f"feed ({len(parsed.entries[:25])} entries)"

    # Fetch the page once for both auto-discovery and scraping.
    r = requests.get(url, headers=_UA, timeout=_TIMEOUT)
    r.raise_for_status()
    page = _PageParser()
    page.feed(r.text[:600000])

    # 2. Auto-discovered feed in the page <head>.
    for fl in page.feeds:
        try:
            p2 = feedparser.parse(urljoin(url, fl.strip()), request_headers=_UA)
        except Exception:
            continue  # malformed discovered href — skip
        if p2.entries:
            label = name or getattr(p2.feed, "title", "") or _host(url)
            arts = [a for t in topics for a in _entries_to_articles(p2, t.name, label, AUTHORITY["rss"])]
            return arts, f"auto-discovered feed ({len(p2.entries[:25])} entries)"

    # 3. Scrape + LLM extraction (needs the model).
    if client is None:
        return [], "no feed found; LLM unavailable"
    candidates = _article_candidates(page.links, url)
    picked = _llm_pick_articles(candidates, url, client, model)
    label = name or _host(url)
    arts = [
        Article(topic=t.name, title=it["title"], url=it["url"], source=label,
                authority=AUTHORITY["rss"], published=None, snippet=it["title"])
        for t in topics for it in picked[:25]
    ]
    return arts, f"scraped ({len(picked)} articles)"


REGISTRY: dict[str, Callable[[Topic, dict], list[Article]]] = {
    "federal_register": federal_register,
    "regulations_gov": regulations_gov,
    "courtlistener": courtlistener,
    "google_news": google_news,
    "rss": rss,
}


def ingest(topics: list[Topic], source_ids: list[str], env: dict | None = None,
           feeds: list | None = None, client=None, model: str = "") -> list[Article]:
    """Query every enabled source for every topic, plus any custom sources (a
    feed URL or any website). A failing source is logged and skipped, never fatal."""
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

    # Custom sources: each is resolved (feed / auto-discovered feed / scraped),
    # fetched once, and fanned out to every topic; the keyword prefilter + LLM
    # scoring decide which topic(s) each item belongs to.
    for feed in feeds or []:
        url = getattr(feed, "url", None) or (feed.get("url") if isinstance(feed, dict) else None)
        if not url:
            continue
        name = getattr(feed, "name", "") or (feed.get("name", "") if isinstance(feed, dict) else "")
        try:
            items, how = ingest_custom_source(url, name, topics, client=client, model=model)
            log.info("custom source '%s' via %s -> %d items", name or url, how, len(items))
            collected.extend(items)
        except Exception as exc:
            log.warning("custom source '%s' failed: %s", name or url, exc)
    return collected
