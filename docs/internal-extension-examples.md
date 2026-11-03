# Internal extension examples

Use these examples to change provider routing, folder import, or retained Remix
requests. Update callers and tests together: these internal helpers have no
published compatibility guarantee.

Install the [contributor prerequisites](development.md#first-local-setup) first.
Choose the example matching your change: A checks mixed-provider dispatch, B
checks folder intake, and C checks retained Remix guidance. Their complete tests
and contracts follow each example.

Each fragment below is an **annotated pytest excerpt, not a standalone script**.
Run the linked complete test from the repository root with development dependencies
installed. The `settings` and `fixture_dir` fixtures in
[conftest.py](../tests/conftest.py) create temporary SQLite/reserve storage, a
synthetic owner, and generated PNGs/manifest; `monkeypatch` is supplied by pytest.
Tests construct Settings directly or patch `load_settings`, avoiding real account
configuration. Their mocks replace the named request boundaries; pytest commands
and mocks alone are not universal network isolation. These checks demonstrate
local contracts, not live authentication, model quality, or release acceptance.

## A. Mixed provider dispatch and payload contracts

Use the existing caller to check that classification and Remix reach their own
adapters. This excerpt selects the explicit-override case of
[test_mixed_classification_and_remix_requests](../tests/test_remix.py).
The complete test also covers inherited OpenAI classification and tweak analysis.
Imports and helpers below are from that test module; both request boundaries are
patched **before** importing fixture messages and invoking real callers.

```python
# Annotated pytest excerpt; inside the settings/fixture_dir/monkeypatch test.
from dataclasses import replace
from types import SimpleNamespace
from openai.resources.responses import Responses
from sqlalchemy import select
from meme_arsenal.ai import ClassificationPayload, classify_pending_media
from meme_arsenal.db import session_scope
from meme_arsenal.media import process_pending_media
from meme_arsenal.models import MediaItem
from meme_arsenal.remix import generate_remix
from meme_arsenal.telegram_importer import import_fixture_messages
from test_remix import gemini_image_response  # synthetic PNG and metadata response

settings = replace(
    settings, ai_provider="mock", classification_provider="openai",
    embedding_provider="mock", remix_provider="google",
    openai_api_key="synthetic-openai-key", google_api_key="synthetic-google-key",
    ai_model="explicit-openai-classifier", remix_model="explicit-google-remixer",
)
classification_requests, remix_requests = [], []

def fake_response_create(resource, **request):
    classification_requests.append(request)
    return SimpleNamespace(output_text=ClassificationPayload(
        summary="OpenAI classified fixture",
    ).model_dump_json())

def fake_google_post(selected, model, method, payload):
    remix_requests.append((model, method, payload))
    return gemini_image_response()

monkeypatch.setattr(Responses, "create", fake_response_create)
monkeypatch.setattr("meme_arsenal.remix.post_google_model", fake_google_post)
with session_scope(settings) as session:
    import_fixture_messages(session, settings, fixture_dir)
    process_pending_media(session, settings)
    report = classify_pending_media(session, settings)
    ids = session.scalars(select(MediaItem.id).order_by(MediaItem.id)).all()
    assert report["classified"] == len(ids) == 2
    assert remix_requests == []
    result = generate_remix(session, settings, *ids, job_id="mixed-providers")
assert len(classification_requests) == 2
assert result.metadata.summary == "Deploy smug remix"
assert result.image_path.is_file()
```

Run the complete parametrized test:

```bash
python -m pytest -q tests/test_remix.py::test_mixed_classification_and_remix_requests
```

Expect both parameter cases to pass: two OpenAI classification requests, no Google
Remix request during classification, and a synthetic generated image/metadata.
Remix does not add classification requests. Mock embedding is selected here; the
folder example below actually exercises embedding.

[ai.py](../src/meme_arsenal/ai.py) dispatches explicitly:
`classify_with_mock(context)` returns a payload;
`classify_with_openai(settings, context)` returns a payload;
`classify_with_google(settings, context)` returns `(payload, raw_response)`.
Payload validation and explicit Google blocked states are contracts, not optional
adapter conventions. [embeddings.py](../src/meme_arsenal/embeddings.py) requires
exactly the requested number of finite numeric vector values and rejects bools.

Explicit capability overrides beat global `AI_PROVIDER`. An absent capability
selection inherits configuration; it does not fail over after request failure.
Distinct classification/embedding pause gates do not create missing authoritative
embedding input. See [derived-data refresh](derived-data-refresh.md) for input
identity, reuse, authority and refresh rules. Patching `remix.post_google_model`
does not also patch the separate binding in `ai`.

## B. Folder intake and bounded follow-up

Use the folder caller when adding an intake route: it validates storage separation
and then coordinates intake, processing, classification, embedding and indexes.
The complete [test_two_image_offline_flow](../tests/test_folder_import.py) uses
the module's `command` fixture. That fixture returns `root, settings, run, report`:
`root` is temporary input, reserve is a separate sibling, SQLite is under reserve,
`run` invokes `CliRunner` with `import-folder` and namespace `test`, and `report`
reads the resulting JSON report. The test patches `folder.load_settings` before
calling it and also forbids subprocess creation.

```python
# Annotated pytest excerpt; command and monkeypatch are pytest fixtures.
from dataclasses import replace
from PIL import Image
from meme_arsenal import folder_import as folder
from meme_arsenal.db import session_scope
from meme_arsenal.remix import generate_remix
from meme_arsenal.remix_execution import capture_execution
from test_remix import gemini_image_response

root, settings, run, report = command
configured = replace(settings, ai_provider="mock", ai_model="mock-classifier",
                     embedding_provider="mock", embedding_model="mock-embedding",
                     embedding_dimensions=32, remix_provider="google",
                     google_api_key="offline-placeholder",
                     remix_model="gemini-3.1-flash-image")
monkeypatch.setattr(folder, "load_settings", lambda _: configured)
for key, color in (("a.png", "red"), ("b.png", "blue")):
    Image.new("RGB", (64, 64), color).save(root / key)
assert run("--pipeline-limit", "2").exit_code == 0
data = report()
assert data["stages"]["embedded"] == 2
monkeypatch.setattr("meme_arsenal.remix.post_google_model",
                    lambda *a: gemini_image_response())
with session_scope(configured) as session:
    result = generate_remix(session, configured, *data["validated_media_item_ids"],
        execution_capture=capture_execution(configured, "new_direct"))
    assert result.image_path.is_file()
assert run("--pipeline-limit", "1").exit_code == 0
assert report()["selected"] == 0
```

```bash
python -m pytest -q tests/test_folder_import.py::test_two_image_offline_flow
```

Expect two processed/classified/embedded items, two vectors/search documents,
working search, and an image from the **patched Google boundary**. Mock Remix
does not generate that image. The next bounded run selects zero completed items.

The lower-level [ingestion.py](../src/meme_arsenal/ingestion.py) accepts
`SourceRecord(namespace, external_key, caption=None, date=None, kind="folder")`.
`IngestResult` carries status (`created`, `unchanged`, `conflict`), media and message
IDs; its `media_item_ids` contains the ID only for `created`.
`ingest_local_record(factory, settings, root, record, *, mode, owner_id=None)`
requires initialized reserve/schema and a session factory. It owns fresh
transactions and commits. Full source/output/database/derivative preflight belongs
to `folder_import.preflight`, before layout/schema creation, not this helper.

Namespace and relative path identify an import; equal bytes do not merge items.
Unchanged imports with pending work can still enter the bounded pipeline. An
empty ID list must never select the whole library. Limits count items, not
requests, retries or cost. Saved-library flags grant no public share or separate
owner access and do not imply Telegram origin. Use trusted, stable directories;
filesystem and database writes are not one atomic operation. See
[source ingestion](source-ingestion.md) and
[derived-data refresh](derived-data-refresh.md).

## C. Retained guidance, request evidence and private identity

Use captured Remix settings to preserve generation intent across an internal image
retry while keeping current operational access. This excerpt shows the retained-A
case of [test_remix_retained_initial_and_internal_retry](../tests/test_style_guidance_requests.py).
`prepare_archive` is a helper from `test_remix`, not a fixture: it imports the
synthetic archive, processes it and performs mock classification/embedding first.
`image_error` is the local test helper producing a synthetic unprocessable-image
`GeminiApiError`; it triggers the specific metadata-only retry.

```python
# Annotated pytest excerpt; settings/fixture_dir/monkeypatch come from pytest.
from dataclasses import replace
from sqlalchemy import select
from meme_arsenal import remix
from meme_arsenal.db import session_scope
from meme_arsenal.models import MediaItem
from meme_arsenal.remix_execution import capture_execution, ordered_text_digest
from meme_arsenal.style_guidance import render_style_guidance
from test_remix import prepare_archive, gemini_image_response
from test_style_guidance_requests import image_error

prepare_archive(settings, fixture_dir)
original = replace(settings, remix_provider="google", google_api_key="synthetic",
                   remix_style_guidance="retained A")
capture = capture_execution(original, "new_direct")
current = replace(original, remix_style_guidance="current B")
calls = []
def post(config, model, method, payload):
    assert config.remix_style_guidance == "retained A"
    calls.append(payload)
    if len(calls) == 1:
        raise image_error()
    return gemini_image_response()
monkeypatch.setattr(remix, "post_google_model", post)
with session_scope(current) as db:
    ids = list(db.scalars(select(MediaItem.id).order_by(MediaItem.id)))
    result = remix.generate_remix(db, current, *ids,
        execution_capture=capture, attempt_number=2)
for call in calls:
    text = call["contents"][0]["parts"][0]["text"]
    assert "current B" not in text
    assert render_style_guidance("retained A") in text
evidence = result.generation_provenance
assert evidence.capture == capture and evidence.attempt_number == 2
assert evidence.attachment_mode == "metadata-only"
assert evidence.submitted_text_digest == ordered_text_digest([
    p["text"] for p in calls[-1]["contents"][0]["parts"] if "text" in p])
```

```bash
python -m pytest -q tests/test_style_guidance_requests.py::test_remix_retained_initial_and_internal_retry
```

Expect both empty and retained-A cases to pass. The image error then synthetic
success preserve capture and outer attempt 2; the digest describes final ordered
text parts. In general `attempt_number` may be `None` or a positive int excluding
bool; direct generation need not invent a queue attempt.

Guidance allows at most 1000 Unicode code points after line-ending normalization
and outer trimming. Empty v1 and strict nonempty v2 captures remain distinct.
Capture is not a source snapshot: credentials, source access, and save-time
classification/embedding remain current. Retained generation identity comes from
saved message evidence independently of mutable classification. Public fields
exclude raw private provenance; generated content can still reflect guidance.
See the complete tests above, [strict shape tests](../tests/test_style_guidance_contract.py),
[private/public identity tests](../tests/test_result_identity.py), and
[derived-data refresh](derived-data-refresh.md) for the full invariants.

The small companion checks for those shape and presentation boundaries are:

```bash
python -m pytest -q tests/test_style_guidance_contract.py::test_strict_shapes tests/test_result_identity.py::test_private_results_and_public_boundaries
```
