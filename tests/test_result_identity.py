import json
from dataclasses import replace

import pytest

from meme_arsenal import ai, online
from meme_arsenal.catalog import catalog_item_payload
from meme_arsenal.db import session_scope
from meme_arsenal.models import RemixJobRecord
from meme_arsenal.remix import RemixResult
from meme_arsenal.remix_execution import RemixGenerationProvenance, capture_execution
from meme_arsenal.remix_server import RemixJob
from meme_arsenal.result_identity import generation_identity, saved_result_identity
from meme_arsenal.style_guidance import style_guidance_digest
from test_embedding_execution import establish


@pytest.mark.parametrize("guidance", ["", "UNIQUE_PRIVATE_GUIDANCE_39"])
def test_private_results_and_public_boundaries(settings, monkeypatch, guidance):
    retained = replace(settings, remix_provider="google", remix_style_guidance=guidance)
    capture = capture_execution(retained, "new_job")
    provenance = RemixGenerationProvenance(capture, "a" * 64, "metadata-only", None, "model_json", 1)
    identity = generation_identity(provenance.payload())
    assert identity["style_digest"] == style_guidance_digest(guidance)
    assert identity["format"] == ("remix-execution-v2" if guidance else "remix-execution-v1")
    current = replace(settings, remix_style_guidance="CURRENT_B", classification_style_guidance="PRIVATE_C_39")
    with session_scope(current) as db:
        message, item, row = establish(db, current, monkeypatch)
        message.raw_metadata_json = {**(message.raw_metadata_json or {}),
                                     "generation": {"provenance": provenance.payload()}}
        row.raw_response_json = {**row.raw_response_json, "synthetic_operator_guidance": "PRIVATE_C_39",
                                 "generation": {"provenance": provenance.payload()}}
        db.flush()
        result = RemixResult("job", item.id, item.id, settings.reserve_path / "synthetic.png",
                             ai.ClassificationPayload(summary="generated"), {}, generation_provenance=provenance)
        assert result.payload()["generation_identity"] == identity
        local = RemixJob(id="job", left_id=item.id, right_id=item.id, execution_capture=capture)
        local.result = result
        assert local.snapshot()["generation_identity"] == identity
        hosted = RemixJobRecord(id="job", left_id=item.id, right_id=item.id,
                                execution_capture_json=capture.payload(), result_json={"result": result.payload()})
        hosted.attempts = []
        assert online._job_payload(hosted)["result"]["generation_identity"] == identity
        private = online._item_payload(db, current, item, private_identity=True)
        selected = private["execution_identity"]
        assert selected["generation"] == identity
        assert selected["classification"] == dict(provider="mock", model=row.model,
                                                  prompt_version=row.prompt_version,
                                                  compatibility_key=row.compatibility_key)
        assert saved_result_identity(None, message.raw_metadata_json) == {
            "generation": identity, "classification": None}
        public = [catalog_item_payload(db, current, item.id), online._item_payload(db, current, item),
                  online._capture_recipe_grant(db, current, item),
                  online._source_trace_payload_from_item(current, item)]
        encoded = json.dumps(public)
        for forbidden in ("UNIQUE_PRIVATE_GUIDANCE_39", "PRIVATE_C_39", "generation_provenance",
                          "execution_identity", "remix_style_guidance"):
            assert forbidden not in encoded
        row.raw_response_json = {"generation": {"provenance": {"broken": guidance}}}
        message.raw_metadata_json = {"generation": {"provenance": {"broken": guidance}}}
        row.execution_provider = None
        row.compatibility_key = None
        assert online._item_payload(db, current, item, private_identity=True)["execution_identity"] == {
            "generation": None, "classification": None}


@pytest.mark.parametrize("value", [None, {}, {"capture": None}, "legacy"])
def test_unknown_generation_display(value):
    assert generation_identity(value) is None
