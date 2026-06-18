"""Deduplication: a durable cross-day seen-store + near-duplicate collapse.

Two distinct jobs:

1. SeenStore — remembers which exact links we've already emailed, across runs,
   so a story is never sent twice. Pluggable backend; the default JSON store is
   committed back to the repo by the GitHub Actions workflow (durable, free, no
   AWS). Entries carry a timestamp and are pruned past `ttl_days` on load.

2. collapse_near_duplicates — within a single run, folds the same wire story
   appearing across multiple outlets down to the single most authoritative copy.

The store interface is deliberately small (`seen` / `mark` / `save`) so a future
S3- or DynamoDB-backed implementation is a drop-in replacement.
"""

from __future__ import annotations

import json
import os
import time
from typing import Iterable, Protocol

from .models import Article


class SeenStore(Protocol):
    """Minimal interface every seen-store backend must provide."""

    def seen(self, key: str) -> bool: ...
    def mark(self, key: str) -> None: ...
    def save(self) -> None: ...


class JSONSeenStore:
    """File-backed seen-store. Maps dedup_key -> epoch seconds first seen.

    On load, entries older than ttl_days are dropped, which keeps the file small
    and bounds it the same way a DynamoDB TTL would. Designed to be committed to
    git by the daily workflow; the file is small and stable.
    """

    def __init__(self, path: str, ttl_days: int = 30) -> None:
        self.path = path
        self.ttl_seconds = ttl_days * 86400
        self._data: dict[str, float] = {}
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (json.JSONDecodeError, OSError):
            # A corrupt store should not abort the run; start fresh, log upstream.
            raw = {}
        now = time.time()
        cutoff = now - self.ttl_seconds
        self._data = {
            k: float(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and float(v) >= cutoff
        }
        # If pruning changed anything, mark dirty so save() persists the shrink.
        if len(self._data) != len(raw):
            self._dirty = True

    def seen(self, key: str) -> bool:
        return key in self._data

    def mark(self, key: str) -> None:
        if key not in self._data:
            self._data[key] = time.time()
            self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2, sort_keys=True)
        os.replace(tmp, self.path)
        self._dirty = False


class MemorySeenStore:
    """In-memory store for tests and the offline self-test."""

    def __init__(self, initial: Iterable[str] = ()) -> None:
        self._data: set[str] = set(initial)

    def seen(self, key: str) -> bool:
        return key in self._data

    def mark(self, key: str) -> None:
        self._data.add(key)

    def save(self) -> None:
        pass


def collapse_near_duplicates(articles: list[Article]) -> list[Article]:
    """Fold same-story duplicates to the single most authoritative copy.

    Two passes:
      1. exact story key (content_key) — publisher-stripped, stopword-filtered,
         so cross-outlet variants of one headline match;
      2. fuzzy token-overlap — catches near-identical headlines the exact key
         misses (e.g. an added 'Appeals' or reordered words).

    Ties on authority break by presence of a publish date, then longer snippet,
    then stably by URL.
    """
    # Pass 1: exact key.
    best: dict[str, Article] = {}
    for art in articles:
        key = art.content_key
        incumbent = best.get(key)
        if incumbent is None or _prefer(art, incumbent):
            best[key] = art
    winners = set(id(a) for a in best.values())
    survivors = [a for a in articles if id(a) in winners]

    # Pass 2: conservative fuzzy collapse.
    return _fuzzy_collapse(survivors)


def _fuzzy_collapse(articles: list[Article], threshold: float = 0.85,
                    min_tokens: int = 5) -> list[Article]:
    """Collapse survivors whose significant-token sets overlap >= threshold
    (Jaccard). Conservative: only compares titles with enough tokens, so short
    generic headlines aren't wrongly merged."""
    from .models import story_tokens

    kept: list[Article] = []
    kept_sets: list[set] = []
    for art in articles:
        toks = set(story_tokens(art.title))
        merged = False
        if len(toks) >= min_tokens:
            for i, ks in enumerate(kept_sets):
                if len(ks) < min_tokens:
                    continue
                union = toks | ks
                if union and len(toks & ks) / len(union) >= threshold:
                    if _prefer(art, kept[i]):
                        kept[i], kept_sets[i] = art, toks
                    merged = True
                    break
        if not merged:
            kept.append(art)
            kept_sets.append(toks)
    return kept


def _prefer(candidate: Article, incumbent: Article) -> bool:
    """True if `candidate` is a better representative of the story than `incumbent`."""
    if candidate.authority != incumbent.authority:
        return candidate.authority > incumbent.authority
    if (candidate.published is not None) != (incumbent.published is not None):
        return candidate.published is not None
    if len(candidate.snippet) != len(incumbent.snippet):
        return len(candidate.snippet) > len(incumbent.snippet)
    return candidate.canonical_url < incumbent.canonical_url


def filter_unseen(articles: list[Article], store: SeenStore) -> list[Article]:
    """Drop articles already seen, by *either* exact-URL key or story key.

    Checking content_key as well as dedup_key means the same wire story
    reappearing the next day under a different outlet's URL is still suppressed,
    not just the identical link. Does not mark; marking happens only after
    successful delivery so a failed send doesn't suppress a story."""
    return [
        a for a in articles
        if not store.seen(a.dedup_key) and not store.seen(a.content_key)
    ]


def mark_delivered(articles: list[Article], store: SeenStore) -> None:
    """Record both keys for each delivered article so neither the exact link nor
    the same story via another outlet is re-sent on a later run. Also marks the
    secondary ('also') articles folded into each item, so they don't resurface
    as standalone items next time."""
    for a in articles:
        store.mark(a.dedup_key)
        store.mark(a.content_key)
        for sec in getattr(a, "also", None) or []:
            ghost = Article(topic="", title=sec.get("title", ""),
                            url=sec.get("url", ""), source=sec.get("source", ""))
            store.mark(ghost.dedup_key)
            store.mark(ghost.content_key)
