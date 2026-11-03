# Community private HTTPS deployment

Self-host a private meme library: upload an image, save it, edit its description,
and find it again after reopening the browser. This source-built recipe runs the
API/dashboard, worker, PostgreSQL 16/pgvector and Redis 7/AOF behind Caddy HTTPS.
Telegram and paid providers are optional; the recommended first run uses mock AI.

- [Upload-first setup and prerequisites](#upload-first-setup)
- [Initialize](#initialize-deliberately), [configure/check](#configure-and-check),
  [start the UI](#start-and-open-the-installed-ui), [first use](#normal-operation)
- [Optional providers](#provider-capabilities), [Telegram](#explicit-telegram-opt-in),
  [sharing](#sharing-migration-notice)
- [Owner recovery](#existing-library-owner-recovery), [maintenance](#deliberate-credentialconfig-changes),
  [backup](#consistent-private-maintenance-backups), [restore](#restore-to-a-disposable-empty-instance)
- [Trust reference](#trust-and-ingress-reference), [storage reference](#storage-and-installed-assets-reference),
  [bounded development rehearsal](#development-schema-recovery-rehearsal)

## Upload-first setup

Follow the phases below in order for a **new instance only**. First success is
an uploaded, privately saved item found by its edited description after reload.
This is the primary owner guide for the v0.1.1 source release. See
[release evidence](../../docs/release-preparation.md) and
[verification](verification.txt) for tested snapshots and limits.

### Prerequisites

This guide starts with an administered host, storage and domain. It does not
cover acquiring a server, installing Docker, or provisioning mounts, DNS and
firewall rules. Establish the prerequisites below before initialization; stop
if you cannot verify them.

- Obtain a reviewed release from
  [the public source repository](https://github.com/OmarSarhan/meme-arsenal-community).
  Work from that checkout's root (containing `Dockerfile` and `deploy/community`).
  Record the selected revision with `git rev-parse HEAD`.
- Use Linux amd64, rootful Docker Engine, Compose **2.30+** (raw env files), and
  Python **3.11+** for the standard-library initializer; no host Python packages
  are needed. The operator must be authorized to use Docker and administer the
  storage parent, protected files and container-owned data. Docker privileges
  are administrative privileges. Do not solve permissions with `chmod 777`.
- Verify the intended storage filesystem is mounted and has capacity. Choose a
  new **absent absolute instance path outside every checkout/build context**.
  Its parent must already exist and be writable by the operator. Directory
  existence alone does not prove a mounted disk. Plan private backup storage
  outside the instance and checkout before trusting uploads to this library.
- Replace examples consistently: `/srv/my-library` is the instance,
  `my-library` the explicit Compose project, `library.example.net` your real
  hostname, `meme-arsenal-community:local` a local source-built image tag, and
  `/srv/library-owner.hash` a new protected hash-paste file. Choose an unused
  project. Keep the same selections in every command and configuration file.
- Allow image and dependency fetches for pinned base/service images and the
  source build. The hash image must be available locally or fetchable too.
- Point real DNS A/AAAA records directly at the host; remove unusable AAAA
  records. TCP 80/443 must be free and reachable. Public ACME issuance/renewal
  needs inbound challenge reachability and outbound DNS/HTTPS. This recipe uses
  standard HTTPS 443, without an IP literal, CDN or another reverse proxy.
  `.invalid` fixture names are not real public certificate names.

Only Caddy publishes ports 80/443. **Never publish the app port**: the app does not
itself authenticate the owner. HTTPS Basic Auth is the required ingress boundary;
a cookie is not login, and a secret owner ID is not a substitute for authentication.
Do not share project networks with untrusted containers. Sharing starts disabled;
read [privacy](../../docs/privacy.md) before uploads and before enabling live AI.
Keep the entire instance and backups private, including secrets, drafts and TLS state.
Private library identity uses one persistent owner namespace, independent of
cookies, signing keys and Basic credentials. Read the
[sharing transition](../../docs/opt-in-sharing.md) before upgrading or enabling
the optional public host; without that host, signed links remain behind Basic Auth.

## Initialize deliberately

Generate a bcrypt hash with the same pinned official Caddy image used by Compose.
Docker may first fetch this image; `--network none` isolates the hash-container
runtime, not that fetch. In an interactive terminal, this prompts twice without
echoing the password:

```sh
docker run --rm -it --network none \
  caddy:2.11.7-alpine@sha256:e70b164da81ef698b3ad19af799ea6086ce6d9e48ed4079eb9fe26f6fbf14ee5 \
  caddy hash-password --algorithm bcrypt
```

Copy only the resulting `$2a$...` bcrypt hash into a new protected bare hash file.
For example, run the following in a terminal with access to `/srv`, paste the hash
when prompted, and do not paste the password or terminal transcript:

```sh
umask 077
python3 - <<'PY'
from getpass import getpass
from pathlib import Path
value = getpass("Paste only the bcrypt hash: ")
with Path("/srv/library-owner.hash").open("x") as target:
    target.write(value + "\n")
PY
```

Expected result: the protected file contains only the hash and a final newline,
with mode 0600 from `umask 077`. Stop if either interaction fails; do not initialize
with a password, prompt text or an incomplete hash file.

Do not redirect Docker's `-t` output into the hash file: PTY output includes
prompts. Do not use `--plaintext` or put a password in argv/environment/history.
The pinned command also accepts newline-terminated stdin without a TTY (used
only with synthetic credentials by CI). Its source semantics and image provenance
are recorded in [verification](verification.txt).

From the repository root, replace the paths, hostname, username and image tag:

```sh
python3 deploy/community/init.py /srv/my-library \
  --site-url https://library.example.net --image meme-arsenal-community:local \
  --auth-user owner --auth-hash-file /srv/library-owner.hash
```

The initializer refuses existing directories and paths within this checkout;
it never rotates secrets, touches an existing library, or starts services.
It creates directories mode 0700 and files mode 0600, owned initially by the
invoking operator. It generates matching PostgreSQL password/connection files,
a persistent random session signing secret, and a separate candidate owner ID, without printing their contents.
It validates the origin, safe username and single bcrypt hash before creating
anything. Explicit `:443` and a trailing slash normalize to the same lowercase
HTTPS origin. The lowercase username `import` is reserved and rejected.
The username/hash are copied literally into protected
`config/ingress/owner.caddy`, separate from app env/secrets. Caddy reads this
explicit import; it does not implement the application's `*_FILE` convention.
If initialization is interrupted, inspect the partial directory before retrying;
do not rerun against existing data. Keep config and secrets outside the checkout.

Expected result: a new private instance with empty storage, generated secret
material and a candidate owner file. This has not created a database or durable
owner row. Stop on initializer failure and inspect any partial directory; for
existing data use [owner recovery](#existing-library-owner-recovery).

## Configure and check

Review `/srv/my-library/config/app.env` before building. Keep the generated
`AI_PROVIDER=mock`, blank `CLASSIFICATION_PROVIDER`, `EMBEDDING_PROVIDER` and
`REMIX_PROVIDER`, `ONLINE_EMBEDDINGS_ENABLED=false`, and
`PUBLIC_SHARING_ENABLED=false`. Do not add live `*_FILE` overrides. A fresh
instance has no retained live jobs. No provider or Telegram files are needed.
Mock permits upload/save with deterministic metadata. Analyze is disabled and
Remix generation is unavailable. **Create Remix** appears only after both source
slots have items and remains disabled under mock.
This is the full HTTPS/PostgreSQL/Redis/worker app, distinct from the
[limited SQLite/static demo](../../docs/development.md#no-account-local-demo).

Use an explicit project and env file for **every** command; avoid inherited
Compose variables from other deployments. `config/compose.env` contains only
paths and the local image tag. `app.env` is read literally (no `$` interpolation).
Keep secrets in files, not literal env settings. Use `config --quiet` for validation
so resolved configuration is not printed.

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml config --quiet
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml build app
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml run --rm --no-deps app --check
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml run --rm --no-deps caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

Preflight uses the existing settings loader and `*_FILE` contract. It refuses
missing/empty/unreadable configured secret files, insecure production settings,
invalid core DB coordinates and missing provider keys, naming settings without
printing values. It runs before each app/worker/sync process. Compose separately
rejects missing env files and bind sources. File validation is not a DB connection
test, migration test, or proof that Telegram authorization is still valid.

Expected result: all four commands exit successfully. Stop on failure; do not
start the stack with failed preflight or Caddy validation.

### Migrate and provision the durable owner

Before the first full-stack start, provision the database explicitly. The initializer
only creates a candidate file; it does not select or create a database row. Use the
same explicit project/env/manifest on every command:

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml up -d --wait db redis
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml run --rm --no-deps -e ARSENAL_INSTALL_DASHBOARD=1 app alembic upgrade head
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml run --rm --no-deps -e ARSENAL_INSTALL_DASHBOARD=0 app arsenal owner initialize
```

Expected result: healthy DB/Redis, schema at head, and the configured owner ID
verified as a durable database row. Full-stack startup refuses an absent owner
row; repeating owner initialize verifies the same ID. Stop if any command fails.
Do not use fresh initialization to recover an existing library; see
[owner recovery](#existing-library-owner-recovery).

## Start and open the installed UI

After all preceding commands succeed, start the instance:

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml up -d --no-build
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml exec app curl -fsS http://localhost:8000/api/health
```

The API waits for DB/Redis health, installs the built dashboard via the existing
entrypoint, then runs `alembic upgrade head`. The worker and enabled sync wait for
API health; Caddy also starts after API health. That internal health route checks
DB/Redis, not frontend completeness. Open `https://library.example.net` and use the
configured Basic username and original password, not the stored hash. Port 80
redirects to HTTPS (and serves ACME challenges), not app content. Never send Basic
credentials over HTTP. Container
logs are bounded to 10 MiB × 3 per container (upstream provenance in the deployment
decision); this does not rotate application files inside reserve.
Expected result: health succeeds and the HTTPS dashboard loads after authentication.
If startup or health fails, inspect the failure before proceeding; do not bypass
authentication or publish the app port to diagnose UI access.

## Normal operation

Use a valid PNG or JPEG under 1 MiB that you have rights to store for the first
check. This is a small test input, not the maximum: `PUBLIC_UPLOAD_MAX_MB` defaults
to 128 (128 MiB of file bytes). Larger or other media are unnecessary for this
first check. Keep it disposable until you have completed a private backup.

1. Open the HTTPS site, authenticate with the configured Basic credentials, and
   choose **Remix** in the navigation.
2. Upload the image into the **first file slot**. Choose **Open a remix source
   details**, inspect the draft, then **Save to arsenal**.
3. Choose **Gallery** in navigation and open the exact item you just saved.
   Choose **Edit details**, set Description to a distinctive word such as
   `myvioletmeme`, and choose **Save changes**. Wait for **Saved**, then **Back**.
4. In **Search archive**, try an unrelated word (no matching item), then your
   distinctive word (the saved item). Gallery search is text/prefix search.
5. Reload the page, repeat both searches, reopen that item and **Edit details**:
   the description must still be present.

Expected result: the uploaded item and edited description persist after reload.
Stop and investigate failed upload/save, missing assets, search or persistence
before adding optional services. Health alone is not asset or UI proof. Mock
metadata and vectors do not demonstrate meaningful AI or semantic search quality.

Saved means retained in your authenticated private library, not published to the
internet. Sharing remains disabled: saving creates no public share URL. Backups
must include private drafts as well as saved media. Plan backup storage before
setup, then complete a [private backup](#consistent-private-maintenance-backups)
after startup succeeds and before relying on this library; the first-use steps
do not create one. Unsent edits and old browser pointers can remain local. To
recover retained work from another authenticated browser, open **Remix**, click
**Uploaded drafts** or **Retained jobs**, then choose an entry in the matching
**Select retained work** menu. Use **Load older** if offered. Cookie loss does not
discard the configured durable owner namespace.
See [owner recovery](#existing-library-owner-recovery) if the owner file is lost.

After this works, configure [providers](#provider-capabilities),
[Telegram](#explicit-telegram-opt-in), or [deliberate sharing](../../docs/opt-in-sharing.md)
only if wanted. Provider changes can send media and metadata outside your host.

## Provider capabilities

Keep mock AI for the first upload/save check. Enable live operations only after
reviewing the data they send and the models and credentials they require.

For optional live operation, put the provider key in a mode-0600 file under the
instance's `secrets` directory and select compatible models and the corresponding
`*_FILE` setting. These files are shared by application roles; do not put unrelated
credentials there. Use [graceful configuration maintenance](#deliberate-credentialconfig-changes)
for an existing running instance.

`AI_PROVIDER` supplies the default for all AI stages. `CLASSIFICATION_PROVIDER`,
`EMBEDDING_PROVIDER` and `REMIX_PROVIDER` independently override it (including via
`*_FILE`). Selected null/blank values inherit; an empty environment value is absent
at its layer, while whitespace can clear a lower-layer override to inheritance.
Model settings do not select providers or change automatically.
Read the [upload-time AI privacy disclosure](../../docs/privacy.md)
before uploading with a live provider: upload classification sends media/metadata.

| Effective operation provider | Upload classification | Text embeddings / CLI semantic search | Remix analysis and generation |
| --- | --- | --- | --- |
| `mock` | Local deterministic fixture metadata | Seeded pseudorandom vectors; no meaningful semantic quality | Unavailable |
| `openai` | OpenAI Responses API | OpenAI embeddings | Unavailable, even with a Google key |
| `google` | Gemini | Google embeddings | Requires a nonblank resolved Google key and compatible models |

The online gallery uses text/prefix search, independently of CLI semantic search.
`ONLINE_EMBEDDINGS_ENABLED=false` is the default; enabling it does not backfill
uploaded media or turn gallery search into semantic search. Missing live credentials
do not silently select mock or make CLI search fall back to text only.

For Google, explicitly set `AI_PROVIDER=google`, `AI_MODEL=gemini-2.5-flash`,
`EMBEDDING_MODEL=gemini-embedding-001`, `EMBEDDING_DIMENSIONS=768`, and
`REMIX_MODEL=gemini-3.1-flash-image`, with `GEMINI_API_KEY_FILE` pointing to the
protected key file, as in [the configuration example](app.env.example).
Changing only the provider retains OpenAI-oriented model defaults. These model
names are source examples, not verified vendor availability promises. Community
preflight checks keys for classification, enabled online embeddings and supported
Google Remix, plus every declared broken secret file even for inactive operations.
It does not require unused global-provider credentials. For example, OpenAI
classification with `REMIX_PROVIDER=google` uses the OpenAI classification model
and explicit Google Remix model/key independently; embeddings keep their own
selection. Global mock with live overrides is not offline.
Remix health checks local configuration presence, not remote validity or quota.
Retained Google jobs keep their captured generation configuration and can still
dispatch with current credentials after switching the global provider to mock.
Stop dispatch through the graceful maintenance boundary when changing live work;
an in-flight provider request cannot be cancelled by changing settings.

## Existing-library owner recovery

For an existing library, first take a consistent backup with
all writers stopped using the procedure below. Then run read-only inventory:

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml run --rm --no-deps -e ARSENAL_INSTALL_DASHBOARD=0 app arsenal owner inventory
```

Inventory works with missing/conflicting owner configuration, uses a read-only DB
transaction, installs no assets and creates no schema/layout. Migrate first if
schema is missing, using the one-shot `alembic upgrade head` command above with
`ARSENAL_INSTALL_DASHBOARD=0` when recovering a lost owner file. Other
production/provider/storage/secret checks still apply.
Inventory escapes IDs and flags unsupported legacy representations. Identify one
namespace explicitly; write its exact ID to a protected temporary host file and
atomically replace `secrets/owner_session_id` (mode 0600). The supported UTF-8 ID is
nonempty, at most 64 characters, with no NUL/CR/LF or boundary whitespace; one
terminal newline is allowed. Case, punctuation, Unicode and internal spaces are
preserved. Never trim an unsupported legacy ID into a different namespace.
Then run the same one-shot command with `arsenal owner adopt` to verify that the
row exists. Adoption copies or rewrites no records. Foreign/null jobs and global
curation remain separate. To deliberately select a new empty namespace in an
existing library, initialize requires `--allow-empty-namespace`.

If the owner file is lost, inventory and explicitly adopt the retained namespace;
do not rerun the new-instance initializer. A declared missing/empty/unreadable
file cannot fall back to inline configuration; conflicting supplied values fail.
Keep the secrets directory mounted read-only. Normal startup never writes IDs.
The whole protected-tree backup already captures the owner file.

Signing-key K1→K2 rotation invalidates signed cookies and share URLs but leaves
owner data intact; active DB short aliases survive. Recall unwanted shares
explicitly. Basic credential reset is independent. Two authenticated browsers,
including browsers with cleared cookies/storage, recover the same saved curation,
standalone drafts and retained jobs through the workspace selectors. Unsent edits
remain local. Saving adds to the private library without creating a share.

## Deliberate credential/config changes

Provider/key/model changes require the graceful writer maintenance boundary in
[sharing maintenance steps 1–3](../../docs/opt-in-sharing.md): finish backup/recovery,
suspend external writers, record exact IDs and prior running states, keep DB/Redis,
withdraw ingress, wait for installed TERM handlers, signal each running writer
once and prove normal exit (including same-token API `drained` evidence).
Pending or abnormal drainage requires investigation before any config edit.
Only then edit protected provider/key/model settings, run the configure/preflight
checks above, and recreate only previously running roles using the readiness and
ingress-last order in step 5. Leave stopped roles stopped. Provider-only changes
do not execute sharing-specific steps 4/6. Never force-kill or use finite Compose
stop to drain active writers. Backup recovery resumes old exact containers and
does not apply new configuration. Basic credential reload has its distinct
procedure below.

Caddy starts from `/etc/caddy/Caddyfile`, never `--resume`. Its admin endpoint
stays on container loopback `localhost:2019`, with no published/admin proxy route.
Use operator `exec` for validation/reload. Do not print `caddy adapt` output into
logs: the adapted JSON contains the password hash.

To change credentials, generate a new hash using the pinned command above. Keep
a protected copy of the old credential file, prepare a mode-0600 replacement
containing exactly `username bcrypt-hash` and a newline, and atomically rename it
to `config/ingress/owner.caddy`. The directory mount makes the replacement visible.
Validate, then deliberately reload:

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml exec -T caddy \
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml exec -T caddy \
  caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
```

Run reload only after validation succeeds. A failed reload can retain the old
gated configuration; restore the valid file before restarting. Missing credential
imports fail startup. Check correct credentials work and missing/old credentials
fail after a successful change. Browsers cache Basic credentials; there is no
reliable app logout button. Credential changes leave DB/signing keys and session
ownership unchanged. Do not rerun the initializer for rotation or existing OMA-18
instances; those need deliberate config/path provisioning and review. Changing
the hostname requires coordinated app origin, Caddy configuration and DNS changes.

## Explicit Telegram opt-in

Only after core readiness, create mode-0600 `secrets/telegram_api_id` and
`secrets/telegram_api_hash`. Perform the existing interactive login deliberately
against the same reserve (this command contacts Telegram):

```sh
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml --profile telegram run --rm --no-deps \
  -e ARSENAL_TELEGRAM_SYNC=0 sync arsenal telegram-login
docker compose -p my-library --env-file /srv/my-library/config/compose.env \
  -f deploy/community/compose.yml --profile telegram up -d --no-build sync
```

The one-shot login bypasses only the preexisting-session requirement; credentials
are still checked as configured files. Sync startup requires credentials and a
persistent `data/telegram.session`. The loop runs every 20 minutes, marks failure
unhealthy, and uses a 45-minute success freshness window. Its stdin is closed so
an absent/expired authorization cannot trigger interactive login at boot. A saved
file does not guarantee valid authorization: stop sync and repeat deliberate
login if it expires. Default `up` does not create sync. To disable an already
running sync, explicitly `--profile telegram stop sync`; omitting the profile
does not stop an existing container.

See [verification](verification.txt) for actual checks and unresolved evidence.

The targeted [community CI workflow](../../.github/workflows/community.yml) runs
[a disposable smoke check](../../scripts/verify_community.py) on relevant changes.
It builds one app image and exercises verified HTTPS Basic Auth using Caddy's
fixture-only CA, real dashboard/assets/uploads/media/ready jobs, denial routes,
Origin CSRF rejection, proxy trust and recreation of all five containers. It
checks DB/Redis/signing/TLS-state persistence and a missing-credential reload.
Runtime networks have no outbound access; `ports: !reset []` removes the base
Caddy publications. Both effective Compose config and runtime PortBindings are
checked. Only the fixture CA certificate reaches the client, with hostname/SNI
verification enabled; no host CA installation or TLS-verification bypass.
It uses root only to clean its temporary bind data owned by container users.
The disposable drill verifies retained owner recovery and explicit sharing;
real provider behavior, Telegram sync, public ACME reachability and video decoding
remain outside that proof.
## Consistent private maintenance backups

Run the standard-library host helper from the source checkout. First verify the
instance and backup storage mounts yourself; existing directories cannot prove a
disk is mounted. Create the backup destination deliberately outside both the
instance and checkout. Use the exact project and ordered Compose files that
created the containers, including any network override:

```sh
python3 scripts/backup_community.py --instance /srv/my-library \
  --project my-library --backup-dir /srv/my-library-backups \
  -f "$PWD/deploy/community/compose.yml" --operator-writers-stopped
```

The assertion means manual imports, migrations, cleanup, Telegram login, dashboard
exports/media warming, direct SQL/configuration edits and scheduled external
writers have finished normally and will remain suspended until recovery finishes.
This includes internal folder intake (see [source ingestion](../../docs/source-ingestion.md));
its atomic file rename does not coordinate with backups.
Do not deploy or run other Compose operations during this interval. The stable
instance `.backup.lock` excludes cooperating backups (even at different backup
destinations); it cannot exclude arbitrary SQL or CLI writers. Existing project
one-offs must finish first. DB/Redis must already be healthy; running app startup
(bootstrap, migration and server readiness) must already have completed.

The helper records exact prior IDs, drains Caddy then app/worker/existing sync
with one TERM each, and observes for `--drain-timeout` seconds (default 300).
There is no forced stop. A timeout leaves pending stop intent: the process may
exit later. No backup is captured until all required drains are proved. The API
uses private process-token evidence after request/dependency completion; worker
finishes active work and explanation threads; sync waits for its importer.

Complete sets contain a custom-format logical PostgreSQL dump, the **whole**
reserve (including private drafts and Telegram session), complete config/secrets,
Caddy certificate/state trees, and ordered recipe contents. Future protected
configuration files are included automatically. Keep every set private, including
reserve archives and TLS/signing/authentication material. Files/directories begin
at 0600/0700. Symlinks and special files are unsupported; ordinary hardlinks are
preserved. Raw PostgreSQL/Redis directories are excluded; disaster recovery uses
fresh Redis. The capacity guard is apparent protected-tree/reserve bytes plus
1 GiB, a lower bound which cannot predict dump size or guarantee against ENOSPC.

Dump listing, full archive reading and SHA-256 checks precede exact-ID recovery.
Only previously running containers are started, without recreating containers or
starting dependencies. Ingress waits for a ready app. Only successful validation
and required recovery publish `community-<id>/complete.json`. A failed operation
returns nonzero and publishes no complete set. `--retention-days N` is opt-in and
removes only recognized older complete community directories; a retention error
reports the already completed backup separately.

On ordinary failure or INT/TERM the helper waits for its current capture reader
before recovery. It retains `.backup-recovery.json` for unresolved obligations
and prints recorded `docker inspect <id>` / `docker start <id>` commands. Inspect
the recorded IDs, mounts, image/start identity, exit status and API drain proof
first; a pending TERM is not evidence that a service is restored or corrupted.
Clean services resume independently when another same-process writer is still
draining; that writer's pending intent remains recorded, even while it is running.
A late exit can resume only after normal drainage is verified. Ingress stays
stopped while the app has pending drainage or recovery, even if its last Docker
health result was healthy. For remaining clean obligations, start the recorded
app, verify readiness, start only previously running worker/sync, then Caddy.
Abnormal/unknown writer exits, bootstrap failures or shared-data uncertainty
need investigation before restarting writers. Do not
replace these steps with `compose up`. After explicit reconciliation, remove
the recovery record yourself; the next invocation refuses a stale record.

SIGKILL/host power loss cannot run cleanup. Intent records use atomic replacement
and fsync, but backup publication is not a promise of power-loss durability. An
unadvertised partial set is incomplete. Listing/checksum validation does not prove
pre-existing DB/file consistency or replace a restore drill. The exact-image
restore and development-schema recovery procedures below define the tested
recovery boundary, including retained owner identity. The legacy
`service_backup_postgres.sh` is explicitly incomplete and is not this procedure.
## Sharing migration notice

Before upgrading an existing database, read [the opt-in sharing transition](../../docs/opt-in-sharing.md).
Migration 013 recalls every legacy share, including output links. Back up first;
deliberate republication creates new links. Sharing remains disabled by default.

## Plan an upgrade

The new-instance setup is not an existing-library upgrade command. Read the
target release notes and migration changes before choosing an upgrade path.
The evidence below covers one development-schema transition, not arbitrary
released-version upgrades or downgrades.

1. Record the current source revision, exact app/DB images, project, ordered
   Compose files and running roles. Retain the images and matching source as
   described under restore.
2. Take a complete private backup and verify its completion. The backup helper
   resumes previously running services; it does not leave an upgrade window open.
3. Before changing images, configuration or schema, repeat the
   [graceful maintenance boundary](#deliberate-credentialconfig-changes). Keep
   ingress off and external writers suspended until the upgrade is validated.
4. Apply only the target release's reviewed migration and configuration steps.
   On failure, leave writers and ingress off and investigate. An older app image
   is usable only with a compatible schema; do not assume Alembic downgrade works.
5. Restore only previously running roles, verify app health, dashboard assets,
   media and owner data, then restore ingress last. If recovery is needed, use
   the matching backup/images in a new empty instance and review restored shares
   before enabling public access.

## Restore to a disposable empty instance

This is private disaster recovery, not `export-catalog` browsing export. Use
`scripts/restore_community.py` from the reviewed matching-source checkout. It
accepts only a recognized complete v1 set, an **absent** absolute instance path
with an existing canonical parent, and an unused explicit local Compose project.
It never overwrites, merges, repairs, chooses an owner, migrates, or starts the
application. All source deployments and backup inputs remain unchanged.

Before disaster, retain the actual running app and DB images separately from
backup retention. Record each backup's association with image IDs, Linux amd64
platform, source commit and ordered recipes. For example, with the IDs recorded
in `complete.json`, deliberately retain operator artifacts outside the backup root:

```sh
docker image inspect sha256:APP_ID sha256:DB_ID > /secure/recovery/images.json
docker image save -o /secure/recovery/runtime.tar sha256:APP_ID sha256:DB_ID
sha256sum /secure/recovery/runtime.tar > /secure/recovery/runtime.tar.sha256
git rev-parse HEAD > /secure/recovery/source-revision.txt
```

Retain the matching source checkout and recipe context, checksum files and backup
association privately. These commands are operator prerequisites, not automatic
backup changes. On the recovery host verify the artifact checksum, then:

```sh
sha256sum -c /secure/recovery/runtime.tar.sha256
docker image load -i /secure/recovery/runtime.tar
docker image inspect sha256:APP_ID sha256:DB_ID
docker image tag sha256:APP_ID arsenal-recovery:retained
```

Compare IDs/platform to the recorded backup. A retained upstream DB digest may
supply its image while available, but the loaded ID must still match. Tags,
same-source rebuilds and matching schema numbers do not prove identical images.
Old sets without recoverable exact images cannot use supported automated recovery;
there is no compatibility fallback. App image identity, matching-source recipes
and schema head are separate requirements.

Use supported rootful Linux amd64 Docker, local `/var/run/docker.sock`, Compose
2.30+ and stdlib Python. Verify storage mounts and available Docker/target space
yourself. The helper checks expanded archive bytes plus 1 GiB only; DB expansion,
indexes and WAL are unestimated and ENOSPC can still occur. Keep trusted backup
inputs immutable throughout the operation; this is accidental-escape protection,
not a hostile SQL/config sandbox. Exclude manual writers and concurrent deployment
operations. Supply reviewed recipes at their proper source locations, including
reviewed custom overrides. Numbered captured recipes are evidence and are never
executed. Unsupported layouts must be resolved explicitly before restoring.

```sh
python3 scripts/restore_community.py --backup-set /srv/backups/community-SET_ID \
  --instance /srv/recovered-library --project recovered-library \
  --image arsenal-recovery:retained -f "$PWD/deploy/community/compose.yml" \
  --operator-writers-stopped
```

The helper checks checksums, sizes, roots, regular files and contained backward
hardlinks before private extraction. It preserves exact config, secrets, signing,
owner and proxy bytes; it creates only `restore.env`, `restore-safe.json` and a
private `restore-report.json` alongside them. It requires paired project-local
DB credentials without URL connection overrides. Missing complete-set material
cannot be bypassed by generating replacement keys or adopting another owner.
Lost-file inventory/adoption remains the separate owner procedure above.

Only the new DB starts (`--no-deps --no-build --pull never`). After proving an
empty catalog it consumes the dump via checked stdin `pg_restore` with
`--single-transaction --exit-on-error --no-owner --no-privileges`. It does not
create vector, run migrations or initialize schema/owner beforehand. Read-only
postchecks require the matching schema head, vector and exact configured owner
row. Nonempty retained originals, including unpublished drafts, and usable
ready/saving/saved job outputs must exist inside the restored reserve. Missing
derivatives are reported as regenerable omissions; unavailable tombstones/NULL
states remain preserved. No files are repaired and no AI output regenerated.
Detailed identities stay in the private report; normal output contains counts.

Success requires a verified clean stopped DB. Redis storage is new and empty;
cache/queue hints and rate limits reset, while SQL jobs remain. No app, worker,
sync or ingress starts. INT/TERM latch until the owned child is reaped; a restore
may already commit before cancellation, so cancellation means unvalidated, not
rollback. Failure retains target identity/phase and an honest possibly-running
status if shutdown cannot be proved. Inspect the recorded exact DB ID and mounts
before explicit target-only cleanup. If start failed before its ID was captured,
inspect only the recorded project's DB. Never prune globally or restart source
services. Retry with another absent path/project, or explicitly clean only the
recorded disposable target. SIGKILL/power loss has no cleanup guarantee.

For deliberate **private** activation, keep the generated overlay in every check
and start. This shell function fixes the selection (run from matching source):

```sh
recovery_compose() {
  docker --host unix:///var/run/docker.sock compose -p recovered-library \
    --env-file /srv/recovered-library/restore.env \
    -f "$PWD/deploy/community/compose.yml" \
    -f /srv/recovered-library/restore-safe.json "$@"
}
recovery_compose run --rm --no-deps --pull never app --check
recovery_compose up -d --no-deps --no-build --pull never --wait db redis
recovery_compose run --rm --no-deps --pull never app arsenal owner adopt
# Before starting app, deliberately change only the generated overlay's app
# ARSENAL_INSTALL_DASHBOARD to "1". Its migration must be a no-op at the verified
# matching head. Never use this as an upgrade command.
recovery_compose up -d --no-deps --no-build --pull never app
```

The overlay selects mock AI, disables embeddings and `PUBLIC_SHARING_ENABLED`,
and disables automatic dashboard installation in one-shots. To install dashboard
assets for the deliberate app start, review changing only the generated overlay's
app `ARSENAL_INSTALL_DASHBOARD` to `1` first; the one-shot `--check` itself installs
no assets. Review optional protected file references, original origins, bind paths
and effective preflight before activation. Preserve original `config/app.env` as
evidence. On app boot failure stop this target app explicitly using
`recovery_compose stop app`; never expose ingress during a retry loop.

Keep public ingress, worker and sync excluded until deliberate later activation.
Preserved signing keys retain compatible signatures; key rotation does not recall
DB-backed aliases. An older DB may restore then-active shares. Exact-version
recovery does not run migration013's legacy recall; aliases remain and a deliberate
new-generation publication is required after that upgrade. Keep sharing disabled
until reviewing restored state. The development rehearsal below exercises
authenticated HTTPS/API, dashboard-asset and worker recovery separately from these
dormant restore mechanics. It does not exercise browser interaction against the
restored instance. Hashes/listing alone do not prove application readiness.
## Trust and ingress reference

Caddy joins only this project's `egress` network and proxies `app:8000`; it has no
DB-network membership, reserve mount or application-secret mount. The app's
`FORWARDED_ALLOW_IPS=*` trusts all peers on the operator-controlled host/project
networks, **not a Caddy-IP allowlist**. Raw app ports must remain unpublished and
these networks must not be shared with untrusted containers. The app's separate
raw-forwarding parser stays disabled (`PUBLIC_TRUST_PROXY_HEADERS=false`). Caddy
sets canonical Host/X-Forwarded-Host/HTTPS, replaces X-Forwarded-For with its direct
client IP, and removes Authorization, Forwarded, X-Real-IP and X-Forwarded-Prefix.
No caller-supplied owner header establishes identity.

Every site route authenticates before proxying, including uploads, job images,
raw media, schema, health and share aliases. After auth, unsafe methods with a
present Origin other than the exact canonical HTTPS origin get 403 (including
`Origin: null`). GET/HEAD/OPTIONS and absent-Origin CLI requests are exempt from
that Origin rule, never from auth. This prevents foreign-site multipart forms
using cached Basic credentials from creating uploads/sessions and invoking
classification. CORS and SameSite cookies alone do not prevent that attack.

## Storage and installed assets reference

| Host path under instance | Container path | Runtime ownership (pinned images) |
| --- | --- | --- |
| `state/reserve` | `/reserve` | App runs as root, UID:GID 0:0 |
| `state/postgres-data` | `/var/lib/postgresql/data` | PostgreSQL UID:GID 999:999 |
| `state/redis-data` | `/data` | Redis UID:GID 999:1000 |
| `secrets` | `/run/secrets` read-only in app/worker/sync | Operator-owned; read by app UID 0 |
| `secrets/postgres_password` | Same path in db, read-only | Read by PostgreSQL's root entrypoint |
| `config/app.env` | Loaded by Compose, not mounted | Operator-owned |
| `config/ingress` | `/etc/caddy` read-only | Operator-owned; Caddy UID:GID 0:0 |
| `state/proxy/data` | `/data` in Caddy | Caddy UID:GID 0:0; certificates/ACME identity |
| `state/proxy/config` | `/config` in Caddy | Caddy UID:GID 0:0; persisted configuration |

PostgreSQL and Redis entrypoints initially run as root and adjust ownership of
their data directories before dropping privileges. App-created reserve files are
root-owned; use administrative access for maintenance, never `chmod 777`. Preserve
numeric ownership when moving data. Rootless/user-namespace-remapped Docker and
network filesystems require separate ownership verification. This recipe does
not provision backups. Ingress config/state directories start private (0700).
Retain the complete proxy state and protected credential import along with the
application signing key. Never replace these with world-writable directories.

All bind sources must already exist (`create_host_path: false`). Data outlives
container recreation and `compose down`; never remove the instance as a reset
unless loss of the entire library is intended. Retain the complete reserve,
including draft media and Telegram session files, plus protected config/secrets.
Changing `postgres_password` does not change the password in an initialized DB.

Installation requires readable, nonempty prebuilt `index.html`; no frontend build
runs at startup. Assets and online configuration are validated in a temporary
sibling before replacing only generated `dashboard/dist` (including stale hidden
files). Ordinary copy/configuration failures preserve the prior dist; a failed
publication rename restores it. This is not a power-loss-atomic directory exchange.
Other reserve content is preserved. Disabled installation creates no dashboard.
The helper uses the same `load_settings()` path as the application: the local
default remains `meme-arsenal-reserve`, with YAML/dotenv/environment and `_FILE`
handling unchanged. Community explicitly sets `/reserve` for every role regardless
of the chosen host bind. Arbitrary overridden CLI `--config` is outside this
installer contract; direct image execution is not another complete deployment.

## Development schema recovery rehearsal

The OMA30 candidate smoke drill (`sudo python3 scripts/verify_community.py`, from
the repository root on a disposable CI runner) extends the complete current-image
restore drill with private TLS activation, retained owner cookie and no-cookie
Basic Auth, installed dashboard assets, saved replay/ordered lineage, owner
curation, a ready result, a SQL-queued job and retained public grants. It first
finishes the existing backup and sharing gates, TERM-drains the ordinary worker,
then prepares the queue and takes a second complete backup. The ordinary source
worker is never resumed with that queue. Target work uses a bounded fixture
worker with deterministic generation; it does not contact a generation provider.
Public opt-in follows private acceptance and tests retained links before any
republishing. All networks remain internal and no host ports are published.

The separate upgrade boundary is **populated DEVELOPMENT PostgreSQL
`012_public_share_codes` → `013_explicit_recipe_grants`**, using the exact
candidate application image and pinned PostgreSQL 16 recipe. It is not a released
application version upgrade, historical application startup, packaging migration,
arbitrary database adoption or downgrade promise. There is no historical image.
Do not weaken the complete restore helper's exact image/source-head and absent
target checks to use it for this logical checkpoint.

The fixed `scripts/oma30_upgrade.py` fixture creates a new instance and PostgreSQL
bind using normal `meme_arsenal` database/user and `db` host coordinates. Actual
candidate-image `alembic upgrade 012_public_share_codes` runs through community
preflight with dashboard installation disabled and a synthetic owner secret.
No current ORM, schema stamping, downgrade or application startup constructs
the baseline. Explicit SQL populates owner/media/jobs/attempts/curation/lineage,
active and inactive legacy grants and aliases. PostgreSQL 16, vector, version
and absence of the recipe column are checked before the checkpoint.

Recovery order, with app/worker/ingress never started on schema 012:

1. Save a private `pg_dump -Fc` checkpoint and reserve/secret byte copies before
   installing the committed fixture statement trigger.
2. Run actual `alembic upgrade head`. The trigger raises SQLSTATE `P0030` at
   013's real share recall UPDATE. Require failure and matching SQL context.
3. Use new independent SQL connections to require version 012, absent recipe
   column and exact unchanged rows/catalog. This proves transactional failure
   rollback; it does not prove process interruption or power-loss recovery.
4. Explicitly drop the fixture trigger/function, retry head and require 013.
   Every legacy grant, including inactive grants, must be recalled with NULL
   recipe consent. Only active/revoked/updated fields and the new column may
   differ; token/alias identity and creation dates remain. Repeat head as a no-op.
5. Stop that isolated fixture. Create another fresh instance/empty PostgreSQL
   bind sequentially, using the checkpoint secrets before database creation.
   Restore with `pg_restore --single-transaction --exit-on-error --no-owner
   --no-privileges`, compare exact 012 rows/catalog and protected/reserve bytes,
   then retry head and require recall again before any public serving.

A checkpoint rollback loses writes since that checkpoint and can resurrect
legacy active capabilities. Keep ingress and all application writers off until
the forward upgrade and recall checks succeed. No Alembic downgrade is tested.
The current-image complete backup/restore and this schema checkpoint remain
distinct procedures. CI logs record candidate commit/image, fixture row counts,
schema identities and recovery phases; private tokens/cookies/raw rows remain
inside disposable mode-0700 fixture storage. OMA30's accepted candidate CI proved
this declared development boundary and current-image restore; final supported-release
evidence remains OMA31 work.
