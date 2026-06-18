# Policy Signal

A topic-monitoring service for a small healthcare revenue-cycle team. It watches
regulatory, legal, and press sources for configured topics (flagship: the federal
**No Surprises Act**), deduplicates, has an LLM judge relevance and summarize, and
emails a topic-grouped daily digest.

**Design stance: authoritative-first.** For policy topics the timely signal lives
in rulemaking and litigation, not the press, so government and court sources are
primary and general news is a secondary net.

**Data note:** reads only public sources. No claim-level data, transcripts, or PHI.

## Two halves

| Part | What it is | Runtime |
|------|------------|---------|
| [`news-digest/`](news-digest/) | Python pipeline that produces and emails the digest | GitHub Actions cron (daily) |
| [`companion/`](companion/) | Static React app to browse/filter/export the latest digest | GitHub Pages |

The pipeline emits a structured `digest.json`; the daily workflow commits it into
the companion's published data, so the companion always shows the latest run.
**No AWS, no servers** — durable dedup is a JSON file the workflow commits back.

## Architecture

```
ingest -> recency filter -> dedup (durable seen-store) -> near-dup collapse
       -> keyword prefilter -> LLM score -> render (html/text/json) -> deliver (email)
```

- **ingest** — query each enabled source for each topic; a failing source is
  logged and skipped, never fatal.
- **recency** — drop items older than `lookback_hours` (undated items kept).
- **dedup** — durable cross-day seen-store (JSON, 30-day TTL) + near-duplicate
  collapse (same wire story across outlets folds to the most authoritative copy).
- **prefilter** — cheap keyword cut before any LLM spend.
- **LLM score** — Claude rates each survivor against the topic's plain-English
  `description` and returns `{relevant, score, reason, summary}`. Offline
  deterministic stub used when no `ANTHROPIC_API_KEY` is set.
- **render** — topic-grouped HTML + text + JSON.
- **deliver** — Resend HTTP API primary, SMTP fallback.

Topics are **config, not code** ([`news-digest/config.example.yaml`](news-digest/config.example.yaml)).
The `description` field is the relevance rubric the model judges against.

## Sources

| id | key needed | cost |
|----|------------|------|
| `federal_register` | none | free |
| `google_news` (RSS) | none | free |
| `rss` (generic, per-topic `extra_rss`) | none | free |
| `regulations_gov` | `REGS_GOV_API_KEY` | free key |
| `courtlistener` | `COURTLISTENER_TOKEN` (optional) | free |

> Bing Search/News API was retired 2025-08-11 and is intentionally absent.

## Run locally

```bash
cd news-digest
python3 -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

python run.py --self-test                      # offline, no keys, asserts behavior
cp config.example.yaml config.yaml             # then edit topics/recipients
python run.py --config config.yaml --dry-run   # live sources, writes digest.html, no email
python run.py --config config.yaml             # live run + email + seen-store update
```

Companion:

```bash
cd companion
npm install
npm run dev        # http://localhost:5173 — reads public/digests/latest.json
```

## Deploy

This repo is its own infrastructure — two GitHub Actions workflows:

1. **[`daily-digest.yml`](.github/workflows/daily-digest.yml)** — runs the pipeline
   on a daily cron, emails the team, and commits the updated seen-store +
   published digest back to the repo.
2. **[`deploy-companion.yml`](.github/workflows/deploy-companion.yml)** — builds
   the companion and deploys it to GitHub Pages whenever the companion or its
   published digests change.

### One-time setup

1. **Secrets** (repo → Settings → Secrets and variables → Actions):
   `ANTHROPIC_API_KEY`, `RESEND_API_KEY`, and optionally `REGS_GOV_API_KEY` /
   `COURTLISTENER_TOKEN`.
2. **Email sender** — verify your sending domain in Resend and set
   `delivery.sender` in the config to an address on that domain.
3. **Recipients & topics** — commit a real `config.yaml` (or template it from a
   secret in the workflow; the example is copied by default).
4. **Pages** — repo → Settings → Pages → Source: GitHub Actions.
5. **Schedule** — adjust the cron in `daily-digest.yml` (UTC).

### Acceptance criteria (from the original handoff)

- A scheduled daily run sends one grouped email to all recipients. ✔ `daily-digest.yml`
- No item repeats across days (durable dedup). ✔ committed `state/seen.json`, verified by `--self-test`
- A single dead source does not abort the run. ✔ `sources.ingest`
- The full stack is reproducible from the repo. ✔ no external infra to provision

## What changed from the original handoff

The handoff assumed AWS (Lambda + EventBridge + SES + DynamoDB). This build
targets GitHub Actions instead, which dissolves most of that:

- **DynamoDB → committed JSON seen-store.** DynamoDB existed only to give Lambda's
  ephemeral `/tmp` durability; off Lambda it isn't needed. The store is pluggable
  (`seen`/`mark`/`save`) so swapping to S3/Dynamo later is one class.
- **SES → Resend** (SMTP fallback retained).
- **Lambda/EventBridge → GitHub Actions cron.**
- **In-browser companion → preview/export companion** fed by the published JSON.

Faithful to the original: authoritative-first sources, topics-as-config,
dead-source-skips-not-aborts, offline self-test, Bing stays retired.
