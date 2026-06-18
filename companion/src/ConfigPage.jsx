import React, { useEffect, useState } from "react";
import yaml from "js-yaml";
import { getConfigFile, putConfigFile, dispatchWorkflow, whoami, getSeenCount, resetSeenStore, REPO } from "./github.js";

const KNOWN_SOURCES = ["federal_register", "courtlistener", "google_news", "regulations_gov", "rss"];
const TOKEN_KEY = "ps_gh_token";

// Deep-ish clone for our plain config object (no functions/dates).
const clone = (o) => JSON.parse(JSON.stringify(o));

export default function ConfigPage() {
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_KEY) || "");
  const [cfg, setCfg] = useState(null);
  const [sha, setSha] = useState(null);
  const [status, setStatus] = useState(null); // {kind, msg}
  const [busy, setBusy] = useState(false);
  const [seenCount, setSeenCount] = useState(null);
  const [testAddr, setTestAddr] = useState("ben@zdkinnovations.com");

  const say = (kind, msg) => setStatus({ kind, msg });

  async function load() {
    setBusy(true);
    say("info", "Loading config…");
    try {
      if (token) {
        // Best-effort greeting. Fine-grained tokens often can't read /user
        // (needs account permissions we don't require) — don't let it block.
        try {
          const me = await whoami(token);
          say("info", `Authenticated as ${me.login}. Loading config…`);
        } catch {
          say("info", "Loading config…");
        }
      }
      const { text, sha } = await getConfigFile(token);
      const parsed = yaml.load(text) || {};
      parsed.settings = parsed.settings || {};
      parsed.delivery = parsed.delivery || {};
      parsed.delivery.recipients = parsed.delivery.recipients || [];
      parsed.settings.sources = parsed.settings.sources || [];
      parsed.topics = parsed.topics || [];
      // Normalize feeds to {url, name} objects (YAML may have bare URL strings).
      parsed.feeds = (parsed.feeds || []).map((f) =>
        typeof f === "string" ? { url: f, name: "" } : { url: f.url || "", name: f.name || "" });
      setCfg(parsed);
      setSha(sha);
      say("ok", "Config loaded.");
      try { setSeenCount(await getSeenCount(token)); } catch { setSeenCount(null); }
    } catch (e) {
      say("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  async function resetSent() {
    if (!token) return say("error", "Paste a GitHub token first (needs Contents: Read & Write).");
    const ok = window.confirm(
      "Reset sent history?\n\nThis clears the record of what's already been emailed. " +
      "The next run will re-surface and re-send the recent backlog to ALL configured " +
      "recipients. Use this for a new recipient or to rebuild a full digest."
    );
    if (!ok) return;
    setBusy(true);
    say("info", "Resetting sent history…");
    try {
      const r = await resetSeenStore(token);
      setSeenCount(0);
      say("ok", r.alreadyEmpty ? "Sent history was already empty." : "Sent history reset — the next run will re-send the recent backlog.");
    } catch (e) {
      say("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  // Auto-load (read-only) on first mount so the current config is visible.
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  function saveToken() {
    localStorage.setItem(TOKEN_KEY, token);
    say("ok", "Token saved in this browser.");
  }

  function update(mut) {
    const next = clone(cfg);
    mut(next);
    setCfg(next);
  }

  async function save() {
    if (!token) return say("error", "Paste a GitHub token first (needs Contents: Read & Write).");
    setBusy(true);
    say("info", "Saving…");
    try {
      const out = clone(cfg);
      // Drop blank feeds so a half-typed row can't break the pipeline config.
      out.feeds = (out.feeds || []).filter((f) => f.url && f.url.trim());
      const text = yaml.dump(out, { lineWidth: 100, noRefs: true });
      const res = await putConfigFile(token, text, sha, "chore(config): update via Configure page");
      setSha(res.content.sha);
      say("ok", "Saved and committed. The next scheduled run will use it — or hit ‘Run now’.");
    } catch (e) {
      say("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  async function runNow() {
    if (!token) return say("error", "Paste a GitHub token first (needs Actions: Read & Write).");
    setBusy(true);
    say("info", "Triggering a run…");
    try {
      await dispatchWorkflow(token);
      say("ok", "Run triggered — emails the configured recipients (not you, unless you're listed). Check the Actions tab.");
    } catch (e) {
      say("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  async function sendTest() {
    if (!token) return say("error", "Paste a GitHub token first (needs Actions: Read & Write).");
    if (!testAddr.trim()) return say("error", "Enter an address to send the test to.");
    setBusy(true);
    say("info", `Sending a test digest to ${testAddr}…`);
    try {
      await dispatchWorkflow(token, { test_recipients: testAddr.trim() });
      say("ok", `Test send triggered to ${testAddr}. Fresh window; the team is not emailed and saved state is untouched.`);
    } catch (e) {
      say("error", e.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="cfg">
      <section className="auth">
        <label>
          GitHub token (fine-grained; this repo; Contents R/W, Actions R/W)
          <div className="row">
            <input type="password" value={token} placeholder="github_pat_…"
                   onChange={(e) => setToken(e.target.value)} />
            <button className="secondary" onClick={saveToken}>Save token</button>
            <button className="secondary" onClick={load} disabled={busy}>Reload</button>
          </div>
        </label>
        <p className="hint">
          Stored only in this browser. Edits commit to <code>{REPO.owner}/{REPO.name}</code>’s{" "}
          <code>{REPO.configPath}</code>. Reading works without a token; saving needs one.
        </p>
      </section>

      {status && <div className={`banner ${status.kind}`}>{status.msg}</div>}

      {cfg && (
        <>
          <section>
            <h3>Delivery</h3>
            <label className="field">Sender
              <input type="text" value={cfg.delivery.sender || ""}
                     onChange={(e) => update((c) => { c.delivery.sender = e.target.value; })} />
            </label>
            <label className="field">Subject prefix
              <input type="text" value={cfg.delivery.subject_prefix || ""}
                     onChange={(e) => update((c) => { c.delivery.subject_prefix = e.target.value; })} />
            </label>
            <div className="field">
              <span>Recipients</span>
              {cfg.delivery.recipients.map((r, i) => (
                <div className="row" key={i}>
                  <input type="email" value={r}
                         onChange={(e) => update((c) => { c.delivery.recipients[i] = e.target.value; })} />
                  <button className="danger" onClick={() => update((c) => { c.delivery.recipients.splice(i, 1); })}>Remove</button>
                </div>
              ))}
              <button className="secondary" onClick={() => update((c) => { c.delivery.recipients.push(""); })}>+ Add recipient</button>
            </div>
          </section>

          <section>
            <h3>Settings</h3>
            <div className="grid">
              <label className="field">Lookback (hours)
                <input type="number" value={cfg.settings.lookback_hours ?? 720}
                       onChange={(e) => update((c) => { c.settings.lookback_hours = Number(e.target.value); })} />
              </label>
              <label className="field">Min relevance: {Number(cfg.settings.min_relevance ?? 0.5).toFixed(2)}
                <input type="range" min="0" max="1" step="0.05" value={cfg.settings.min_relevance ?? 0.5}
                       onChange={(e) => update((c) => { c.settings.min_relevance = Number(e.target.value); })} />
              </label>
              <label className="field">Max items / topic
                <input type="number" value={cfg.settings.max_items_per_topic ?? 12}
                       onChange={(e) => update((c) => { c.settings.max_items_per_topic = Number(e.target.value); })} />
              </label>
              <label className="field">Model
                <input type="text" value={cfg.settings.model || ""}
                       onChange={(e) => update((c) => { c.settings.model = e.target.value; })} />
              </label>
              <label className="field">Combine similar articles
                <select value={cfg.settings.combine_tolerance || "balanced"}
                        onChange={(e) => update((c) => { c.settings.combine_tolerance = e.target.value; })}>
                  <option value="strict">Strict — only the exact same item</option>
                  <option value="balanced">Balanced — same development</option>
                  <option value="broad">Broad — same story &amp; reactions</option>
                </select>
              </label>
            </div>
            <div className="field">
              <span>Sources</span>
              <div className="sources">
                {KNOWN_SOURCES.map((s) => (
                  <label key={s} className="chk">
                    <input type="checkbox" checked={cfg.settings.sources.includes(s)}
                           onChange={(e) => update((c) => {
                             if (e.target.checked) { if (!c.settings.sources.includes(s)) c.settings.sources.push(s); }
                             else { c.settings.sources = c.settings.sources.filter((x) => x !== s); }
                           })} />
                    {s}
                  </label>
                ))}
              </div>
            </div>
          </section>

          <section>
            <h3>Custom sources (any site or feed)</h3>
            <p className="hint" style={{ margin: "0 0 10px" }}>
              Add any URL — an RSS/Atom feed, or just a website's news page. Feeds are used
              directly; for a plain page we look for a hidden feed and otherwise read the
              page and let the model pick out the article links. Items are scanned against
              every topic and scored like any other source. Label is optional.
            </p>
            {(cfg.feeds || []).map((f, fi) => (
              <div className="row" key={fi}>
                <input type="text" placeholder="Label (optional)" value={f.name || ""}
                       style={{ flex: "0 0 160px" }}
                       onChange={(e) => update((c) => { c.feeds[fi].name = e.target.value; })} />
                <input type="url" placeholder="https://example.com/feed.xml" value={f.url || ""}
                       onChange={(e) => update((c) => { c.feeds[fi].url = e.target.value; })} />
                <button className="danger" onClick={() => update((c) => { c.feeds.splice(fi, 1); })}>Remove</button>
              </div>
            ))}
            <button className="secondary" onClick={() => update((c) => {
              c.feeds = c.feeds || []; c.feeds.push({ name: "", url: "" });
            })}>+ Add feed</button>
          </section>

          <section>
            <h3>Topics</h3>
            {cfg.topics.map((t, ti) => (
              <div className="topic-card" key={ti}>
                <div className="row">
                  <input className="topic-name" type="text" value={t.name || ""} placeholder="Topic name"
                         onChange={(e) => update((c) => { c.topics[ti].name = e.target.value; })} />
                  <button className="secondary" title="Move up" disabled={ti === 0}
                          onClick={() => update((c) => { const a = c.topics; [a[ti - 1], a[ti]] = [a[ti], a[ti - 1]]; })}>↑</button>
                  <button className="secondary" title="Move down" disabled={ti === cfg.topics.length - 1}
                          onClick={() => update((c) => { const a = c.topics; [a[ti + 1], a[ti]] = [a[ti], a[ti + 1]]; })}>↓</button>
                  <button className="danger" onClick={() => update((c) => { c.topics.splice(ti, 1); })}>Delete topic</button>
                </div>
                <label className="field">Description (the relevance rubric the LLM judges against)
                  <textarea rows={4} value={t.description || ""}
                            onChange={(e) => update((c) => { c.topics[ti].description = e.target.value; })} />
                </label>
                <div className="field">
                  <span>Keywords</span>
                  <div className="kw-wrap">
                    {(t.keywords || []).map((k, ki) => (
                      <span className="kw" key={ki}>
                        <input type="text" value={k}
                               onChange={(e) => update((c) => { c.topics[ti].keywords[ki] = e.target.value; })} />
                        <button className="kw-x" onClick={() => update((c) => { c.topics[ti].keywords.splice(ki, 1); })}>×</button>
                      </span>
                    ))}
                    <button className="secondary" onClick={() => update((c) => {
                      c.topics[ti].keywords = c.topics[ti].keywords || []; c.topics[ti].keywords.push("");
                    })}>+ keyword</button>
                  </div>
                </div>
              </div>
            ))}
            <button className="secondary" onClick={() => update((c) => {
              c.topics.push({ name: "", description: "", keywords: [] });
            })}>+ Add topic</button>
          </section>

          <section>
            <h3>Preview / test send</h3>
            <p className="hint" style={{ margin: "0 0 10px" }}>
              Sends a one-off digest to the address below using a fresh window. The
              configured recipients are NOT emailed and saved dedup is untouched.
            </p>
            <div className="row">
              <input type="email" value={testAddr} placeholder="you@example.com"
                     onChange={(e) => setTestAddr(e.target.value)} />
              <button className="secondary" onClick={sendTest} disabled={busy}>Send test</button>
            </div>
          </section>

          <section>
            <h3>Sent history</h3>
            <p className="hint" style={{ margin: "0 0 10px" }}>
              {seenCount === null
                ? "Tracks what's already been emailed so it isn't sent twice."
                : `${seenCount} dedup ${seenCount === 1 ? "entry" : "entries"} remembered (≈${Math.ceil(seenCount / 2)} items). These won't be re-sent.`}
            </p>
            <button className="danger" onClick={resetSent} disabled={busy}>Reset sent history</button>
          </section>

          <div className="save-bar">
            <button onClick={save} disabled={busy}>Save config</button>
            <button className="secondary" onClick={runNow} disabled={busy} title="Emails the configured recipients now">Run now (email recipients)</button>
          </div>
        </>
      )}
    </div>
  );
}
