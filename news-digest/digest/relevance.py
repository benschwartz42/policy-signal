"""Relevance: recency filter, cheap keyword prefilter, then LLM scoring.

The ordering matters for cost: recency and dedup cut volume for free, the
keyword prefilter cuts more for free, and only the survivors reach the LLM. The
LLM judges each item against the topic's plain-English `description` (the rubric)
and returns {relevant, score, reason, summary}.

`score_offline` is a deterministic stub used by --self-test and whenever no
ANTHROPIC_API_KEY is present, so the pipeline is exercised end-to-end with no
network and no spend.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from .config import Settings, Topic
from .models import Article

log = logging.getLogger("digest.relevance")


def filter_recent(articles: list[Article], lookback_hours: int) -> list[Article]:
    """Drop items older than the lookback window. Undated items are KEPT
    (per the handoff: better to over-include than silently drop)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    out = []
    for a in articles:
        if a.published is None or a.published >= cutoff:
            out.append(a)
    return out


def _topic_terms(topic: Topic) -> list[str]:
    terms = [topic.name] + list(topic.keywords)
    return [t.lower() for t in terms if t.strip()]


def keyword_prefilter(articles: list[Article], topic: Topic) -> list[Article]:
    """Keep items whose title/snippet mention any topic term. Pure volume cut
    before any LLM spend. If a topic declares no keywords, the name alone is the
    term (so nothing is dropped spuriously for keyword-less topics)."""
    terms = _topic_terms(topic)
    if not terms:
        return articles
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    out = []
    for a in articles:
        haystack = f"{a.title}\n{a.snippet}"
        if pattern.search(haystack):
            out.append(a)
    return out


# --- Scoring -----------------------------------------------------------------

_SYSTEM = (
    "You are a policy analyst writing a daily digest for a healthcare "
    "revenue-cycle team. Given a topic definition and a news/document item, "
    "judge whether the item is genuinely relevant to the topic and score your "
    "confidence 0.0-1.0.\n"
    "Then write a SELF-CONTAINED summary of 3-5 sentences that lets a busy "
    "reader fully understand the development WITHOUT opening the article. Cover: "
    "what happened; the concrete specifics (agency/court, rule or case name, "
    "effective dates, dollar amounts, percentages, deadlines); who is affected; "
    "and why it matters for a revenue-cycle team. Be factual and neutral, do not "
    "speculate beyond the source, and do not just restate the headline. If a "
    "detail isn't in the source, omit it rather than inventing it.\n"
    "Also give a one-sentence reason for the relevance decision.\n"
    'Respond with ONLY a JSON object: '
    '{"relevant": bool, "score": number, "reason": string, "summary": string}.'
)


def _build_prompt(topic: Topic, article: Article) -> str:
    return (
        f"TOPIC: {topic.name}\n"
        f"TOPIC DEFINITION (relevance rubric): {topic.description}\n\n"
        f"ITEM TITLE: {article.title}\n"
        f"ITEM SOURCE: {article.source}\n"
        f"ITEM EXCERPT: {article.snippet[:1500]}\n"
    )


def score_offline(topic: Topic, article: Article) -> dict:
    """Deterministic, network-free scoring. Score = fraction of topic terms that
    appear in the item, lightly boosted for authoritative sources."""
    terms = _topic_terms(topic)
    haystack = f"{article.title}\n{article.snippet}".lower()
    if not terms:
        hits_ratio = 1.0
    else:
        hits = sum(1 for t in terms if t in haystack)
        hits_ratio = hits / len(terms)
    score = min(1.0, hits_ratio + (0.15 if article.authority >= 80 else 0.0))
    score = round(score, 3)
    return {
        "relevant": score >= 0.5,
        "score": score,
        "reason": f"Matched {int(hits_ratio * len(terms))}/{len(terms)} topic terms (offline stub).",
        "summary": (article.snippet[:600] or article.title).strip(),
    }


def _score_llm(topic: Topic, article: Article, client, model: str) -> dict:
    msg = client.messages.create(
        model=model,
        max_tokens=800,
        system=_SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(topic, article)}],
    )
    text = "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")
    data = _extract_json(text)
    return {
        "relevant": bool(data.get("relevant", False)),
        "score": float(data.get("score", 0.0)),
        "reason": str(data.get("reason", "")),
        "summary": str(data.get("summary", "")),
    }


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of model output, tolerating stray prose."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return {"relevant": False, "score": 0.0, "reason": "unparseable model output", "summary": ""}


