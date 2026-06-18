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
