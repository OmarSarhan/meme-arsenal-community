# Resumable folder CLI

Copy a trusted folder into your library without Telegram or provider calls during
intake. For example, after the [contributor installation](development.md#first-local-setup)
and local archive initialization (`arsenal init`), run this from the configured
checkout root, replacing the source path and keeping the namespace stable.
Before initialization or intake, select isolated writable reserve/database paths
and a trusted stable source containing only intended files. Do not import during
backup/recovery; this command is an external writer:

```bash
arsenal import-folder /absolute/path/to/trusted-images --namespace personal-images
```

Use an isolated writable reserve/database and a stable source containing only
files intended for import. Expect copied originals and created/unchanged counts,
plus a private report under `data/run-logs`. Intake alone does not classify,
export a catalog or build a dashboard. For a full owner app, initialize through
the [community guide](../deploy/community/README.md#upload-first-setup) instead;
then use hosted mode with its configured and provisioned owner.

## Configuration and repeat imports

Run `arsenal import-folder ROOT --namespace NAME` from a directory containing only
the intended configuration. Relative configuration paths resolve against that working
directory. The default `--mode local` copies supported image/video files into the
reserve without provider work. `--mode hosted` requires the configured, already
provisioned owner. Namespace and exact sorted POSIX relative paths are identities;
keep the namespace stable. Files are copied, never moved or deleted. Captions and
capture dates are not inferred from filenames. Symlinks and special files are rejected;
`.tgs` and manifests are not imported. Use a trusted, stable source directory.

Rerun the same command to validate existing copies and import additions. Renaming a
file creates a new identity; moving the whole source directory preserves relative
identities. Changed bytes at an existing identity report a conflict. Reports are
private mode-0600 `data/run-logs/folder-import-*.json` files containing sensitive
relative keys and aggregate outcomes; they are diagnostics, not resume authorities.
An uncertain commit or interruption stops further work and retains potentially
committed bytes. Stabilize the source and rerun to reconcile; do not delete attempt
files based on an uncertain report.

## Deliberately run a bounded paid pipeline

`--pipeline-limit N` explicitly authorizes configured processing, classification and
embedding, including provider costs. Embeddings run regardless of the automatic
online-sync embedding switch. N must be positive and bounds media items, not requests,
retries or tokens. Each stage uses one worker. Only actionable pending items in this
invocation's successfully validated scope qualify, including unchanged imports;
completed prefixes consume no slots. Normal bounded deferral succeeds. Repeated
failures can recur at the front of the scope. Provider admission and legacy evidence
rules still apply; successful exit does not guarantee AI approval or searchability.
After a returned pipeline result, public indexes refresh for the entire validated
scope, even after returned stage failures. Thrown orchestration defers refresh until
a later explicit rerun. Intake-only mode promises no index refresh. Pipeline mode
requires a file-backed SQLite database (or the configured supported server database),
not an in-memory SQLite database. SQLite URI options (`uri=...`) are unsupported;
use an ordinary SQLAlchemy file URL such as `sqlite:////absolute/path/library.sqlite`.
Before any writes, pipeline mode checks existing derivative directories and symlinks
for overlap with the source, including links below the configured output roots.

## Results and backup coordination

Exit codes: 0 successful requested work, including empty input or bounded deferral;
2 invalid configuration/preflight/schema; 1 intake failure, conflict or uncertain
commit; 3 downstream/index/report failure after clean intake; 130 interruption.
Intake failures take precedence over downstream/report failures. Reporting cannot
undo committed intake. The console uses fixed safe diagnostics.

There is no automatic static export or dashboard build. Existing `export-catalog`
and dashboard commands operate on their documented archive scope, independently of
this folder bound. Back up the database, full reserve, and owner/signing configuration;
the original source directory is unnecessary to restore copied data. Suspend every
external writer, including this command, during backup and recovery.

## Internal folder ingestion foundation

`ingestion.SourceRecord` and `ingest_local_record` accept one trusted root and one
exact POSIX relative key. The CLI above uses these helpers for discovered files.
No providers are called during intake. Supply an explicit sessionmaker bound to
an initialized database; the operation owns fresh sessions and commits each item.
Do not wrap it in a caller transaction or use a scoped/shared session factory.

Identity is the full case-sensitive `(folder, namespace, external_key)` tuple.
Namespace must be 1–128 UTF-8 bytes, key 1–1024 UTF-8 bytes (combined maximum
1158 bytes including kind). NUL and invalid UTF-8 are rejected. Keys reject absolute
paths, empty/dot/dotdot components and repeated separators. Case and Unicode
spelling are preserved; backslash is an ordinary POSIX filename character.
The resolved root must be a directory; relative symlinks and nonregular files
are unsupported. Input must remain stable: stat checks detect ordinary mutation,
not adversarial filesystem races. Database length checks are additional character
bounds; callers must use the helper for the stricter UTF-8 byte validation.

Same key and full SHA256/size returns unchanged IDs and preserves first caption/date.
Changed bytes conflict without replacement. Different keys or namespaces create
distinct items even for equal bytes. Rename creates another identity; moving the
root with the same namespace/key preserves identity. Source deletion has no effect
on reserved bytes. Missing/corrupt/deleted mapped originals fail rather than revive.
Only `created` results expose `media_item_ids` for new pipeline work; preserve an
empty list as no work. Downstream processing failures retain the committed item.

Local mode requires no owner and creates no account. Hosted mode requires the
configured, provisioned owner. Existing mappings require exact ownership equality;
ownerless transferred records cannot be adopted by a hosted rerun. This guards
intake consistency, not per-owner isolation: saved media remain global to the
single private Arsenal. Explicit folder origin and published/public/original-allowed
fields mean saved library membership. No share or recipe grant is created; later
deliberate opt-in publication remains possible. Telegram URLs are never fabricated.

Each copy lives in an exclusive attempt directory and is finalized before DB
commit. Rows are complete before commit; mapping references restrict deletion.
Legacy SQLite does not globally enable foreign keys, so helper checks also enforce
mapped-record validity. Only the named full-key uniqueness race retries, at most
three attempts, verifying the committed winner and reserve bytes. Unrelated errors
propagate. Known rolled-back attempts remove only their own files. A commit
exception raises `CommitUncertain` and retains files: rerun later to resolve the
full key. Crash/uncertain commit can leave orphans; there is no garbage collector,
filesystem/database atomicity or power-loss durability guarantee.

Revision 014 adds one table without backfill or legacy rekeying; SQLite metadata
initialization also creates it. SQLite is trusted single-operator intake. Actual
PostgreSQL concurrency and transfer acceptance belongs to disposable CI, not local
SQLite tests. Back up the mapping database, complete reserve and existing owner /
signing configuration. Source roots are not recovery requirements. As for other
external writers, imports must finish and remain suspended throughout backup and
recovery; atomic rename and `.backup.lock` do not provide that coordination.
Historical private release evidence remains pinned to its original candidate;
this foundation makes no new publication or audit claim.
