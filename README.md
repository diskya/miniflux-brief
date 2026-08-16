# miniflux-brief

Every new Miniflux entry is summarized on arrival and the summary is written into
the entry itself, so the unread list *is* the digest. A **Brief** button in the
Miniflux UI does the same on demand, for older entries and re-runs.

```text
Miniflux ──webhook──▶ feedsum ──▶ queue ──▶ summarize ──▶ write back into the entry
   ▲                     ▲                      │
   └── Brief button ─────┘                      ├─ YouTube ──▶ Gemini watches the video
                                                └─ else ────▶ reader extracts text ──▶ your text model
```

Each brief is 3–5 concrete takeaways, then **Strengths**, then **Critique**, in
the language of the source.

- **Routing follows the content, not the URL.** A page that is really a wrapper
  around a video declares a schema.org `VideoObject`; the video gets summarized,
  not the page's navigation rails.
- **The button is a status light** — one request per page says which entries have
  a brief, which are running, which failed.
- **Re-running never stacks up.** The original entry content is kept, and every
  write rebuilds from it.
- **A model that was not given the article says so** instead of inventing one.

## What you need

Miniflux, an OpenAI-compatible endpoint (a provider, or Ollama), and Docker or
Python 3.11+. A Gemini key is optional — without one, only YouTube entries fail.

## Quick start

```bash
git clone https://github.com/diskya/miniflux-brief
cd miniflux-brief
cp .env.example .env      # every variable is explained in the file
$EDITOR .env              # MINIFLUX_URL, a token, a secret, an LLM endpoint
docker compose up -d
```

This starts **feedsum** (the summarizer, ~200 MB) and the **reader** (URL to
clean article text, ~2.4 GB — it carries two Chromium builds for pages that will
not give up their text to a plain fetch).

Then in Miniflux:

1. **Settings → API Keys** → create one, put it in `MINIFLUX_API_TOKEN`.
2. **Settings → Integrations → Webhook** → `http://127.0.0.1:8012/webhook` and
   the same secret as `FEEDSUM_WEBHOOK_SECRET`. A private URL also needs
   `INTEGRATION_ALLOW_PRIVATE_NETWORKS=1` in the Miniflux config — that is scoped
   to integrations, so feed fetching keeps its SSRF protection.
3. Optional, for the button: paste `ops/custom.js` into **Settings → Custom
   JavaScript** and `ops/custom.css` into **Settings → Custom CSS**. By hand —
   the Miniflux API has no field for custom JS. Re-paste after editing either
   file, or let `ops/apply-assets.py` write both to the database for you (it
   keeps any CSS of your own).

If Miniflux runs in its own Compose project, join its network rather than routing
through the host:

```yaml
# docker-compose.override.yml
services:
  feedsum:
    networks: [brief, miniflux_default]
networks:
  miniflux_default: { external: true }
```

### The Brief button needs a reverse proxy

Miniflux's CSP is `connect-src 'self'`, so its custom JS can only call the
Miniflux origin. Whatever proxy already fronts Miniflux must therefore strip one
prefix and forward it to feedsum:

```caddyfile
feeds.example.com {
    handle /_summary/* { uri strip_prefix /_summary; reverse_proxy 127.0.0.1:8012 }
    reverse_proxy 127.0.0.1:8080
}
```

An nginx version is in [`examples/`](examples/); any proxy expresses the same two
rules. Whatever authenticates Miniflux now authenticates these calls too, and no
API token reaches the page.

**Optional.** It buys the button. Without a proxy the webhook still runs and the
briefs still land in the entries.

| Button | Meaning |
| --- | --- |
| `Brief` grey | none yet — clicking generates one (60–100s) |
| `Brief …` amber | being generated, or waiting on a retry |
| `Brief` green | ready — opens instantly |
| `Brief !` red | failed; click to see why |