# Grouping guidance by tolerance level, injected into the clustering prompt.
_TOLERANCE_RULES = {
    "strict": "Group ONLY items that report the exact same document, ruling, "
              "filing, or event. If two items cover different angles, different "
              "actions, or different events, keep them in separate groups.",
    "balanced": "Group items that report the SAME underlying development — the "
                "same rule, ruling, filing, announcement, or event — even when "
                "headlines differ or come from different outlets. Items about "
                "DIFFERENT developments stay separate; when unsure, keep separate.",
    "broad": "Group items that cover the same overall development OR closely "
             "related aspects, reactions, and analysis of it, even from different "
             "angles. Prefer fewer, broader groups; only separate items that are "
             "about clearly distinct developments.",
}


def _cluster_system(tolerance: str) -> str:
    rule = _TOLERANCE_RULES.get(tolerance, _TOLERANCE_RULES["balanced"])
    return (
        "You group related news/document items for a policy digest. " + rule +
        " Respond with ONLY a JSON object {\"clusters\": [[indices], ...]} "
        "where every item index appears exactly once."
    )


def cluster_same_story(articles: list[Article], client=None, model: str = "",
                       tolerance: str = "balanced") -> list[Article]:
    """Merge items reporting the same development into one, attaching the others
    as `also` (secondary links). `tolerance` (strict|balanced|broad) controls how
    aggressively items are grouped. The most relevant/authoritative item is the
    primary and keeps its summary. Requires the LLM client; offline it is a
    no-op (returns the list unchanged), so the self-test is unaffected."""
    if client is None or len(articles) < 2:
        return articles

    listing = "\n".join(
        f"[{i}] ({a.source}) {a.title} — {(a.summary or '')[:240]}"
        for i, a in enumerate(articles)
    )
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_cluster_system(tolerance),
            messages=[{"role": "user", "content": "ITEMS:\n" + listing}],
        )
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        raw = _extract_json(text).get("clusters")
        if not isinstance(raw, list):
            log.warning("clustering returned no clusters — leaving items unmerged")
            return articles
    except Exception as exc:
        log.warning("clustering failed — leaving items unmerged: %s", exc)
        return articles

    # Repair rather than reject: keep valid, in-range, first-seen indices; an
    # imperfect grouping from the model still merges what it can, and any item
    # it omitted simply stays on its own (a safe singleton).
    n = len(articles)
    clusters: list[list[int]] = []
    assigned: set[int] = set()
    for group in raw:
        if not isinstance(group, list):
            continue
        kept_idx = []
        for i in group:
            try:
                idx = int(i)
            except (TypeError, ValueError):
                continue
            if 0 <= idx < n and idx not in assigned:
                assigned.add(idx)
                kept_idx.append(idx)
        if kept_idx:
            clusters.append(kept_idx)
    for idx in range(n):
        if idx not in assigned:
            clusters.append([idx])

    merged: list[Article] = []
    for cluster in clusters:
        members = [articles[i] for i in cluster]
        primary = max(members, key=lambda a: ((a.score or 0.0), a.authority, len(a.summary or "")))
        secondaries = sorted((m for m in members if m is not primary),
                             key=lambda a: a.authority, reverse=True)
        primary.also = [{"title": m.title, "url": m.url, "source": m.source} for m in secondaries]
        merged.append(primary)

    if len(merged) < len(articles):
        log.info("clustering merged %d items into %d", len(articles), len(merged))
    return merged


def score_articles(
    articles: list[Article],
    topic: Topic,
    settings: Settings,
    client=None,
) -> list[Article]:
    """Annotate each article with relevance fields. Uses the live LLM when a
    client is supplied, otherwise the offline stub. A failed individual call
    falls back to the offline stub for that item rather than dropping it."""
    for a in articles:
        try:
            result = _score_llm(topic, a, client, settings.model) if client else score_offline(topic, a)
        except Exception as exc:
            log.warning("LLM scoring failed for '%s' — using offline stub: %s", a.title[:60], exc)
            result = score_offline(topic, a)
        a.relevant = result["relevant"]
        a.score = result["score"]
        a.reason = result["reason"]
        a.summary = result["summary"]
    return articles
