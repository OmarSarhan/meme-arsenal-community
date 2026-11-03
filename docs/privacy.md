# Data flows and private deployment

Before uploading, decide whether your selected providers may receive the media.
For example, uploading a private photo with live classification can send its bytes
and filename-derived caption before you choose Save. Private application access
does not imply local-only processing.

## Deployment and data decisions

Meme Arsenal is designed for one trusted owner running a private library.
Publishing its source does not publish an operator's reserve. The current online
application relies on authentication supplied by a reverse proxy; its browser
session cookie is not a login account. Use the authenticated
[community deployment recipe](../deploy/community/README.md) and keep the
application port unpublished. The [dated test evidence](release-preparation.md)
covers exact-image restore, restored app/worker activation and development-schema
012→013 recovery. It does not establish arbitrary historical upgrades,
downgrades or power-loss recovery.

## What stays in the reserve

The database and reserve hold imported messages, captions, originals, previews,
AI metadata, embeddings, remix jobs/results, and operational records. Telegram
sync uses the configured Telegram credentials and saved session. Back these up
as private data. A catalog export is a browsing artifact, not a scrubbed public
dataset or a complete recovery backup.

Private drafts, favorites, and notes use one configured durable owner namespace
across authenticated browsers. Cookie loss does not select a new library.
Provisioning and lost-owner-file recovery require the explicit procedures in the
[community runbook](../deploy/community/README.md#existing-library-owner-recovery).

## What live AI operations transmit

Depending on provider and settings, classification can send media, captions,
OCR text, and link-preview context to the configured provider. Video processing
can include keyframes and, when enabled, original video with audio, compressed
video, or extracted audio. Embedding operations can send captions, link-preview
text, and derived classification text including visible text and narration;
semantic search also embeds the query text. Remix analysis and generation send
selected source media, user guidance, captions, and classification context such
as summaries, visible text, and narration, with original video/audio inclusion
depending on settings and the request path.

Uploading a private draft can trigger classification before library save or
publication, including a filename-derived caption in the context.
Here, **private** describes who can access it through the application; it does
not mean the configured AI provider receives no data. Review the provider's
current account terms, region restrictions, and data settings before using
personal media. The project makes no provider-retention guarantee.

## Logs, retention, and sharing

Restrict access to logs and backups as carefully as access to the app. Only put
nonsecret text in style guidance; providers receive it and private retained
requests/results and backups may contain it. See
[retained guidance](derived-data-refresh.md#retained-guidance-and-identity) for
identity, serializer exclusions and confidentiality limits.

Gemini error recording redacts recognized key fields and inline media data in
request/response JSON. Retained text, media context, truncated raw error bodies,
and error messages can still contain private content. This is not a uniform
all-provider logging or complete scrubbing guarantee, and no automatic erasure
or retention deadline is promised. Treat database and log records as
private. Do not paste them into public bug reports without reviewing them.
Deleting an item from the active library does not erase existing backups or
copies already sent to providers or other people.

Saving to the private library does not publish. Sharing is disabled by default
and requires explicit enablement and publication. Sharing an output image does
not share its recipe or sources. Publishing a recipe grants access to approved,
eligible source previews and descriptions. See the
[sharing guide](opt-in-sharing.md) for grants and independent source/recipe recall.

Public share links deliberately disclose selected content. The public share
hostname must use its restricted route configuration, separately from private
dashboard access. Sharing is optional. Recall prevents future access through
the application according to the supported share lifecycle; it cannot revoke
files that recipients already downloaded.

## Provider selection and retained work reference

Each operation uses its effective provider: `CLASSIFICATION_PROVIDER`,
`EMBEDDING_PROVIDER` and `REMIX_PROVIDER` fall back to `AI_PROVIDER` when unset.
Global mock with a live override is not offline: that operation sends the data
described above to its selected provider.
New remix jobs capture their effective generation configuration. Retained captured
Google work can still call Google after current defaults and overrides become mock,
using current credentials, source authorization, and the credit circuit. Hosted queued
jobs retain their capture through requeue and worker restart. The local executor runs
in its process; it is not a durable queue. Stopping the worker or local process prevents
new dispatch but cannot cancel an in-flight provider request. Saving an already ready
image does not regenerate it; optional classification and embeddings use current settings.
Mock operations use deterministic local behavior for exploration and tests.
They do not provide useful AI metadata or generate remix images. A fixture
import avoids Telegram but still uses the configured database and AI provider;
set isolated storage and mock AI explicitly.
