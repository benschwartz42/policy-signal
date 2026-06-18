import React, { useEffect, useState } from "react";
import ConfigPage from "./ConfigPage.jsx";

// The companion has two tabs: a read-only view of the published digest, and a
// Configure view that edits the backend config via the GitHub API.

const DIGEST_URL = import.meta.env.BASE_URL + "digests/latest.json";

function scoreLabel(s) {
  return typeof s === "number" ? s.toFixed(2) : "—";
}

// Mirrors render._score_palette in the backend: [text, background].
function scorePalette(s) {
  if (s == null) return ["#5b6b80", "#eef1f5"];
  if (s >= 0.85) return ["#137333", "#e6f4ea"];
  if (s >= 0.65) return ["#1457b8", "#e8f0fc"];
  return ["#9a6700", "#fef7e0"];
}

function Shell({ header, body }) {
  return (
    <div className="ps-card">
      <div className="ps-header">
        <div className="ps-title">Policy&nbsp;Signal</div>
        {header}
      </div>
      <div className="ps-body">{body}</div>
      <div className="ps-foot">Policy Signal · public sources only · authoritative-first</div>
    </div>
  );
}

function DigestView() {
  const [payload, setPayload] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    fetch(DIGEST_URL)
      .then((r) => {
        if (!r.ok) throw new Error(`Could not load digest (${r.status}). Has the backend published one yet?`);
        return r.json();
      })
      .then(setPayload)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return <Shell body={<div className="ps-note error">{error}</div>} />;
  }
  if (!payload) {
    return <Shell body={<div className="ps-note">Loading digest…</div>} />;
  }

  const date = new Date(payload.generated_at).toLocaleDateString(undefined, {
    weekday: "long", month: "short", day: "numeric", year: "numeric",
  });
  const itemsWord = payload.item_count === 1 ? "item" : "items";

  const header = (
    <>
      <div className="ps-sub">Daily regulatory &amp; legal digest · {date}</div>
      <div className="ps-sub2">{payload.item_count} {itemsWord} · {payload.topic_count} topics</div>
    </>
  );

  const body = payload.item_count === 0 ? (
    <div className="ps-note">No new items today — all clear.</div>
  ) : (
    payload.topics.map((topic) => (
      <section className="ps-topic" key={topic.name}>
        <div className="ps-topic-head">
          <span className="ps-topic-name">{topic.name}</span>
          <span className="ps-badge">{topic.items.length}</span>
        </div>
        {topic.items.map((it, i) => {
          const [fg, bg] = scorePalette(it.score);
          return (
            <div className="ps-item" style={{ borderLeftColor: fg }} key={it.url + i}>
              <a className="ps-item-title" href={it.url} target="_blank" rel="noopener noreferrer">{it.title}</a>
              <div className="ps-meta">
                <span className="ps-srcchip">{it.source}</span>
                <span className="ps-dot">·</span>{it.published_display}<span className="ps-dot">·</span>
                <span className="ps-score" style={{ color: fg, background: bg }}>{scoreLabel(it.score)}</span>
              </div>
              {it.summary && <div className="ps-summary">{it.summary}</div>}
              {Array.isArray(it.also) && it.also.length > 0 && (
                <div className="ps-also">
                  Also covered by:{" "}
                  {it.also.map((x, j) => (
                    <React.Fragment key={x.url + j}>
                      {j > 0 && " · "}
                      <a href={x.url} target="_blank" rel="noopener noreferrer">{x.source}</a>
                    </React.Fragment>
                  ))}
                </div>
              )}
            </div>
          );
        })}
      </section>
    ))
  );

  return <Shell header={header} body={body} />;
}

export default function App() {
  const [tab, setTab] = useState("digest");
  return (
    <>
      <div className="wrap tabbar">
        <nav className="tabs">
          <button className={tab === "digest" ? "tab active" : "tab"} onClick={() => setTab("digest")}>Digest</button>
          <button className={tab === "config" ? "tab active" : "tab"} onClick={() => setTab("config")}>Configure</button>
        </nav>
      </div>
      {tab === "digest" ? <DigestView /> : <div className="wrap"><div className="panel"><ConfigPage /></div></div>}
    </>
  );
}
