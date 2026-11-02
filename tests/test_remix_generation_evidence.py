from dataclasses import replace
from types import SimpleNamespace

import pytest

from meme_arsenal.remix import _request_provenance, generate_remix, RemixMitigation
from meme_arsenal.remix_execution import capture_execution


@pytest.mark.parametrize("text,origin", [
    ('{"summary":"validated returned JSON"}', "model_json"),
    ('{"confidence":"invalid"}', "local_fallback"),
    ("", "local_fallback"),
])
def test_origin_follows_actual_metadata_validation(monkeypatch, text, origin):
    from meme_arsenal.remix import _parse_response_metadata_with_origin
    fallback = object()
    monkeypatch.setattr("meme_arsenal.remix._fallback_metadata", lambda *_: fallback)
    metadata, actual = _parse_response_metadata_with_origin(text, None, None)
    assert actual == origin
    if origin == "local_fallback":
        assert metadata is fallback


@pytest.mark.parametrize("attempt", [0, -1, True, 1.5, "1"])
def test_invalid_outer_attempt_precedes_media_and_transport(settings, attempt):
    selected = replace(settings, remix_provider="google", google_api_key="dummy")
    with pytest.raises(ValueError, match="attempt_number"):
        generate_remix(None, selected, 1, 2, attempt_number=attempt)


def test_invalid_mitigation_precedes_media_and_transport(settings):
    selected = replace(settings, remix_provider="google", google_api_key="dummy")
    with pytest.raises(ValueError, match="mitigation_id"):
        generate_remix(None, selected, 1, 2, mitigation=RemixMitigation("invalid", "", ""))


@pytest.mark.parametrize("original", ["present", "missing", "oversized", "mitigated", "generic-mime"])
def test_actual_video_builder_evidence(settings, tmp_path, original):
    from meme_arsenal.remix import _source_video_parts
    video = tmp_path / ("source.gifv" if original == "generic-mime" else "source.mp4")
    if original != "missing":
        video.write_bytes(b"stub")
    selected = replace(settings, remix_provider="google", remix_include_video_original=True,
                       remix_video_inline_max_mb=1)
    if original == "oversized":
        with video.open("wb") as stream:
            stream.truncate(2 * 1024 * 1024)
    mitigation = RemixMitigation("safer-start", "", "") if original == "mitigated" else None
    parts = [{"text": "source label"}, {"inline_data": {"mime_type": "image/jpeg", "data": "stub"}}]
    video_parts = _source_video_parts(selected, "A", SimpleNamespace(video_path=video), mitigation)
    parts += video_parts
    evidence = _request_provenance(
        capture_execution(selected, "new_direct"), parts, mitigation, 2,
        original_video_attached=bool(video_parts),
    )
    assert evidence.attachment_mode == ("original-video" if original in {"present", "generic-mime"} else "stills")
    digest = evidence.submitted_text_digest
    parts[0]["text"] = "provider mutated payload"
    assert evidence.submitted_text_digest == digest
