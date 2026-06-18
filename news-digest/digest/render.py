"""Render the scored, topic-grouped results into HTML, plain text, and JSON.

The JSON form is the contract with the companion app: it loads the same payload
the email is built from. Keep the three renderers in sync — they all consume the
grouped structure produced by `group_by_topic`.
"""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone

from .models import Article


def group_by_topic(articles: list[Article]) -> dict[str, list[Article]]:
    """Group articles by topic, each list sorted by score desc then recency."""
    groups: dict[str, list[Article]] = {}
    for a in articles:
        groups.setdefault(a.topic, []).append(a)
    for items in groups.values():
        items.sort(
            key=lambda x: (x.score or 0.0, x.published or datetime.min.replace(tzinfo=timezone.utc)),
            reverse=True,
        )
    return groups


def _fmt_date(dt: datetime | None) -> str:
    return dt.strftime("%b %d, %Y") if dt else "undated"


def build_payload(articles: list[Article], generated_at: datetime | None = None) -> dict:
    """The structured digest consumed by both the email renderer and the companion."""
    generated_at = generated_at or datetime.now(timezone.utc)
    groups = group_by_topic(articles)
    return {
        "generated_at": generated_at.isoformat(),
        "topic_count": len(groups),
        "item_count": sum(len(v) for v in groups.values()),
        "topics": [
            {
                "name": topic,
                "items": [
                    {
                        "title": a.title,
                        "url": a.url,
                        "source": a.source,
                        "published": a.published.isoformat() if a.published else None,
                        "published_display": _fmt_date(a.published),
                        "score": a.score,
                        "reason": a.reason,
                        "summary": a.summary,
                    }
                    for a in items
                ],
            }
            for topic, items in groups.items()
        ],
    }


def render_json(payload: dict) -> str:
    return json.dumps(payload, indent=2, ensure_ascii=False)


def render_text(payload: dict) -> str:
    lines = [
        "POLICY SIGNAL — DAILY DIGEST",
        f"Generated {payload['generated_at']}",
        f"{payload['item_count']} items across {payload['topic_count']} topics",
        "=" * 60,
        "",
    ]
    if payload["item_count"] == 0:
        lines.append("No new relevant items today.")
    for topic in payload["topics"]:
        lines.append(f"## {topic['name']}  ({len(topic['items'])})")
        lines.append("")
        for it in topic["items"]:
            score = f"{it['score']:.2f}" if it["score"] is not None else "—"
            lines.append(f"  • {it['title']}")
            lines.append(f"    {it['source']} · {it['published_display']} · score {score}")
            if it["summary"]:
                lines.append(f"    {it['summary']}")
            lines.append(f"    {it['url']}")
            lines.append("")
    return "\n".join(lines)


_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a1a;line-height:1.5;max-width:680px;margin:0 auto;padding:24px}
h1{font-size:20px;margin:0 0 4px}
.meta{color:#666;font-size:13px;margin-bottom:24px}
h2{font-size:16px;border-bottom:2px solid #eee;padding-bottom:6px;margin:28px 0 12px}
.item{margin:0 0 18px;padding-left:12px;border-left:3px solid #e6e6e6}
.item a{color:#0b5cad;text-decoration:none;font-weight:600;font-size:15px}
.src{color:#777;font-size:12px;margin:2px 0}
.sum{font-size:14px;margin:4px 0;color:#333}
.score{display:inline-block;background:#eef4fb;color:#0b5cad;border-radius:10px;padding:0 8px;font-size:11px;font-weight:600}
.empty{color:#777;font-style:italic}
.foot{margin-top:32px;color:#999;font-size:12px;border-top:1px solid #eee;padding-top:12px}
"""


def render_html(payload: dict) -> str:
    e = html.escape
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>Policy Signal — Daily Digest</h1>",
        f"<div class='meta'>{e(payload['generated_at'])} · "
        f"{payload['item_count']} items · {payload['topic_count']} topics</div>",
    ]
    if payload["item_count"] == 0:
        parts.append("<p class='empty'>No new relevant items today.</p>")
    for topic in payload["topics"]:
        parts.append(f"<h2>{e(topic['name'])} <span class='score'>{len(topic['items'])}</span></h2>")
        for it in topic["items"]:
            score = f"{it['score']:.2f}" if it["score"] is not None else "—"
            parts.append("<div class='item'>")
            parts.append(f"<a href='{e(it['url'])}'>{e(it['title'])}</a>")
            parts.append(
                f"<div class='src'>{e(it['source'])} · {e(it['published_display'])} "
                f"· <span class='score'>{score}</span></div>"
            )
            if it["summary"]:
                parts.append(f"<div class='sum'>{e(it['summary'])}</div>")
            parts.append("</div>")
    parts.append("<div class='foot'>Policy Signal · public sources only · authoritative-first</div>")
    parts.append("</body></html>")
    return "".join(parts)
