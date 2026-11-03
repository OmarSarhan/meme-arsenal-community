# Meme Arsenal Online Architecture

[README and documentation index](../README.md#read-next) ·
[Local developer guide](development.md#first-local-setup)

Use this page to trace runtime behavior and diagnose an installed private library.
For example, a browser upload crosses authenticated Caddy ingress into the API;
the API stores its records in PostgreSQL and bytes in the reserve. With live
classification that upload may contact a provider before Save. Saving adds it to
the private library; explicit output/recipe grants control external sharing.

## Common runtime tasks

- Start or operate a new instance with the [community guide](../deploy/community/README.md#upload-first-setup),
  using the same project, env file and ordered manifests throughout.
- Change providers/models/keys through [graceful configuration maintenance](../deploy/community/README.md#deliberate-credentialconfig-changes).
  Review [data flows](privacy.md) before live uploads and
  [refresh authority](derived-data-refresh.md) before paying to replace metadata.
- For missing UI or failed jobs, check authenticated health, assets and bounded
  role logs. Health alone does not prove UI readiness. Logs can contain private
  content. [Playback diagnostics](#playback-and-usage-diagnostics) explain events;
  [credit status](#gemini-credit-circuit) explains provider pauses.
- Before maintenance, arrange a [complete private backup](../deploy/community/README.md#consistent-private-maintenance-backups).
  Recovery requires exact images, owner/config and reserve as well as the DB.

The remaining sections explain schema, routes, workers and trust, followed by
legacy external-proxy operations requiring reviewed adaptation.

Read [System Overview](#system-overview) and
[Public And Private Boundaries](#public-and-private-boundaries) first. The browser
uses FastAPI and PostgreSQL; static catalog exports are local tooling. The
`deploy/service` sections below assume an existing authenticated external proxy
and need review before use. Their commands change persistent data. For tested
snapshots and recovery limits, see [release evidence](release-preparation.md).

## System Overview

```text
Browser
  -> operator-managed Caddy, Basic Auth protected
    -> meme-arsenal-app:8000
      -> FastAPI live API and SPA shell
      -> PostgreSQL for catalog/session/remix state
      -> Redis for remix wakeups and rate limits
      -> /reserve for media files

worker
  -> PostgreSQL queued remix jobs
  -> Redis wakeups
  -> Gemini/OpenAI providers
  -> /reserve/media for saved remixes

sync
  -> Telegram import
  -> process/classify/embed/repair
  -> refresh public DB indexes
```

Legacy Compose project: `meme-arsenal`.
Example dashboard hostname: `library.example.net` (replace with your own).
External-proxy upstream alias: `meme-arsenal-app:8000`.
Example reserve: `/srv/my-library/state/reserve`, mounted in containers at `/reserve`.
Community ingress will use `app:8000` on its project-local network.

## Database Architecture

PostgreSQL is the production database. SQLite is only a local development format and a one-time migration source from `/reserve/data/arsenal.sqlite`.

Primary imported/archive tables:

- `telegram_messages`
- `media_items`
- `media_keyframes`
- `classifications`
- `embeddings`
- `import_checkpoints`
- `import_failures`
- `gemini_call_errors`
- `ai_provider_circuits`

Public/live tables:

- `anonymous_sessions`
- `anonymous_session_curation`
- `media_tags`
- `public_search_documents`
- `remix_jobs`
- `remix_attempts`
- `remix_sources`

`media_tags` and `public_search_documents` are regenerated from imported data and should not be copied directly from SQLite. Embeddings preserve `embedding_json`; the native pgvector column is left empty until a proper pgvector adapter is introduced.

## Public And Private Boundaries

The operator's proxy must apply Basic Auth to the dashboard hostname, including legacy
`/share/{token}` paths. New shares use the separate public hostname below and do
not require Basic Auth or an arsenal session. Legacy signed links remain valid
behind the dashboard's existing authentication boundary until revoked.

### Dedicated public sharing hostname

`PUBLIC_SHARE_URL=https://share.example.net` selects a separate hostname for five-character
public links such as `/ADTWR`. Codes use cryptographically random uppercase letters
and digits 2-9. These short links are public, not an unguessable privacy boundary.
Share reads are rate limited per client IP. Meme and remix-card links have separate
codes; a meme code cannot access a remix card's sources. Recall invalidates both
variants. Re-sharing issues new codes, and deleted or recalled codes are never
reassigned to another item. Existing signed links continue to resolve until revoked.

For current enablement and configuration changes, follow the exact graceful-drain
[sharing maintenance procedure](opt-in-sharing.md#maintain-an-existing-instance-safely).
`PUBLIC_SHARING_ENABLED=false` blocks publication and capability serving; a URL
alone never enables sharing. Migration 013 recalls all legacy grants, including
output links; take a consistent private backup before migration. Deliberate
republication creates new links. Do not recreate active writers to change a URL.

The retained `deploy/service/public-share.Caddyfile` is a legacy external-proxy
reference requiring reviewed adaptation. Preserve its narrow anonymous route
allowlist, trusted client-IP forwarding and private dashboard authentication.
Public host routing does not authenticate owners. Current community setup uses
its own ingress and trust configuration; do not mix these proxy recipes.
For the legacy trusted proxy, preserve client-IP forwarding and its
`PUBLIC_TRUST_PROXY_HEADERS=true` setting so limits apply to visitors. Keep the
anonymous hostname free of dashboard Basic Auth, catch-all routes and access
logs containing share codes; keep private dashboard authentication intact.

Remix pages use the saved `remix_tweaks` snapshot, source posters and full descriptions,
output image, and generation intent. `remix_page.py` renders the same responsive HTML
for private previews and public pages, without an AI request. The Arsenal preview
grows with the content, leaving one page scrollbar rather than a nested scroll area.
`GET /api/items/{id}/remix-card?page=true` returns that private preview; the endpoint
without `page` still returns an optional PNG export from `remix_card.py`.
`POST /api/items/{id}/share?card=true` issues a recipe-capable short link on the public
hostname (or a signed token for `/share/{token}/card` without that hostname).
`/card/image?source=A` and `?source=B` serve only the two linked source posters and
require the same recipe-capable share. All public recipe text is HTML-escaped.
Video ingredients have inline players with sound and fullscreen. Adding
`&playback=true` to a source URL serves its cached browser-compatible H.264/AAC
MP4 through the existing playback preparation path. Players use `preload="none"`
so opening a page does not transcode or download its videos, and starting one
pauses the other. Source video access follows the parent recipe's revocation;
ordinary media-share tokens cannot play recipe ingredients. The same source
parameters work on session-owned draft previews and authenticated saved previews.
Ordinary media-share tokens cannot reveal the card's source images. Recalling the
item's share revokes both token variants; re-sharing rotates the capability.

Ready, unsaved results use `GET /api/remix/jobs/{job_id}/card`, scoped to the
owning session. With `?page=true` it renders the readable page from the job's original
tweak snapshot; without it, the optional PNG. Merely opening a preview does not save
or publish anything. Draft pages offer Save to retain privately; after saving, Publish page
creates the public link. Public links still require a saved item. After saving, the result's action
becomes Delete; Scrap only dismisses the workspace result and leaves saved items
untouched.

Saved-remix deletion is shared across authenticated Arsenal browsers, including
legacy remixes without a job record. The API relies on the same proxy Basic Auth
boundary as the gallery; public share links grant no delete access. Draft jobs
remain private to the configured owner namespace. Imported/saved library media
are global within this single private Arsenal; this is not multi-owner isolation. Deleting a referenced remix removes its files, search data,
and share capability, retaining only an unavailable source record for foreign-key
integrity. Dependent remixes are not deleted. Both media and card links stop working.

The remix page view restores existing share links, offers an inline copy field,
and revokes both media and card links through the existing recall endpoint.
Source A/B actions are always visible and do not create assignment notifications.
`PATCH /api/items/{id}/metadata` saves manually edited classification fields without
an AI request. Manual classifications take priority over subsequent AI results and
refresh the catalog search index. The full-page editor handles blocked items and
ordinary items through the same path.

The gallery's `include_hidden=true` query includes both visible and session-hidden
items. The older `archived=true` API filter remains hidden-only. Random selection
always excludes hidden items, including when favorites or inclusive browsing are
enabled.

The focused workflow and recipe tests use Playwright with mocked API endpoints.
Use the same build and test selection as CI:

```sh
scripts/run_chromium_tests.sh
```

See [browser setup](../README.md#tests) for local dependencies or forced Docker mode.
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH` can select an already-installed browser.
Screenshots are written under the ignored `tmp/` directory. Tests cover mobile and
desktop navigation, source assignment, hidden-item browsing, analysis, generation,
saved settings, draft recipe previews, saved page sharing and revocation, media
downloads, deletion versus scrapping,
compact blocked-item controls, and retrying a failed save from the gallery.

Available after Basic Auth:

- `/`
- `/arsenal-config.json`
- `/api/*`
- `/api/remix/*`
- `/media/*` for published or session-owned media only

Private:

- `/catalog/*` in production
- `/data/*`
- raw reserve database files
- Telegram sessions
- run logs
- secret/config files
- unowned dashboard-upload drafts

The FastAPI app serves the SPA shell for normal frontend routes, but keeps `/api/*`, `/api/remix/*`, `/media/*`, `/catalog/*`, and `/data/*` out of the fallback.

## API Runtime

The production app is `arsenal serve-online`.

Important endpoints:

- `GET /api/health`
- `GET /api/catalog-state`
- `GET /api/items`
- `GET /api/items/random`
- `GET /api/items/{id}`
- `GET /api/tags`
- `PATCH /api/session/items/{id}`
- `GET /api/remix/health`
- `POST /api/remix/analysis`
- `POST /api/remix/jobs`
- `GET /api/remix/jobs` (configured durable owner namespace)
- `GET /api/remix/jobs/{id}`
- `GET /api/remix/jobs/{id}/image`
- `POST /api/remix/jobs/{id}/save`
- `POST /api/remix/uploads`
- `POST /api/remix/uploads/{id}/save`
- `POST /api/items/{id}/playback-events`
- `POST /share/{token}/events`

Production `arsenal-config.json` contains only:

```json
{
  "mode": "online",
  "apiBase": "/api",
  "remixApiBase": "/api/remix"
}
```

It must not include `catalogIndex`.

Session creation and throttled heartbeats use a separate short transaction. Heartbeats skip locked session rows, so analysis and upload work cannot block gallery reads through the heartbeat dependency.

Background catalog polling merges arrivals into the loaded gallery without discarding older pages, the selected detail, or the visible scroll anchor. Tags load only after opening tags or filters; an already-loaded tag list can refresh in the background.

## Remix Worker Flow

1. The browser creates a remix job through `/api/remix/jobs`.
2. The API validates public/session media access, checks limits, stores the queued job in Postgres, and wakes Redis.
3. `arsenal online-worker` picks queued work from Redis or the database.
4. The worker records attempts, calls the configured provider, and saves the generated draft into `/reserve` for review.
5. The browser polls job state and displays the result. Explicit save retains the item privately, records lineage, and refreshes library DB indexes. It creates no external share.

The tab's source selections, tweak controls, and job ID persist in `sessionStorage`. Reloading reconnects to the existing job; it never creates another generation request. If the local job pointer was lost, the UI discovers retained jobs in the configured durable owner namespace. The configured owner ID and its existing database row govern drafts, jobs and recovery; browser pointers and signed cookies are convenience state, not storage authority. Locally dismissed jobs are not reopened. `GET /api/remix/jobs/{id}?include_sources=true` restores public or session-owned source cards, including private upload drafts; ordinary polling omits those payloads.

Content-mitigation retries apply only to safety filtering and missing-image responses. Invalid credentials, permission errors, and other provider failures terminate the content-attempt loop immediately. The HTTP client's separate bounded transport retry policy remains unchanged.

Only the worker needs provider egress for remix generation.

## Playback And Usage Diagnostics

Gallery, detail, and public-share players report bounded play, playing, stall, and error events; the gallery also reports animated-preview fallback. The API validates access, same-origin writes, field sizes, and rate limits before logging `playback_diagnostic` JSON records. These contain item ID, browser user agent, elapsed time, buffer position, native media/error states, source category, and reported H.264/AAC support. They do not include raw media URLs or share tokens. They are client-reported diagnostics, not trusted analytics.

For a mobile Edge reproduction, note the time and item, then inspect:

```bash
service_compose logs --since=10m app | rg playback_diagnostic
```

Use the `service_compose` helper defined under [Environment Variables](#environment-variables)
for an existing legacy deployment. A play event without playing, followed by an
empty-buffer stall or native error, helps distinguish a fetch/startup failure from
successful download followed by a decoder stall. No events can also indicate the
browser never ran the handler or blocked telemetry. Desktop Chromium emulation
does not prove compatibility on a physical Edge device.

Gemini responses with `usageMetadata` emit `ai_usage` JSON records containing model, duration, provider token counts, and operation/job attribution where available. Prompts, media, and API keys are excluded from these usage records. These logs are not a billing ledger or subscription-usage estimate.

## Cron Sync

Production uses the `sync` compose service instead of host cron. The service waits `SYNC_START_DELAY_SECONDS`, runs `arsenal sync-online`, then sleeps `SYNC_INTERVAL_SECONDS` seconds. The default interval is `1200` seconds. Normal import scans messages newer than the newest archived Telegram message; it does not walk backward through historical messages. Explicit skip-import and successful credit recovery can select a bounded pending backlog as described below.

`sync-online` does:

```text
Telegram import -> repair media references -> process/classify changed media -> warm recent video playback -> refresh changed public indexes
```

Normally processing and classification scope media imported or repaired by that run. With `--skip-import` or successful credit recovery, the CLI instead passes `media_ids=None` for bounded pending recovery across the library. An empty import therefore does not unconditionally rule out paid work. See [derived refresh authority](derived-data-refresh.md) for known-stale versus unknown evidence, exact embedding text and distinct message/item limits. Hosted search uses PostgreSQL full-text search, so online embeddings default off through `ONLINE_EMBEDDINGS_ENABLED=false`; offline semantic-search commands still support embeddings. Index refreshes are content-aware and scoped to changed media. Historical imports remain an explicit `arsenal sync` action. Pending-item backfills use `arsenal sync --skip-import` or `arsenal pipeline`.

Recent uncached videos are prepared incrementally according to `BROWSER_PLAYBACK_BACKFILL_BATCH_SIZE`. Operators can warm a larger bounded batch without creating a separate transcoding service:

```bash
arsenal warm-browser-playback --limit 24
```

## Gemini Credit Circuit

Gemini prepaid-credit depletion opens a persisted, provider-wide circuit after the first rejected request. While it is open, normal classification, embedding, analysis, and remix requests are rejected locally before any provider HTTP call. Telegram import, media processing, repair, and index refresh continue; remix jobs remain queued.

`sync-online` alone schedules a database-backed probe after `AI_CREDIT_PROBE_INTERVAL_SECONDS` (six hours by default). It selects configured Google classification first, then Google embedding if online embeddings are enabled, then supported Google Remix. Each probe makes one request with retries disabled. Only successful completion by the lease owner closes the circuit and starts recovery of at most `AI_RECOVERY_BATCH_SIZE` pending **messages**, with one worker per stage. Recovery honors `ONLINE_EMBEDDINGS_ENABLED`.

Classification and embedding pause independently by their effective providers. An allowed embedding stage may use existing compatible classification or manual metadata through the normal input selector while Google classification is paused. Missing or incompatible input does not authorize embedding. Unavailable circuit storage also pauses Google work, without recording a new depletion observation; other providers can continue when their own database operations are usable.

Operator commands:

```bash
arsenal ai-status
arsenal ai-resume
arsenal ai-resume --operation embedding
```

`ai-status` reports persisted state and configured operation providers without calling a model. `ai-resume` bypasses the cooldown but respects the exclusive lease. Its optional `--operation classification|embedding|remix` selects that currently configured Google operation, including local embedding when online embeddings are disabled. Unsupported selections and invalid configuration never fall back to another probe. Success certifies the selected request only, not every model or capability. When online embeddings are disabled, rerun local embedding work explicitly after recovery.

Worker-only operators use `ai-resume`; the worker does not schedule probes. Retained Remix jobs keep their captured execution identity and first-attempt boundary across recovery, using current credentials and source access. The selector does not look up or probe queued jobs; changing current configuration to mock does not select a retained Google job.

## Docker Compose Layout

For new installations use [deploy/community](../deploy/community/README.md).
Its app, worker, PostgreSQL and Redis use project-local networks, no host ports,
and optional Telegram sync. Follow its own explicit project/env-file commands.
Only its authenticated Caddy ingress publishes TCP 80/443; see the recipe's
[accepted bounded private evidence](release-preparation.md#owner-onboarding-and-documentation-acceptance--2026-11-02)
for the mapped owner walkthrough, tested identities and limits.

The retained `deploy/service/compose.yml` is an external-proxy example:

- `app`: FastAPI and SPA shell, on the operator-provided `edge` network.
- `worker`: remix worker, on private DB/Redis and egress networks.
- `sync`: Telegram sync loop, on private DB/Redis and egress networks.
- `db`: PostgreSQL 16 with pgvector, on the internal `private` network.
- `redis`: Redis 7 with append-only persistence, on `private`.

The external `edge` network and authenticated proxy must already exist. No app
port should bypass that proxy. The `meme-arsenal-app` alias is for this legacy
network; it is not a community-network prerequisite.

Choose persistent directories outside the source checkout/build context. For
example, `/srv/my-library/state/{reserve,postgres-data,redis-data}` and a separate
backup destination. The complete reserve includes originals, drafts, generated
previews, Telegram sessions and job files. Set `RESERVE_HOST_PATH`,
`POSTGRES_DATA_HOST_PATH` and `REDIS_DATA_HOST_PATH` to your actual absolute paths.
Preserve the appropriate image user's ownership; do not use world-writable data.

Bind mounts refuse to auto-create missing host directories. If storage is on a
separate disk, verify the mount before creating directories or starting Compose.
Directory existence alone is not proof of a mounted disk. For a systemd-managed
Compose unit, `deploy/service/storage.conf` is an optional template: replace its
unit name, storage path and actual mountpoint, install the drop-in for your unit,
then run `systemctl daemon-reload`. It retains both `RequiresMountsFor` and
mountpoint refusal. No unit or storage shortcuts are provisioned by this repo.
Never start with empty replacement data when recovering an existing library.

## Environment Variables

Community configuration is initialized outside the checkout by
[its initializer](../deploy/community/README.md#initialize-deliberately).
The legacy example instead uses `deploy/service/env.production.example`: copy
it to an operator-selected file such as `/srv/my-library/config/service.env`,
protect it, and adapt the paths/domains. Keep actual config and secrets out of git.

Set `SERVICE_SECRETS_DIR` in that file to an absolute directory such as
`/srv/my-library/secrets`. The legacy Compose file requires all seven files:

- `database_url`
- `postgres_password`
- `telegram_api_id`
- `telegram_api_hash`
- `public_session_secret`
- `gemini_api_key`
- `openai_api_key`

Their container destinations remain `/run/secrets/<name>`, consumed through the
existing `*_FILE` settings. Restrict the files to the operator and the container
users that need them. The community recipe uses the same names for applicable
secrets but does not require Telegram/provider files for mock upload-first use.
Do not switch recipes against an existing library without a reviewed migration.

Dashboard authentication material belongs to your proxy's protected configuration,
separate from application secrets. Configure forwarded-header trust only for the
isolated trusted proxy topology; keep app ports unpublished.
The app supports direct environment variables for local development and `_FILE`
variants for protected configuration.

Run the following legacy operations from the repository root, with the same env
file and project every time. Replace the example path before use:

```bash
export SERVICE_ENV_FILE=/srv/my-library/config/service.env
service_compose() {
  docker compose -p meme-arsenal -f deploy/service/compose.yml \
    --env-file "$SERVICE_ENV_FILE" "$@"
}
service_compose config --quiet
```

The exported variable also selects the services' `env_file`; `--env-file`
supplies Compose interpolation. Avoid inherited settings from another instance.

Credit-circuit settings:

- `AI_CREDIT_PROBE_INTERVAL_SECONDS`: delay between automatic probes while paused; default `21600`.
- `AI_CREDIT_PROBE_LEASE_SECONDS`: abandoned-probe lease timeout; default `300`.
- `AI_RECOVERY_BATCH_SIZE`: maximum pending messages processed immediately after recovery (retaining actionable media within each selected message); default `5`.

## First Deploy

Follow the [community core guide](../deploy/community/README.md) for a new empty
instance, including storage ownership, mock/provider configuration and explicit
Telegram opt-in. That guide records the verified bootstrap, authenticated ingress,
owner identity, sharing and exact-image recovery procedures. The
[release preparation evidence](release-preparation.md) records the remaining
public-release gates and the limited development-schema upgrade proof. The
[self-hosting decision](self-hosting-decision.txt) and
[owner decision](owner-identity-decision.txt) define the release contract.

A fresh install has no SQLite archive to migrate. An existing local archive can
be considered separately for `arsenal migrate-sqlite-to-postgres`, using a
consistent backup and an isolated migration rehearsal. Do not run forced migration
or draft-promotion flags as part of ordinary first setup.

## Update Deploy

This legacy section is an outline requiring reviewed adaptation, not an executable
upgrade recipe. Use the [community maintenance boundary](../deploy/community/README.md#deliberate-credentialconfig-changes)
for that topology. Before any migration or config edit, suspend external writers,
record exact container IDs/images and prior running roles, withdraw ingress, wait
for startup/TERM-handler readiness, and prove each writer's normal graceful exit
with the same-token API `drained` evidence. Keep DB/Redis running. Never use finite
Compose stop or forced recreation to drain active writers. Pending or abnormal
exits require investigation with ingress off.

Take a complete private recovery backup and retain exact images before upgrading.
Only after verified drainage may a reviewed one-shot migration run. On failure
keep writers and ingress off for recovery. On success restore only previously
running roles, verify app readiness and assets, and restore ingress last. Leave
stopped roles stopped. The legacy helper below does not establish this contract;
arbitrary historical upgrades and downgrades remain unverified.

If public metadata changed manually, refresh its indexes deliberately:

```bash
service_compose run --rm --no-deps app arsenal refresh-public-indexes
```

## Health Checks

```bash
service_compose ps
service_compose logs --tail=100 app
service_compose logs --tail=100 worker sync
service_compose exec -T app curl -fsS http://localhost:8000/api/health
```

Validate and check your proxy with its own documented tooling. Replace the example
host in these unauthenticated external checks:

```bash
curl -I https://library.example.net/
curl -I https://library.example.net/api/health
```

Unauthenticated responses should be `401`. Through authenticated ingress,
`/api/health` should report `ok`, `database_ok`, and `redis_ok` as true;
`/catalog/index.json` should return `404`. Also verify dashboard assets and raw
media protection: API health alone does not prove frontend or ingress readiness.

## Backup

The retained helper is `scripts/service_backup_postgres.sh`. It does not quiesce
writers or include external signing/authentication configuration. Record prior
process state and gracefully drain app, worker, enabled sync and operator CLI
writers before capture, with ingress withdrawn and exact-ID/readiness/normal-exit
evidence as above; keep PostgreSQL running. Resume only previously running services after
success or failure, once safe. This legacy helper is incomplete. The accepted
bounded community procedure includes protected configuration, exact-image restore
and restored activation; see [evidence and limits](release-preparation.md).

For a prepared legacy instance using the env file above and a chosen backup root:

```bash
COMPOSE_PROJECT_NAME=meme-arsenal COMPOSE_FILE=deploy/service/compose.yml \
  BACKUP_DIR=/srv/my-library-backups \
  scripts/service_backup_postgres.sh "$SERVICE_ENV_FILE"
```

The first argument selects the env file; `COMPOSE_FILE` selects the recipe.
The default `state/backups` is relative to the working directory, not a guaranteed
mounted or separate disk. Choose and check backup storage explicitly.

The helper produces a custom-format PostgreSQL dump (`pg_dump -Fc`), a reserve
tarball, listing checks and SHA-256 checksums. Listing validation does not prove
restoration or cross-component consistency. Protect signing/authentication and
other required configuration separately. Missing reserve can produce DB-only
output; do not treat that as a complete backup. `BACKUP_RESERVE=0` explicitly
requests DB-only output; `BACKUP_RETENTION_DAYS` opts into deleting older
script-created backups. Exported catalogs are not recovery backups.

Preview stale online-state cleanup separately before choosing to apply it:

```bash
arsenal cleanup-online-state
arsenal cleanup-online-state --apply
```

## Restore

Rehearse restoration into an empty, isolated instance before relying on a backup.
Keep writers stopped and ingress disabled. Verify checksums, restore the reserve
tarball into the chosen reserve path with the correct ownership, and restore the
separately protected config/signing material. Never overwrite a live library.

For the custom-format dump, use `pg_restore`, not SQL input to `psql`. With the
target database empty and running, adapting the path and user/database if changed:

```bash
service_compose exec -T db pg_restore --exit-on-error \
  -U meme_arsenal -d meme_arsenal < /srv/my-library-backups/selected.dump
service_compose run --rm --no-deps app alembic upgrade head
```

Use fresh Redis on disaster recovery as specified in the deployment decision;
pending-job recovery in this legacy outline has not been verified. Accepted
community evidence separately covers exact-image/owner/media/ready/saved/queued
boundaries and bounded worker activation, plus development-schema 012→013 proof.
Validate media, metadata, jobs, identity and share state before restoring ingress
and intended services. This legacy outline is not that accepted full restore drill
and establishes no arbitrary historical upgrade, downgrade or power-loss support.

## Rollback

Record the running source/image version before an update. Roll back application
images only if the database schema remains compatible. From the checkout, replace
`KNOWN_GOOD_COMMIT` with the retained compatible revision before running:

```bash
git log --oneline -5
git checkout KNOWN_GOOD_COMMIT
service_compose build
```

Then recreate only the previously enabled services and verify health. If rollback
crosses an incompatible migration boundary, restore the matching PostgreSQL and
reserve/config backup into a recovery instance instead of relying on Alembic
downgrade. Preserve the failed instance for recovery until validation succeeds.

## Community backup boundary

The legacy `scripts/service_backup_postgres.sh` interface above produces incomplete
backups without writer drainage or protected configuration. Community operators
must use `scripts/backup_community.py` and the maintenance procedure in
`deploy/community/README.md`; database dumps alone cannot recover a library.
