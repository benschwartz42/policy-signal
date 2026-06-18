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
                        "also": a.also,
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
            for x in it.get("also") or []:
                lines.append(f"    also: {x['source']} — {x['url']}")
            lines.append("")
    return "\n".join(lines)


_ACCENT = "#0f2a4a"


def _score_palette(score) -> tuple[str, str]:
    """(text, background) colors for a score chip / accent — green/blue/amber."""
    if score is None:
        return ("#5b6b80", "#eef1f5")
    if score >= 0.85:
        return ("#137333", "#e6f4ea")
    if score >= 0.65:
        return ("#1457b8", "#e8f0fc")
    return ("#9a6700", "#fef7e0")


def _date_label(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%A, %b %-d, %Y")
    except (ValueError, TypeError):
        try:
            return datetime.fromisoformat(iso).strftime("%A, %b %d, %Y")
        except (ValueError, TypeError):
            return (iso or "")[:10]


def render_html(payload: dict, companion_url: str | None = None) -> str:
    """Modern, email-client-safe HTML digest: centered 600px card, table layout,
    inline styles (so Gmail/Outlook/Apple Mail render it consistently)."""
    e = html.escape
    n = payload["item_count"]
    items_word = "item" if n == 1 else "items"
    date_label = _date_label(payload["generated_at"])
    font = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"

    p: list[str] = []
    p.append(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="light only"></head>'
        '<body style="margin:0;padding:0;background:#eef1f5;-webkit-text-size-adjust:100%;">'
    )
    # Hidden preheader (inbox preview text).
    p.append(
        '<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:#eef1f5;">'
        f'{n} {items_word} across {payload["topic_count"]} topics — {e(date_label)}</div>'
    )
    p.append(
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#eef1f5;"><tr><td align="center" style="padding:24px 12px;">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:600px;max-width:600px;background:#ffffff;border-radius:14px;'
        f'overflow:hidden;box-shadow:0 1px 4px rgba(15,42,74,0.10);font-family:{font};">'
    )
    # Header
    p.append(
        f'<tr><td style="background:{_ACCENT};padding:26px 32px;">'
        '<div style="color:#ffffff;font-size:21px;font-weight:700;letter-spacing:.2px;">Policy&nbsp;Signal</div>'
        f'<div style="color:#9db6d6;font-size:13px;margin-top:6px;">Daily regulatory &amp; legal digest&nbsp;·&nbsp;{e(date_label)}</div>'
        f'<div style="color:#cfe0f2;font-size:13px;margin-top:2px;">{n} {items_word}&nbsp;·&nbsp;{payload["topic_count"]} topics</div>'
        '</td></tr>'
    )
    # Body
    p.append('<tr><td style="padding:18px 24px 6px;">')
    if n == 0:
        p.append('<p style="color:#6b7a90;font-size:15px;text-align:center;padding:30px 10px;margin:0;">'
                 'No new items today — all clear. 👍</p>')
    for topic in payload["topics"]:
        count = len(topic["items"])
        p.append(
            '<div style="margin:22px 0 12px;border-bottom:2px solid #eef1f5;padding-bottom:7px;">'
            f'<span style="text-transform:uppercase;letter-spacing:.6px;font-size:13px;font-weight:700;color:{_ACCENT};">{e(topic["name"])}</span>'
            f'<span style="background:#eef1f5;color:#5b6b80;border-radius:9px;padding:2px 8px;font-size:11px;font-weight:700;margin-left:8px;">{count}</span>'
            '</div>'
        )
        for it in topic["items"]:
            fg, bg = _score_palette(it["score"])
            score = f'{it["score"]:.2f}' if it["score"] is not None else "—"
            summary = e(it["summary"]) if it["summary"] else ""
            p.append(
                '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
                'style="margin:0 0 14px;border:1px solid #e6eaf1;border-radius:10px;">'
                f'<tr><td style="padding:15px 17px;border-left:4px solid {fg};">'
                f'<a href="{e(it["url"])}" style="color:#0f2a4a;font-size:16px;font-weight:700;'
                f'line-height:1.35;text-decoration:none;">{e(it["title"])}</a>'
                '<div style="margin:8px 0 9px;font-size:12px;color:#6b7a90;">'
                f'<span style="background:#f1f4f8;border-radius:6px;padding:2px 7px;">{e(it["source"])}</span>'
                f'&nbsp;·&nbsp;{e(it["published_display"])}&nbsp;·&nbsp;'
                f'<span style="background:{bg};color:{fg};border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700;">{score}</span>'
                '</div>'
                f'<div style="font-size:14px;line-height:1.6;color:#36424f;">{summary}</div>'
            )
            also = it.get("also") or []
            if also:
                links = " &nbsp;·&nbsp; ".join(
                    f'<a href="{e(x["url"])}" style="color:#1457b8;text-decoration:none;">{e(x["source"])}</a>'
                    for x in also
                )
                p.append(
                    '<div style="margin-top:10px;padding-top:9px;border-top:1px solid #eef1f5;'
                    'font-size:12px;color:#8a98aa;">Also covered by:&nbsp; ' + links + '</div>'
                )
            p.append('</td></tr></table>')
    p.append('</td></tr>')
    # Footer
    link = ""
    if companion_url:
        link = (f'<br><a href="{e(companion_url)}" style="color:#1457b8;text-decoration:none;font-weight:600;">'
                'View the full digest online →</a>')
    p.append(
        '<tr><td style="background:#f7f9fc;border-top:1px solid #e6eaf1;padding:16px 32px;">'
        f'<div style="font-size:12px;color:#90a0b3;line-height:1.7;font-family:{font};">'
        f'Policy Signal · public sources only · authoritative-first{link}</div>'
        '</td></tr>'
    )
    p.append('</table></td></tr></table></body></html>')
    return "".join(p)
