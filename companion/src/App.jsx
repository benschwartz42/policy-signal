import React, { useEffect, useMemo, useState } from "react";

// The companion is preview/export only: it loads the structured digest the
// backend publishes (digests/latest.json) and lets you browse, filter, and
// export it. No live search, no API keys in the browser.

const DIGEST_URL = import.meta.env.BASE_URL + "digests/latest.json";

function scoreLabel(s) {
  return typeof s === "number" ? s.toFixed(2) : "—";
}

function buildMarkdown(payload, filtered) {
  const lines = [
    `# Policy Signal — Daily Digest`,
    `_${payload.generated_at} · ${filtered.reduce((n, t) => n + t.items.length, 0)} items_`,
    "",
  ];
  for (const topic of filtered) {
    if (!topic.items.length) continue;
    lines.push(`## ${topic.name}`, "");
    for (const it of topic.items) {
      lines.push(`- **[${it.title}](${it.url})** — ${it.source} · ${it.published_display} · score ${scoreLabel(it.score)}`);
      if (it.summary) lines.push(`  ${it.summary}`);
    }
    lines.push("");
  }
  return lines.join("\n");
}

function download(filename, text, type) {
  const blob = new Blob([text], { type });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

export default function App() {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState(null);
  const [topicFilter, setTopicFilter] = useState("all");
  const [minScore, setMinScore] = useState(0);
  const [query, setQuery] = useState("");

  useEffect(() => {
    fetch(DIGEST_URL)
      .then((r) => {
        if (!r.ok) throw new Error(`Could not load digest (${r.status}). Has the backend published one yet?`);
        return r.json();
      })
      .then(setPayload)
      .catch((e) => setError(e.message));
  }, []);

  const filtered = useMemo(() => {
    if (!payload) return [];
    const q = query.trim().toLowerCase();
    return payload.topics
      .filter((t) => topicFilter === "all" || t.name === topicFilter)
      .map((t) => ({
        ...t,
        items: t.items.filter((it) => {
          if ((it.score ?? 0) < minScore) return false;
          if (!q) return true;
          return (
            it.title.toLowerCase().includes(q) ||
            (it.summary || "").toLowerCase().includes(q) ||
            (it.source || "").toLowerCase().includes(q)
          );
        }),
      }))
      .filter((t) => t.items.length > 0);
  }, [payload, topicFilter, minScore, query]);

  if (error) {
    return (
      <div className="wrap">
        <header><h1>Policy Signal</h1></header>
        <div className="error">{error}</div>
      </div>
    );
  }
  if (!payload) {
    return (
      <div className="wrap">
        <header><h1>Policy Signal</h1></header>
        <div className="loading">Loading digest…</div>
      </div>
    );
  }

  const totalShown = filtered.reduce((n, t) => n + t.items.length, 0);

  return (
    <div className="wrap">
      <header>
        <h1>Policy Signal — Daily Digest</h1>
        <div className="meta">
          Generated {new Date(payload.generated_at).toLocaleString()} · {payload.item_count} items · {payload.topic_count} topics
        </div>
      </header>

      <div className="controls">
        <label>
          Topic
          <select value={topicFilter} onChange={(e) => setTopicFilter(e.target.value)}>
            <option value="all">All topics</option>
            {payload.topics.map((t) => (
              <option key={t.name} value={t.name}>{t.name}</option>
            ))}
          </select>
        </label>
        <label>
          Min score: {minScore.toFixed(2)}
          <input type="range" min="0" max="1" step="0.05" value={minScore}
                 onChange={(e) => setMinScore(parseFloat(e.target.value))} />
        </label>
        <label className="grow">
          Search
          <input type="text" placeholder="title, summary, source…" value={query}
                 onChange={(e) => setQuery(e.target.value)} />
        </label>
        <div className="actions">
          <button className="secondary" onClick={() => navigator.clipboard.writeText(buildMarkdown(payload, filtered))}>
            Copy Markdown
          </button>
          <button onClick={() => download(`policy-signal-${payload.generated_at.slice(0, 10)}.md`,
                                          buildMarkdown(payload, filtered), "text/markdown")}>
            Download
          </button>
        </div>
      </div>

      {totalShown === 0 ? (
        <div className="empty">No items match the current filters.</div>
      ) : (
        filtered.map((topic) => (
          <section className="topic" key={topic.name}>
            <h2>{topic.name} <span className="count">{topic.items.length}</span></h2>
            {topic.items.map((it, i) => (
              <div className="item" key={it.url + i}>
                <a href={it.url} target="_blank" rel="noopener noreferrer">{it.title}</a>
                <div className="src">{it.source} · {it.published_display} · score {scoreLabel(it.score)}</div>
                {it.summary && <p className="sum">{it.summary}</p>}
                {it.reason && <div className="reason">{it.reason}</div>}
              </div>
            ))}
          </section>
        ))
      )}

      <div className="foot">Policy Signal · public sources only · authoritative-first · preview &amp; export</div>
    </div>
  );
}
