import React, { useEffect, useState } from "react";
import ConfigPage from "./ConfigPage.jsx";

// The companion has two tabs: a read-only view of the published digest, and a
// Configure view that edits the backend config via the GitHub API.

const DIGEST_URL = import.meta.env.BASE_URL + "digests/latest.json";

function scoreLabel(s) {
  return typeof s === "number" ? s.toFixed(2) : "—";
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

  return (
    <div className="wrap">
      <header>
        <h1>Policy Signal — Daily Digest</h1>
        <div className="meta">
          Generated {new Date(payload.generated_at).toLocaleString()} · {payload.item_count} items · {payload.topic_count} topics
        </div>
      </header>

      {payload.item_count === 0 ? (
        <div className="empty">
          The latest digest is empty — the most recent run found no new items.
          New items appear here after a run that finds them.
        </div>
      ) : (
        payload.topics.map((topic) => (
          <section className="topic" key={topic.name}>
            <h2>{topic.name} <span className="count">{topic.items.length}</span></h2>
            {topic.items.map((it, i) => (
              <div className="item" key={it.url + i}>
                <a href={it.url} target="_blank" rel="noopener noreferrer">{it.title}</a>
                <div className="src">{it.source} · {it.published_display} · score {scoreLabel(it.score)}</div>
                {it.summary && <p className="sum">{it.summary}</p>}
                {Array.isArray(it.also) && it.also.length > 0 && (
                  <div className="also">
                    Also covered by:{" "}
                    {it.also.map((x, j) => (
                      <React.Fragment key={x.url + j}>
                        {j > 0 && " · "}
                        <a href={x.url} target="_blank" rel="noopener noreferrer">{x.source}</a>
                      </React.Fragment>
                    ))}
                  </div>
                )}
                {it.reason && <div className="reason">{it.reason}</div>}
              </div>
            ))}
          </section>
        ))
      )}

      <div className="foot">Policy Signal · public sources only · authoritative-first</div>
    </div>
  );
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
      {tab === "digest" ? <DigestView /> : <div className="wrap"><ConfigPage /></div>}
    </>
  );
}
