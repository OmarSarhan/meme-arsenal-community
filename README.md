# Meme Arsenal

Meme Arsenal helps individuals self-host a private meme library. Upload an image,
save it to your arsenal, add a description, and find it again in the gallery search.
Public sharing and Telegram import are optional.

**[Set up the full private owner app](deploy/community/README.md#upload-first-setup)**
for HTTPS browser upload, save and search with PostgreSQL and a persistent worker.
Start with that guide's host, storage and source-access prerequisites; its first
workflow uses mock AI without provider accounts or Telegram.

For source changes, use the [contributor installation](docs/development.md#first-local-setup)
and its development prerequisites. To try fixture browsing without accounts, use
the [limited SQLite/static mock demo](docs/development.md#no-account-local-demo);
it does not demonstrate the installed owner app.

Public source: [OmarSarhan/meme-arsenal-community](https://github.com/OmarSarhan/meme-arsenal-community).
Version **v0.1.1** is a source-built release for Linux amd64 with rootful Docker
and Compose 2.30+. No prebuilt container image or wheel is published.

```bash
git clone --branch v0.1.1 https://github.com/OmarSarhan/meme-arsenal-community.git
cd meme-arsenal-community
git rev-parse HEAD
```

Continue with the [owner setup guide](deploy/community/README.md#upload-first-setup).
See [release evidence and limits](docs/release-preparation.md) and
[confidential security reporting](SECURITY.md). Public history starts fresh;
the original and development repositories remain private.

See [derived data and explicit refresh](docs/derived-data-refresh.md) for identity,
legacy data, and potentially paid archive refresh behavior.

## License

Meme Arsenal's original code and project assets use the [0BSD license](LICENSE).
You can use, modify, redistribute, and sell them without attribution or an
obligation to publish your changes. See the [asset and dependency notes](docs/licensing.md)
for provenance and third-party licenses. Your imported media retains its own rights.

The sections below retain local CLI and Telegram reference material for development
and optional imports. They are not prerequisites for the owner app above.

The **Meme Arsenal Reserve** is the data folder. By default, local commands use
this folder under the current working directory:

```text
meme-arsenal-reserve/
```

By local default, that reserve contains your SQLite database, Telegram session,
downloaded media, generated previews, catalog JSON, dashboard build, run logs,
and remix drafts. It is ignored by git. Follow the backup instructions below;
archiving the reserve alone does not capture an externally configured database.

## Read Next

This is the starting point and documentation index. All maintained project guides
are linked below; generated reserve data is not project documentation.

- **Contributors:** [developer guide](docs/development.md#first-local-setup)
  for setup, [source map](docs/development.md#where-to-make-changes), and
  [safe checks](docs/development.md#checks-without-live-services).
- **Private upload-first self-hosting:** [primary community guide](deploy/community/README.md#upload-first-setup).
- **Local Telegram workflow:** [Quick Start](#quick-start), [Configuration](#configuration),
  [Everyday Commands](#everyday-commands), [Pipeline](#pipeline),
  [Dashboard](#dashboard), [Remix](#remix), [AI Classification](#ai-classification),
  [Semantic Search](#semantic-search), and [Telegram Notes](#telegram-notes).
- **Data and recovery:** [Reserve Layout](#reserve-layout),
  [reserve placeholder](meme-arsenal-reserve.example/README.md), and
  [Troubleshooting](#troubleshooting).
- **Hosted service operators:** [online architecture](docs/online-architecture.md#system-overview),
  [public/private boundaries](docs/online-architecture.md#public-and-private-boundaries),
  [deployment](docs/online-architecture.md#first-deploy), and
  [backup](docs/online-architecture.md#backup)/[restore](docs/online-architecture.md#restore).
  Start new instances with the [community deployment recipe](deploy/community/README.md).
  The architecture guide retains generic operations for existing deployments.
- **Verification:** [Tests](#tests) and the developer guide's
  [limitations](docs/development.md#limits-and-troubleshooting).
- **Contributing and privacy:** [contribution guide](CONTRIBUTING.md),
  [data flows](docs/privacy.md), and [security reporting](SECURITY.md).

## Quick Start

Local reference only: this inherited workflow imports from Telegram. For private browser upload/save
without Telegram, use the [community guide](deploy/community/README.md#upload-first-setup).

Run commands from the repository root.

1. Install the prerequisites and editable package described in [Setup Notes](#setup-notes).
2. Copy `.env.example` to `.env`.
3. Add your Telegram app credentials from `https://my.telegram.org/apps`:

```bash
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
```

4. Initialize the reserve and database schema:

```bash
arsenal init
```

5. Log in to Telegram once:

```bash
arsenal telegram-login
```

6. Import, process, classify, and embed new content:

```bash
arsenal sync --limit 100
```

7. Serve the local dashboard:

```bash
arsenal serve-dashboard --host 127.0.0.1 --port 8123
```

Open:

```text
http://127.0.0.1:8123/dashboard/dist/
```

Keep the server running while browsing. It serves the dashboard, catalog, and
media paths without exposing private reserve data.

## Online Deployment

Start with the [community deployment recipe](deploy/community/README.md) for Docker
Compose, private Caddy HTTPS, PostgreSQL, Redis, FastAPI and a persistent remix
worker, with optional Telegram sync. Authenticated ingress, a configured durable
owner namespace, and explicit opt-in sharing are implemented; see the current
recipe and [sharing guide](docs/opt-in-sharing.md). Accepted private CI covers
restored app/worker operation and the declared development-schema recovery path.
See [release preparation evidence](docs/release-preparation.md) for tested
identities, support limits and the remaining public-release gates.
See [docs/online-architecture.md](docs/online-architecture.md) for data architecture,
generic service operations, backup/restore limitations and health checks.
The hosted runtime serves a live, API-backed dashboard and does not use
`/catalog/*` in production.

## Setup Notes

This checkout does not include a devcontainer configuration. For local use,
use the Linux/Python 3.12/Node 22 development target with ffmpeg. Node must be
at least 22.12 within major 22 to satisfy the locked toolchain. CI selects
Node major 22; this does not mean every minor is tested. Package metadata allows
Python 3.11+, without promising release testing for every version/platform.
Chromium is needed only for browser checks. Install the editable dev extra and
dashboard dependencies as in the [developer guide](docs/development.md#first-local-setup):

```bash
python -m pip install -e ".[dev]"
npm --prefix frontend/dashboard ci
```

The local default is `./meme-arsenal-reserve`, with SQLite at
`data/arsenal.sqlite` inside it unless `DATABASE_URL` is set explicitly.
Relative paths use the working directory; for a reserve beside the repo, set
`RESERVE_PATH=../meme-arsenal-reserve`. Containers explicitly use `/reserve`.
Changing settings does not move an existing library; keep its explicit paths.

## Configuration

Settings load in order: defaults, the first existing YAML candidate (`--config`,
`config.yaml`, `config.yml`), `env`, `.env`, then environment variables. Later
values override earlier ones. Secret-file settings use the `*_FILE` suffix;
see [the loader](src/meme_arsenal/config.py) for field-specific empty-value rules.
For local commands, `.env` is usually enough. The owner app uses its external
instance configuration.

Optional bounded operator style guidance and its private result identity, retention
and refresh implications are described in [derived data and refresh](docs/derived-data-refresh.md).

Common settings:

```bash
RESERVE_PATH=meme-arsenal-reserve
TELEGRAM_API_ID=123456
TELEGRAM_API_HASH=your_api_hash
AI_PROVIDER=mock
```

For offline smoke runs and tests, use `AI_PROVIDER=mock` with no live capability
overrides or retained live remix work. Use a live provider for useful
classification and embeddings:

```bash
AI_PROVIDER=google
AI_MODEL=gemini-2.5-flash
EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMENSIONS=768
GEMINI_API_KEY=your_key
```

or:

```bash
AI_PROVIDER=openai
OPENAI_API_KEY=your_key
```

Example AI settings (choices, not a list of defaults):

```bash
AI_MODEL=gemini-2.5-flash
EMBEDDING_MODEL=gemini-embedding-001
AI_REQUEST_DELAY_SECONDS=4
AI_MAX_RETRIES=3
AI_MAX_RETRY_DELAY_SECONDS=90
AI_STOP_ON_RATE_LIMIT=true
AI_INCLUDE_VIDEO_ORIGINAL=false
AI_VIDEO_INLINE_MAX_MB=20
```

Useful remix knobs:

```bash
REMIX_MODEL=gemini-3.1-flash-image
REMIX_ASPECT_RATIO=1:1
REMIX_IMAGE_SIZE=1K
REMIX_INCLUDE_VIDEO_ORIGINAL=true
REMIX_VIDEO_INLINE_MAX_MB=20
MAX_VIDEO_KEYFRAMES=8
```

[config.example.yaml](config.example.yaml) and the `.env.example` template list
user-facing options; [config.py](src/meme_arsenal/config.py) defines defaults
and loading behavior.

## Everyday Commands

Show command help:

```bash
arsenal --help
```

Initialize or update the database schema:

```bash
arsenal init
```

Log in to Telegram:

```bash
arsenal telegram-login
```

Run the full normal workflow:

```bash
arsenal sync --limit 100
```

For a complete isolated no-account setup, follow the
[local mock demo](docs/development.md#no-account-local-demo). In an already
configured mock archive, import the local fixtures with:

```bash
arsenal sync --fixture-dir tests/fixtures --limit 10
```

Rebuild only catalog JSON and the dashboard from existing database/media:

```bash
arsenal export-catalog
arsenal build-dashboard
```

Serve the local dashboard with remix API support:

```bash
arsenal serve-dashboard --host 127.0.0.1 --port 8123
```

Run semantic search and export a static result bundle:

```bash
arsenal search "when the deploy fails"
```

Run tests:

```bash
arsenal test
```

## Pipeline

`arsenal sync` (local mode) runs:

```text
import -> backfill-telegram-post-urls -> pipeline
```

In local mode, `pipeline` is followed by static catalog/dashboard rebuilds to keep
`serve-dashboard` data fresh.

The `pipeline` step runs:

```text
process-media -> classify -> embed
```

For a single item, those stages remain ordered. Across different items, the
pipeline can overlap work: one item can be classified while another is having
thumbnails, previews, keyframes, hashes, or OCR generated.

`--limit` means "work on up to this many items/messages that still need work."
Import scans Saved Messages newest-to-older and continues farther back on later
runs. A limited sync does not unexpectedly classify or embed your entire backlog.

Worker knobs are available for larger runs:

```bash
arsenal sync --limit 500 --process-workers 6 --classify-workers 3 --embed-workers 3
arsenal pipeline --limit 500
```

Defaults are 6 local processing workers, 3 classification workers, and 3
embedding workers. Telegram import remains single-account and is paced by
`IMPORT_DELAY_SECONDS`; use `--delay` to override it for one run:

```bash
arsenal import --limit 100 --delay 0.75
```

For Gemini free-tier runs, keep `--classify-workers 1` unless you have quota
headroom.

During Telegram import, the CLI shows progress with the current message date and
message id so you can see where it is while it scans newest-to-older and imports
oldest-to-newest.

Tests generate their own fixtures, so the real test suite does not require
Telegram or live AI calls.

## Dashboard

For local mode, the generated dashboard lives at:

```text
meme-arsenal-reserve/dashboard/dist/
```

Serve it with the built-in server so the app can load sibling `catalog/` and
`media/` paths without exposing private reserve data:

```bash
arsenal serve-dashboard --host 127.0.0.1 --port 8123
```

Then open:

```text
http://127.0.0.1:8123/dashboard/dist/
```

Local browsing uses an exported snapshot. Rebuild it after CLI changes with
`arsenal export-catalog` and `arsenal build-dashboard`.

The dashboard includes:

- Gallery browsing with infinite scroll.
- Unified archive search over captions, source URLs, summaries, OCR text,
  narration, media fields, manual tags, notes, topics, keywords, and search
  phrases.
- Filter rail and mobile-friendly filter controls.
- Detail panel with media, metadata, source links, narration, tags, duplicate
  groups, and remix lineage.
- Video hover previews when generated previews or conventional fallback preview
  files are available.
- Optional semantic result bundles produced by `arsenal search`; when present,
  they enrich the same gallery search results instead of creating a separate
  result subset.
- Remix panel for live AI remixing when served by `arsenal serve-dashboard`.

Telegram links are shown only when the stored link is a permanent public post
URL such as:

```text
https://t.me/public_channel/123
```

Private `t.me/c/...` links, invite links, and other non-public Telegram URLs are
intentionally hidden from the dashboard action area.

## Local vs Live Dashboard

Use `arsenal serve-dashboard` for local browsing and Remix; use the online app
for server-backed editing, sharing and durable owner recovery.

The local server excludes `data/telegram.session`, `data/arsenal.sqlite`, run logs,
exports and remix drafts from static serving. It has no owner-login boundary;
use loopback for local work. Binding to `0.0.0.0` exposes it to reachable peers.

For the hosted service, first follow the explicit owner provisioning steps in
[the community runbook](deploy/community/README.md). API and worker startup
require `OWNER_SESSION_ID_FILE` and its existing database row; cookies do not
select a namespace. After provisioning, use:

```bash
arsenal serve-online --host 0.0.0.0 --port 8000
```

The production frontend path is loaded from `dashboard/arsenal-config.json` and
calls `/api/*` endpoints at runtime.

Do not serve the whole reserve with a general static server: it contains private
database, session and log files.

`arsenal serve-dashboard` serves local files and adds:

```text
/api/remix/health
/api/remix/analysis
/api/remix/jobs
/api/remix/jobs/{id}
/api/remix/jobs/{id}/image
/api/remix/jobs/{id}/save
/api/remix/uploads
/api/remix/uploads/{id}/save
```

When running inside a container, bind to `0.0.0.0` so Docker can forward the port. Open
the browser on the host at:

```text
http://127.0.0.1:8123/dashboard/dist/
```

If the local dashboard is stale, rebuild first:

```bash
arsenal export-catalog
arsenal build-dashboard
```

Use `arsenal build-online-dashboard` only when building the hosted, API-backed
dashboard. It rewrites `dashboard/arsenal-config.json` for API mode and is not
part of local `serve-dashboard` recovery.

## Remix

Remix generation requires the effective Remix provider to be Google and a
compatible Gemini image model. For example:

```bash
AI_PROVIDER=google
GEMINI_API_KEY=your_key
REMIX_MODEL=gemini-3.1-flash-image
```

Start the live dashboard:

```bash
arsenal serve-dashboard --host 127.0.0.1 --port 8123
```

In the dashboard, choose two different source memes in the Remix panel. You can
open detail views and browse normally while the panel stays visible. Video
sources use sampled stills across the clip, and short original videos are sent
to Gemini on the first generation attempt when `REMIX_INCLUDE_VIDEO_ORIGINAL` is
enabled and the file is under `REMIX_VIDEO_INLINE_MAX_MB`.

Use **Analyze** before generation to ask Gemini for pair-specific tweak controls.
The analysis can return up to four sliders based on the selected memes and their
metadata. Each slider samples from its own fixed color range, uses a bar-shaped
handle, and keeps a small grey numeric value visible. Changing tweak values does
not clear an already generated image.

Use **Create Remix** to start a job. The server prepares source previews,
submits a Gemini request, and records mitigation attempts if Gemini blocks or
refuses the first prompt. If a full-video remix attempt is blocked, retries omit
the original video and fall back to the richer still-frame context. A ready remix
can be saved back into the arsenal as a normal image item with summary,
narration, tags, source lineage, embedding, and catalog export support. Tweak
values and prompt metadata are stored in the remix's raw metadata for
traceability.

Turn on **Safer** before **Create Remix** when a source looks likely to trigger a
Gemini safety refusal. Safer jobs skip the original-video first attempt and start
with both sources reduced to abstract visual and meme-format cues. If Gemini still
blocks that request, the automatic retry switches to visual-only metadata while
keeping the sampled still frames.

Generated drafts are stored under:

```text
meme-arsenal-reserve/data/remix-drafts/
```

## AI Classification

The default mock provider is deterministic and local. Retained remix jobs keep their
captured generation configuration: Google jobs can still call Google after settings
switch to mock, using current credentials, source authorization, and the credit circuit.
Hosted jobs retain capture across requeue/restart; the local executor has no durable
queue. Stopping its process or the hosted worker prevents new dispatch, but cannot
cancel an in-flight request. See [privacy](docs/privacy.md).
Mock is useful for
testing the pipeline shape, but it will not produce meaningful archive metadata.

With a live provider, Meme Arsenal classifies images and generated video
keyframes into a V1 schema that includes summary, visible text, narration,
language, meme format, tone, reaction type, topics, keywords, people or
characters, safety flags, and search phrases.

The OpenAI path uses the Responses API for image/text classification with JSON
Schema output and the Embeddings API for vector generation.
The Google path uses Gemini `generateContent` for image/text classification and
`embedContent` for vector generation.

For videos, Meme Arsenal sends generated keyframes to the classifier. If
`AI_INCLUDE_VIDEO_ORIGINAL=true`, it can also include small original video files
up to `AI_VIDEO_INLINE_MAX_MB` for models that understand audio/video. When an
original video is larger than that limit and `ffmpeg` is available, the Google
classifier first tries a temporary low-frame-rate compressed MP4 with audio
retained. If that still cannot fit, it falls back to extracted audio plus the
sampled keyframes.

Gemini free-tier quotas are easy to hit during archive backfills. When
`AI_PROVIDER=google` and `AI_REQUEST_DELAY_SECONDS` is unset, `arsenal classify`
paces requests at 4 seconds between items. Each Gemini request also uses bounded
exponential backoff with jitter and honors Gemini retry hints when available.
Rate-limit and failure details are written to:

```text
meme-arsenal-reserve/data/run-logs/
```

If Gemini blocks a classification response, Meme Arsenal stores a placeholder
classification instead of dropping the item. The catalog marks the item as
blocked, and the dashboard renders it with blocked status styling.

Useful Gemini tuning knobs:

```bash
AI_REQUEST_DELAY_SECONDS=4
AI_MAX_RETRIES=3
AI_MAX_RETRY_DELAY_SECONDS=90
AI_STOP_ON_RATE_LIMIT=true
```

For a one-off run, prefer:

```bash
arsenal classify --ai-delay 4 --ai-max-retries 3
```

## Semantic Search

`arsenal embed` stores vectors in SQLite as JSON. `arsenal search "query"` runs
hybrid lexical and semantic search using portable cosine similarity and writes a
static result bundle under:

```text
meme-arsenal-reserve/catalog/search-results/
```

Generated bundles add related items to the local gallery search and can improve
ranking while keeping exact text matches visible. The online gallery uses
PostgreSQL text/prefix search; enabling embeddings does not make it semantic.

## Telegram Notes

Meme Arsenal is designed for your own Telegram Saved Messages archive, not public
channel scraping or bulk harvesting. It keeps imports bounded by `--limit`, paces
requests with `IMPORT_DELAY_SECONDS`, and respects Telegram flood-wait responses.

The Telegram session is stored only in the reserve:

```text
meme-arsenal-reserve/data/telegram.session
```

Import and media processing reports are written under:

```text
meme-arsenal-reserve/data/run-logs/
```

Reports include useful debugging context such as message id, message date,
caption snippet, source URLs from captions/link previews, and errors where
available.

If Telegram post URLs were imported before URL backfill support existed, run:

```bash
arsenal backfill-telegram-post-urls
arsenal export-catalog
arsenal build-dashboard
```

## Reserve Layout

A typical reserve looks like:

```text
meme-arsenal-reserve/
  data/
    arsenal.sqlite
    telegram.session
    run-logs/
    exports/
    remix-drafts/
  media/
    originals/
    thumbnails/
    previews/
    video-previews/
    keyframes/
  catalog/
    index.json
    tags.json
    search-index.json
    items-000.json
    search-results/
  dashboard/
    dist/
```

For local SQLite tooling only, stop all writers and verify that the actual
configured SQLite database file is inside this reserve before archiving it:

```bash
tar -czf meme-arsenal-reserve-backup.tgz meme-arsenal-reserve
```

With writers stopped, extract into a new empty directory and configure
`RESERVE_PATH` and the SQLite database path to the restored files. Verify the
database and media before resuming. An externally configured SQLite file is not
included. Community PostgreSQL and protected configuration require the
[complete private backup](deploy/community/README.md#consistent-private-maintenance-backups)
and [exact-image restore](deploy/community/README.md#restore-to-a-disposable-empty-instance).

## Tests

Run everything:

```bash
arsenal test
```

Run individual suites:

```bash
arsenal test-backend
arsenal test-frontend
arsenal test-e2e
arsenal test-chromium
```

Run the same bounded React dashboard suite as CI (workflow and recipe preview,
24 cases across 320, 390, 768, and 1440 pixel viewports):

```bash
scripts/run_chromium_tests.sh
```

The helper installs dashboard npm dependencies and builds the real React dashboard
before testing. It uses local tooling when Python's `.[dev,browser]` extras, npm,
and Chromium are available, and otherwise builds and runs the Docker test image.
For local setup, run `python -m pip install -e '.[dev,browser]'` and
`python -m playwright install chromium`, or set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` to an installed Chromium executable.
System `chromium` is also detected. These image-only fixture tests do not need ffmpeg.
Set `MEME_ARSENAL_FORCE_DOCKER=1` to use the same container path as CI.
Optional pytest arguments work in either mode, for example
`scripts/run_chromium_tests.sh -k small-mobile`.

The suite uses a loopback fixture server and mocked API responses; no archive,
provider credentials, or Telegram account is needed. Missing frontend builds fail
the selected tests instead of silently skipping them. CI reports backend checks,
the frontend build, and this browser suite in independent jobs. The older
`test_dashboard_chromium.py` and `test_dashboard_live_chromium.py` suites remain
available for explicit checks but are not part of this bounded CI selection.

Useful direct commands while developing:

```bash
pytest
npm --prefix frontend/dashboard run build
```

Backend and E2E tests use SQLite plus mock AI by default. They do not require a
Telegram account or live AI keys.

## Troubleshooting

If `arsenal` is not found, activate the virtual environment where you installed
the package, or install it there:

```bash
pip install -e ".[dev]"
```

If Telegram login repeats, check that `RESERVE_PATH` points to the same
persistent reserve every time. The session should be at:

```text
meme-arsenal-reserve/data/telegram.session
```

If dashboard images or JSON do not load, run `arsenal serve-dashboard` and open
`/dashboard/dist/`.

If Remix controls show disabled or unavailable, review the
[provider capabilities](deploy/community/README.md#provider-capabilities).
Analyze/Create require a compatible Google Remix model and resolved key; changing
only `AI_PROVIDER` leaves existing model settings unchanged. Classification,
embeddings and Remix can each override the global provider.

For offline local work, select `AI_PROVIDER=mock`, remove live capability overrides
(including file-based overrides), and use no retained live Remix work. Retained
Google jobs keep their captured generation settings and can still dispatch with
current credentials after a switch to mock; see [privacy](docs/privacy.md).

If you hit Gemini quota errors, reduce classification concurrency and add delay:

```bash
arsenal classify --ai-delay 4 --ai-max-retries 3
arsenal pipeline --classify-workers 1 --ai-delay 4 --ai-max-retries 3
```

For a disposable local SQLite experiment only, stop all writers and back up
before deliberately deleting the **actual configured SQLite database file** if
its loss is intended. The default is `data/arsenal.sqlite` under `RESERVE_PATH`,
but `DATABASE_URL` can select another location. This loses database records and
leaves media behind; it is not owner-app reset or recovery. For a real library,
use the [complete private backup and restore procedures](deploy/community/README.md#consistent-private-maintenance-backups).
