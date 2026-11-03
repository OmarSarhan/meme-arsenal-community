# Opt-in sharing transition

Save privately, then deliberately publish selected content. For example, save a
remix, publish only its output, and publish its recipe separately only when you
intend to disclose eligible source presentation and descriptions. A private
preview or download creates no public grant.

Before upgrading an existing instance: migration 013 recalls **all legacy
shares, including output links**. It preserves media, curation and retired alias
rows. Old links stay dead after deliberate republication, which uses a new
generation. Take the documented consistent private backup before migrating.

## Configure sharing and manage links

Use the item sharing controls to check status, publish an output or recipe, and
recall links. Recalling a recipe withdraws its grant. Recalling a separately
shared source is independent.

`PUBLIC_SHARING_ENABLED=false` is the default and emergency stop. It blocks
publication and capability serving, while library save, download, private recipe
preview and recall remain available. A nonempty `PUBLIC_SHARE_URL` alone does not
enable sharing. With sharing enabled and an empty URL, signed links remain behind
the authenticated private ingress. An optional distinct HTTPS DNS origin selects
short links and narrow anonymous ingress. Host routing does not authenticate owners.

New instances may pass `--share-url https://share.example.net` to `init.py`; the
initializer generates consistent app and ingress values but leaves sharing off.
It refuses existing instance directories. Never rerun initialization over state.

## Maintain an existing instance safely

For an existing healthy instance, use the same explicit project, env file and
ordered Compose files as the deployment guide. Each configuration change below
requires this maintenance boundary, including later enable/disable toggles:

1. Finish any backup/recovery and suspend manual/scheduled writers, imports and
   deployments for the entire operation. Record exact container IDs, images,
   start times and which services are running, including an existing `sync` even
   when the telegram profile is not selected. Keep DB and Redis running.
2. Stop ingress first: send `docker kill --signal=TERM <recorded-caddy-id>` and
   wait for that container's normal exit. This immediately withdraws public access
   while writers drain. Do not recreate any writer yet. Ensure app startup has
   completed and copy `/tmp/arsenal-api-drain.json` from its recorded container
   with `docker cp` into a private maintenance directory; it must say `ready`.
   Wait for worker/sync startup and their TERM handlers before signalling them.
   On Linux, PID 1's `/proc/1/status` `SigCgt` mask must include TERM (`0x4000`);
   merely being listed as running is insufficient during imports/startup.
3. Send one `docker kill --signal=TERM <recorded-id>` to each running app, worker
   and sync. Use `docker wait <recorded-id>` and `docker inspect` to verify normal
   exit, unchanged image/start identity and no OOM: worker/sync/Caddy exit 0;
   app exit 0 or 143 additionally requires the copied API proof to have the SAME
   token and phase `drained`. Copy that proof again with `docker cp` after exit.
   Follow the [backup drain evidence](../deploy/community/README.md#consistent-private-maintenance-backups).
   Pending/abnormal drainage means stop and investigate, with ingress still off.
   Never substitute finite `compose stop`, forced kill or `up --force-recreate`
   for drainage. No config edits or recreation while a writer remains unresolved.
4. Set `PUBLIC_SHARING_ENABLED=false` in the protected `config/app.env`.
   Keep `PUBLIC_SITE_URL` consistent with the private site. Set `PUBLIC_SHARE_URL`
   to the canonical distinct HTTPS origin (port 443, no path or credentials).
   Render the checked-in `deploy/community/Caddyfile` replacing `SITE_AUTHORITY`
   with that private hostname, then append `public.Caddyfile` replacing
   `SHARE_AUTHORITY` with the public hostname. Write the result to the protected
   `config/ingress/Caddyfile`; preserve `owner.caddy` and its permissions.
5. Run `run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile
   --adapter caddyfile`; do not print adapted JSON containing the password hash.
   With old writers now cleanly stopped, recreate a previously running app using
   `up -d --no-deps --no-build --force-recreate --wait app` and confirm readiness.
   Recreate only previously running worker/sync with the updated environment,
   using `--no-deps`; explicitly select sync with the telegram profile if needed.
   Leave previously stopped services stopped. Restore previously running Caddy
   last with `up -d --no-deps --no-build --wait caddy` after validation/readiness.
6. Review retained shares. Deliberately enable sharing only when ready, repeating
   steps 1–3 before editing the flag and step 5 afterward. Publish each desired
   item explicitly. A failed proxy reload can retain old configuration; emergency
   withdrawal stops ingress immediately, then completes the same graceful drain
   before changing/recreating the application. Resume external writers only after
   the maintenance operation has completed successfully.

## Grant, recall and restored-state reference

Publishing a recipe page explicitly includes eligible saved sources' previews, descriptions
and permitted browser video presentation. Separate source shares are unnecessary.
GET status and private preview do not publish. Fixed A/B consent cannot acquire
new sources or original permissions on later edits. Summaries and eligible media
presentation remain live. Drafts, denied sources and raw stored snapshots are not
public recipe content. Both captured and current original permission constrain
original-derived posters and video.

Standalone source recall is independent. Source deletion/ineligibility suppresses
its slot; the same approved ID/position may resume if truly eligible again. Missing
bytes alone are not recall. Replacing bytes does not undo repair-cleared fields or
deleted tombstones. Recall the parent recipe to withdraw its full grant; republish
rotates links. Output deletion kills its shares. Authorized streams may finish;
downloaded copies cannot be recalled.

Signing-key rotation breaks signed links but active database aliases survive;
rotation is not recall. Disable/re-enable preserves active grants. After restoring
an older backup keep sharing disabled and public ingress stopped until share state
has been reviewed/recalled. Protect the complete config/secrets/ingress tree in
the existing private backup procedure. No backup or restore algorithm changes here.