Miniflux's **Save** button also fires the webhook and forces a re-run.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `MINIFLUX_URL` | `http://127.0.0.1:8080` | write-back + URL lookup |
| `MINIFLUX_API_TOKEN` | — | **required** |
| `FEEDSUM_WEBHOOK_SECRET` | — | must match the Miniflux integration |
| `OPENAI_API_BASE` | — | **required**; any OpenAI-compatible endpoint |
| `OPENAI_API_KEY` | — | **required** |
| `FEEDSUM_TEXT_MODEL` | — | **required**; summarizes articles |
| `GEMINI_API_KEY` | — | optional, **comma-separated list**; a quota each |
| `FEEDSUM_GEMINI_MODEL` | `gemini-3.7-flash` | must support video input |
| `FEEDSUM_WRITE_BACK` | `1` | `0` leaves entries alone |
| `FEEDSUM_FEED_IDS` | all | restrict automatic briefing to these feeds |
| `FEEDSUM_CONCURRENCY` | `1` | parallel jobs |
| `FEEDSUM_JOB_TIMEOUT` | `600` | seconds per entry |
| `FEEDSUM_RETRY_DELAYS` | `120,900,3600` | waits between attempts; empty disables |
| `FEEDSUM_RESUME_LIMIT` | `20` | pending entries a restart picks up again |
| `FEEDSUM_MAX_ARTICLE_CHARS` | `200000` | truncation before the text model |
| `FEEDSUM_READER_URL`, `FEEDSUM_DB`, `FEEDSUM_BIND` | | set by Compose |

## HTTP

| Route | Purpose |
| --- | --- |
| `GET /healthz` | liveness |
| `POST /v1/summaries` | `{"entry_id": 586, "force": false}` — queue one entry |
| `GET /v1/summaries/{id}` | poll for the result |
| `POST /v1/summaries/status` | `{"entry_ids":[…]}` — status for a whole page |
| `POST /webhook` | Miniflux `new_entries` / `save_entry` |

Nothing runs inline: a request records a pending row and queues a job. Entry URLs
are read back from Miniflux by id, never taken from the caller. Webhooks need a
valid `X-Miniflux-Signature`.

## Why these pieces

- **No fork, no plugin.** Miniflux has none; a webhook, an API that rewrites an
  entry, and Custom JS are enough.
- **Two services**, because fetching is the hostile half: the reader is the only
  thing touching untrusted sites, so it gets its own container and a network with
  nowhere private to go.
- **Scrapling fetches, trafilatura extracts.** Scrapling solves acquisition
  (HTTP → browser → stealth). Its Markdown output is everything inside `<body>`,
  navigation and "Up next" rails included — exactly what an LLM must not be fed,
  because it will summarize it. Trafilatura decides which part of the DOM is the
  article; `favor_precision` drops the rails.
- **Cheap path first.** A static fetch answers in ~400 ms; a browser costs
  seconds. The reader escalates only when extraction comes back too thin, or the
  page was a challenge.
- **Gemini watches YouTube directly**, which deletes a whole transcript pipeline.
  Articles are text in, text out, so that half stays provider-neutral.
- **SQLite, no broker.** A few minute-long jobs a day do not need Celery, and the
  database has to exist anyway to hold the original entry content.
- **A 60-line Markdown renderer**, not a parser: the output shape is known, and
  escaping everything first keeps model output from becoming markup.

## Behaviour worth knowing

- **Write-back is idempotent.** Miniflux strips markers from HTML, so instead the
  first content ever seen is stored and every write rebuilds from that copy.
- **Several Gemini keys rotate.** On `429`/`5xx` feedsum moves to the next key and
  remembers the one that worked. Keys go in a header, never a query string.
- **Transient failures retry** on `FEEDSUM_RETRY_DELAYS` and stay amber; refusals
  and paywalls fail immediately with the reason. A restart resumes what was in
  flight, up to `FEEDSUM_RESUME_LIMIT`, so a burst cannot stampede providers.
- **The reader is swappable**: point `FEEDSUM_READER_URL` at anything answering
  `GET /v1/read?url=…&json=true` with `{"text", "title", "video_url"}`.
- **Do not bulk-queue a backlog.** ~95 entries at once is enough to exhaust a
  Gemini daily quota and get you throttled by the source sites.

## Without Docker

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt          # feedsum
.venv/bin/pip install -r reader/requirements.txt   # the reader
sudo .venv/bin/playwright install-deps chromium
.venv/bin/scrapling install && .venv/bin/patchright install chromium

.venv/bin/python -m uvicorn feedsum.app:app --host 127.0.0.1 --port 8012
.venv/bin/python -m uvicorn reader.app:app  --host 127.0.0.1 --port 8010
```

Both browsers are needed: Scrapling drives the dynamic tier with Playwright and
the stealth tier with Patchright. `ops/` has systemd units for both services.

## Development

```bash
pip install -r requirements-dev.txt -r reader/requirements.txt
pytest        # 60 tests, no network or browser needed
```

## Known gaps

- Video summarizing is **YouTube-only**, since it works by handing Gemini a watch
  URL. Pages without a schema.org `VideoObject` fall back to the article path and
  refuse rather than fabricate.
- Briefs are only as good as the extraction; the refusal path is the safety net.

## License

MIT — see [LICENSE](LICENSE).
