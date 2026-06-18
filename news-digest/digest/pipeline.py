"""Pipeline orchestration:

    ingest -> recency -> dedup(seen) -> collapse -> prefilter -> LLM score
           -> cutoff -> cap -> render -> deliver -> mark seen

`run` wires the real stages together. `self_test` runs the same logic over
in-memory fixtures with the offline scorer and asserts each behavior, so it needs
no keys and no network.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import relevance, render
from .config import Config, Delivery, Settings, Topic
from .dedup import (
    JSONSeenStore,
    MemorySeenStore,
    SeenStore,
    collapse_near_duplicates,
    filter_unseen,
    mark_delivered,
)
from .models import Article

log = logging.getLogger("digest.pipeline")


@dataclass
class RunResult:
    payload: dict
    html: str
    text: str
    json: str
    delivered_via: str | None = None
    kept: list[Article] = field(default_factory=list)


def _make_client(settings: Settings, env: dict):
    """Return an Anthropic client if a key is present, else None (offline stub)."""
    if not env.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed — falling back to offline scoring")
        return None
    return anthropic.Anthropic(api_key=env["ANTHROPIC_API_KEY"])


def process(
    articles: list[Article],
    config: Config,
    store: SeenStore,
    client=None,
) -> list[Article]:
    """The pure transform: candidates in, kept-and-scored articles out.
    Does not deliver and does not mark the store (caller owns those side effects)."""
    s = config.settings

    # 1. recency
    fresh = relevance.filter_recent(articles, s.lookback_hours)
    # 2. cross-day dedup against the durable seen-store
    unseen = filter_unseen(fresh, store)
    # 3. near-duplicate collapse (same wire story across outlets)
    collapsed = collapse_near_duplicates(unseen)

    kept: list[Article] = []
    by_topic: dict[str, Topic] = {t.name: t for t in config.topics}
    # group survivors by topic for per-topic prefilter + scoring + cap
    per_topic: dict[str, list[Article]] = {}
    for a in collapsed:
        per_topic.setdefault(a.topic, []).append(a)

    for topic_name, items in per_topic.items():
        topic = by_topic.get(topic_name)
        if topic is None:
            continue
        # 4. keyword prefilter
        pre = relevance.keyword_prefilter(items, topic)
        # 5. LLM (or offline) scoring
        scored = relevance.score_articles(pre, topic, s, client=client)
        # 6. relevance cutoff
        relevant = [a for a in scored if (a.score or 0.0) >= s.min_relevance and a.relevant]
        # 7. cap per topic (already score-sorted later in render, sort here for the cap)
        relevant.sort(key=lambda x: x.score or 0.0, reverse=True)
        kept.extend(relevant[: s.max_items_per_topic])

    return kept


def run(
    config: Config,
    dry_run: bool = False,
    env: dict | None = None,
    emit_json_path: str | None = None,
    write_artifacts: bool = True,
) -> RunResult:
    """Full live run. Ingests real sources, scores, renders, and (unless dry_run)
    delivers email and marks the seen-store."""
    from . import sources, deliver  # local import keeps self-test free of network deps

    env = env or dict(os.environ)
    s = config.settings
    store: SeenStore = JSONSeenStore(s.seen_store_path, s.seen_ttl_days)
    client = _make_client(s, env)

    candidates = sources.ingest(config.topics, s.sources, env)
    log.info("ingested %d candidate items", len(candidates))

    kept = process(candidates, config, store, client=client)
    log.info("kept %d relevant items after scoring", len(kept))

    payload = render.build_payload(kept)
    html = render.render_html(payload)
    text = render.render_text(payload)
    json_str = render.render_json(payload)

    if write_artifacts:
        _write("digest.html", html)
        _write("digest.txt", text)
    if emit_json_path:
        _write(emit_json_path, json_str)

    result = RunResult(payload=payload, html=html, text=text, json=json_str, kept=kept)

    if dry_run:
        log.info("dry-run: skipping delivery and seen-store update")
        return result

    subject = f"{config.delivery.subject_prefix} {datetime.now(timezone.utc):%Y-%m-%d} — {len(kept)} items"
    result.delivered_via = deliver.deliver(config.delivery, subject, html, text, env)

    # Mark seen ONLY after a successful send, so a failed delivery doesn't suppress.
    mark_delivered(kept, store)
    store.save()
    return result


def _write(path: str, content: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)


# --------------------------------------------------------------------------- #
# Offline self-test                                                            #
# --------------------------------------------------------------------------- #

def _fixture_config() -> Config:
    return Config(
        topics=[
            Topic(
                name="No Surprises Act",
                description="Federal balance-billing protections: rulemaking, IDR, litigation.",
                keywords=["no surprises act", "balance billing", "idr", "independent dispute resolution"],
            ),
            Topic(
                name="Prior Authorization",
                description="Payer prior-authorization rules and reform.",
                keywords=["prior authorization", "prior auth"],
            ),
        ],
        settings=Settings(lookback_hours=48, max_items_per_topic=5, min_relevance=0.5),
        delivery=Delivery(sender="digest@example.com", recipients=["team@example.com"]),
    )


def _fixtures() -> list[Article]:
    now = datetime.now(timezone.utc)
    return [
        # Relevant, authoritative, recent.
        Article("No Surprises Act", "CMS issues No Surprises Act IDR final rule",
                "https://www.federalregister.gov/d/2026-1?utm_source=x", "Federal Register",
                authority=80, published=now - timedelta(hours=3),
                snippet="Independent dispute resolution under the No Surprises Act balance billing."),
        # Same wire story, two outlets -> must collapse to the more authoritative.
        Article("No Surprises Act", "Court rules on No Surprises Act IDR process",
                "https://news.example.com/a", "Outlet A", authority=30,
                published=now - timedelta(hours=5),
                snippet="A court ruled on the independent dispute resolution balance billing process."),
        Article("No Surprises Act", "Court Rules on No Surprises Act IDR Process!",
                "https://www.courtlistener.com/opinion/9/", "CourtListener", authority=90,
                published=now - timedelta(hours=6),
                snippet="The court ruled on the independent dispute resolution balance billing process in detail."),
        # Too old -> dropped by recency.
        Article("No Surprises Act", "Old No Surprises Act balance billing item",
                "https://old.example.com/x", "Outlet B", authority=30,
                published=now - timedelta(hours=200),
                snippet="balance billing idr"),
        # Off-topic for its topic -> prefilter/score should drop it.
        Article("Prior Authorization", "Local bakery wins award",
                "https://news.example.com/bakery", "Outlet C", authority=30,
                published=now - timedelta(hours=2),
                snippet="A bakery won a regional award."),
        # On-topic for prior auth.
        Article("Prior Authorization", "Insurer overhauls prior authorization rules",
                "https://news.example.com/pa", "Outlet D", authority=30,
                published=now - timedelta(hours=1),
                snippet="A major insurer announced prior authorization reform."),
    ]


def self_test() -> bool:
    """Exercise the pipeline offline and assert each documented behavior.
    Returns True on success; raises AssertionError on any failure."""
    logging.basicConfig(level=logging.WARNING)
    config = _fixture_config()
    store = MemorySeenStore()

    kept = process(_fixtures(), config, store, client=None)
    titles = {a.title for a in kept}

    # recency: the 200h-old item is gone
    assert not any("Old No Surprises Act" in t for t in titles), "recency filter failed"

    # near-duplicate collapse: the two 'Court rules...' copies fold to one,
    # and the survivor is the authoritative CourtListener copy.
    court = [a for a in kept if "court rule" in a.title.lower()]
    assert len(court) == 1, f"near-duplicate collapse failed: {[a.title for a in court]}"
    assert court[0].source == "CourtListener", "collapse kept the wrong (less authoritative) copy"

    # prefilter/scoring: the off-topic bakery item is gone
    assert not any("bakery" in t.lower() for t in titles), "prefilter/scoring kept an off-topic item"

    # the genuine prior-auth item survives
    assert any("prior authorization" in t.lower() for t in titles), "dropped a relevant item"

    # render: payload + html are well-formed and non-empty
    payload = render.build_payload(kept)
    assert payload["item_count"] == len(kept)
    html = render.render_html(payload)
    assert "Policy Signal" in html and payload["item_count"] > 0

    # cross-day dedup: marking the kept items means a second run keeps nothing new
    # (including the same story arriving via a different outlet's URL)
    mark_delivered(kept, store)
    second = process(_fixtures(), config, store, client=None)
    assert second == [], "cross-day dedup failed: items repeated after being marked seen"

    print(f"self-test OK — {len(kept)} items kept, dedup/recency/collapse/prefilter/render verified")
    return True
