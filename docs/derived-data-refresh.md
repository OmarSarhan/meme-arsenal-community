# Derived data and explicit refresh

Reuse compatible metadata and vectors, or deliberately refresh selected work when
settings change. Browsing existing descriptions does not itself request a refresh.
For an already configured archive, this explicitly paid example refreshes at most
five eligible classifications:

```bash
arsenal classify --force --limit 5
```

This always selects the newest eligible batch; repeating it is not pagination.
It does not regenerate images. Review provider settings and back up private data
before authorizing replacement. Existing manual metadata keeps authority.

## Common refresh tasks

`classify --force` followed by `embed --force` explicitly refreshes the eligible archive
and may incur provider charges. Without `--limit` these commands are unbounded.
`classify --force --limit N` always selects the newest eligible batch: repeating it is
not pagination. Embedding limits count distinct actionable items, and the two bounded
commands need not select identical sets. Pipeline limits count distinct messages and
retain multiple actionable media items within selected messages.

Known-stale work can run through already configured sync/recovery; unknown legacy
identity needs manual force for paid replacement. See the reference below before
changing settings. Classification and embedding batches need not match.

## Optional operator style guidance

`CLASSIFICATION_STYLE_GUIDANCE` and `REMIX_STYLE_GUIDANCE` (YAML:
`classification_style_guidance` and `remix_style_guidance`) default to empty.
A bounded nonsecret example is `Prefer concise descriptions in plain language.`
Use strings or YAML null (empty). CRLF/CR normalize to LF, outer whitespace is trimmed,
and the normalized maximum is exactly 1000 Unicode code points. TAB/LF are allowed;
other controls and surrogates are rejected before trimming. Unicode joiners are
preserved; there is no NFC normalization. Errors never echo the guidance.

Layer order remains defaults, YAML, dotenv, environment. Within a layer explicit
inline empty clears lower values and wins over its `_FILE`; readable empty guidance
files also clear lower values. A bare dotenv key is unspecified. The corresponding
`CLASSIFICATION_STYLE_GUIDANCE_FILE` / `REMIX_STYLE_GUIDANCE_FILE` options are config
ingestion, not live reload. Restart/reload configuration through the existing workflow.

Only enter nonsecret prose: providers receive it and private jobs/results/logs and
backups can retain it. Generated content can reflect it. See
[retained guidance and identity](#retained-guidance-and-identity) before use.

## Identity and embedding authority reference

Classification reuse depends on the declared provider, model, prompt and source-policy
identity. Embeddings additionally depend on provider, model, requested dimensions and
the exact normalized text produced by the versioned text builder. Compatible unchanged
work is reused. Known stale work is ordinary pending work, including existing row slots.
Unknown legacy evidence is retained for display and lexical search but requires explicit
force before paid replacement; migrations do not infer providers or backfill identity.
Verified manual classification has authority over AI metadata for embedding and display.

App-managed bytes maintain source hashes. Arbitrary external in-place file mutations or
tool changes require explicit processing or a declared policy refresh; this is not an
mtime detector or an exact provider wire-payload guarantee.

Embedding input prefers verified manual metadata, then compatible current independent
classification, then validated saved-generation metadata. The fixed saved-generation
slot binds model-returned metadata and captured generation provenance to the persisted
output and normalized metadata. It does not impersonate independent classification.
Saving uses current classification and embedding settings while retaining the actual
generation configuration, including historical policy. Unknown legacy evidence and
local fallback descriptions remain displayable but cannot alone authorize embeddings.
Historical ready images remain saveable without paid regeneration; evidence is not
backfilled. Fallback-only records require explicit force for paid reclassification.
Loading settings and pure compatibility lookups do not enqueue work or incur provider
costs. Later explicit commands **or already configured processing, sync and recovery**
can select known-stale work and incur costs. Unknown legacy evidence still requires
force; failed refresh preserves prior evidence and manual metadata remains authoritative.
`sync-online` normally scopes processing to imported/repaired IDs, but `--skip-import`
and successful bounded credit recovery can select across the library. Its limit counts
messages, retaining multiple actionable items per selected message; recovery uses its
configured batch bound and online embedding switch. Classification limits count items;
repeated forced newest batches are not pagination. Folder pipeline limits count items
and explicitly enable embeddings independently of the online auto-embedding switch.
The same selected embedding text reuses vectors; Remix-only guidance does not invalidate
unrelated embeddings. These workflows neither regenerate images nor rewrite originals.

## Retained guidance and identity

Empty defaults preserve previous requests and v1 captures. Retained v1 generation
always executes default guidance even with custom current configuration. Nonempty v2
retains the selected prose across retries/restarts. Current credentials, access and
independent save classification/embedding remain current. Mock classification records
guidance identity but deterministic output ignores prose; Remix mock is unavailable.
Guidance remains subordinate to code-owned schema, grounding, safety, mitigation and
tweaks. JSON quoting is not a prompt-injection guarantee; mocks do not prove provider
obedience.

Private local/hosted job results return `generation_identity`; generated result payloads
also return it beside retained provenance. Private saved result `execution_identity`
separates generation from independent classification. Generation is read from the
retained saved-item record, so refreshing classification or editing metadata does
not replace it. Classification provider/model/
prompt version/compatibility key identify the entire executed input/policy, including
guidance; they are not a reconstructable style-only hash. Unknown/manual/malformed
historical evidence yields null rather than today's selection. Historical v1 evidence
and ready-save authority remain valid without re-execution.

Only enter nonsecret prose. Selected providers receive it; raw nonsecret guidance may
remain in private jobs/results/saved provenance and their logs/backups. Public catalog,
item, share, recipe and card serializers do not export that configuration or full private
provenance. Generated summaries/images can reflect guidance, so this is no confidentiality
guarantee. Digests are compatibility evidence, not secrecy, reversible prompts,
authenticated signatures or exact image/source snapshots. Capture does not freeze source
media or promise reproducible images.
