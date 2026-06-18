"""Core data model: the Article, URL canonicalization, and dedup keys.

The dedup key is the heart of cross-day deduplication. Two URLs that point at
the same story (differing only in tracking params, scheme, www, fragments, or a
trailing slash) must produce the same key. A separate *content* key lets us
collapse the same wire story republished under different URLs across outlets.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode


# Query params that are pure tracking noise and never identify the document.
_TRACKING_PREFIXES = ("utm_", "ga_", "fb_", "mc_")
_TRACKING_EXACT = {
    "gclid", "fbclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "ref", "ref_src", "source", "cmpid", "ncid", "spm", "igshid",
    "_hsenc", "_hsmi", "yclid", "wt_mc", "icid",
}


def _strip_tracking(query: str) -> str:
    """Drop tracking params from a query string, preserving meaningful ones in order."""
    kept = []
    for key, value in parse_qsl(query, keep_blank_values=True):
        low = key.lower()
        if low in _TRACKING_EXACT:
            continue
        if any(low.startswith(p) for p in _TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    return urlencode(kept)


def canonicalize_url(url: str) -> str:
    """Normalize a URL so trivially-different forms of the same link match.

    - lowercase scheme + host, force https
    - drop a leading 'www.'
    - strip tracking query params (keep meaningful ones)
    - drop the fragment
    - remove a trailing slash on the path (but keep root '/')
    """
    if not url:
        return ""
    url = url.strip()
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)

    scheme = "https"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    if parts.port and parts.port not in (80, 443):
        netloc = f"{host}:{parts.port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query = _strip_tracking(parts.query)

    return urlunsplit((scheme, netloc, path, query, ""))


_WORD_RE = re.compile(r"[a-z0-9]+")

# Short, high-frequency words that carry no identity signal for story matching.
_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "in", "for", "and", "or", "with", "at", "by",
    "from", "as", "is", "are", "on", "its", "this", "that", "will", "after",
    "amid", "over", "into", "how", "what", "why",
})

# Separators Google News and others use before the publisher name.
_PUBLISHER_SEPS = (" - ", " — ", " – ", " | ", " :: ")


def strip_publisher(title: str) -> str:
    """Drop a trailing ' - Publisher' / ' | Publisher' segment (Google News style)
    so the same headline from different outlets matches. Only strips when the tail
    looks like a source name (<= 6 words), to avoid eating real headline content."""
    if not title:
        return ""
    for sep in _PUBLISHER_SEPS:
        if sep in title:
            head, tail = title.rsplit(sep, 1)
            if head.strip() and 0 < len(tail.split()) <= 6:
                title = head
    return title.strip()


def normalize_title(title: str) -> str:
    """Lowercase, alphanumeric-only, whitespace-collapsed title."""
    if not title:
        return ""
    return " ".join(_WORD_RE.findall(title.lower()))


def story_tokens(title: str) -> list[str]:
    """Significant tokens identifying a story: publisher stripped, lowercased,
    stopwords removed. Used for both the exact story key and fuzzy matching."""
    headline = strip_publisher(title)
    return [t for t in _WORD_RE.findall(headline.lower()) if t not in _STOPWORDS]


def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


@dataclass
class Article:
    """A single candidate item flowing through the pipeline.

    `authority` is a 0-100 rank used to pick the surviving copy when the same
    story appears across outlets (higher wins). Government/court sources rank
    highest, general press lowest. `published` is timezone-aware UTC or None
    (undated items are kept by the recency filter, per the handoff).
    """

    topic: str
    title: str
    url: str
    source: str
    authority: int = 0
    published: Optional[datetime] = None
    snippet: str = ""

    # Populated by the LLM scoring stage.
    relevant: Optional[bool] = None
    score: Optional[float] = None
    reason: str = ""
    summary: str = ""

    # Secondary articles covering the same story, attached by the clustering
    # pass. Each is {"title", "url", "source"}; shown as "Also covered by" links.
    also: list = field(default_factory=list)

    canonical_url: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self.canonical_url = canonicalize_url(self.url)
        if self.published is not None and self.published.tzinfo is None:
            self.published = self.published.replace(tzinfo=timezone.utc)

    @property
    def dedup_key(self) -> str:
        """Identity of *this exact link*, for the cross-day seen-store."""
        return "u:" + _sha1(self.canonical_url or self.url)

    @property
    def content_key(self) -> str:
        """Identity of *this story* for near-duplicate collapse.

        Global (not topic-scoped) so the same wire story relevant to two topics
        is shown once, not once per topic. Built from publisher-stripped,
        stopword-filtered tokens so cross-outlet variants of one headline match.
        """
        return "c:" + _sha1(" ".join(story_tokens(self.title)))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("canonical_url", None)
        d["canonical_url"] = self.canonical_url
        d["published"] = self.published.isoformat() if self.published else None
        return d
