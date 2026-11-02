from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.error import HTTPError

import pytest
from sqlalchemy import select

from meme_arsenal.ai_circuit import (
    acquire_ai_credit_probe,
    ai_circuit_status,
    open_ai_credit_circuit,
)
from meme_arsenal.ai import classify_pending_media
from meme_arsenal.db import session_scope
from meme_arsenal.google_api import (
    GeminiApiError,
    GeminiCreditCircuitOpenError,
    GeminiRateLimitError,
    _error_from_http,
    google_usage_context,
    is_credit_depletion_error,
    post_google_model,
    probe_google_credit,
)
from meme_arsenal.media import process_pending_media
from meme_arsenal.models import GeminiCallError
from meme_arsenal.telegram_importer import import_fixture_messages


def test_gemini_rate_limit_error_reads_retry_hint() -> None:
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "message": (
                    "Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests. "
                    "Please retry in 11.046792613s."
                ),
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [
                            {
                                "quotaMetric": (
                                    "generativelanguage.googleapis.com/"
                                    "generate_content_free_tier_requests"
                                )
                            }
                        ],
                    }
                ],
            }
        }
    )
    http_error = HTTPError("https://generativelanguage.googleapis.com", 429, "Too Many Requests", {}, None)

    error = _error_from_http("generateContent", http_error, body)

    assert isinstance(error, GeminiRateLimitError)
    assert error.status_code == 429
    assert error.status == "RESOURCE_EXHAUSTED"
    assert error.retry_after_seconds == pytest.approx(11.046792613)
    assert error.quota_metric == "generativelanguage.googleapis.com/generate_content_free_tier_requests"


def test_gemini_credit_depletion_is_not_retried(monkeypatch, settings) -> None:
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "message": "Your prepayment credits are depleted. Please go to AI Studio to increase your limit.",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    )
    attempts = 0

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(body.encode("utf-8")))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    live_settings = replace(settings, google_api_key="test-google-key", ai_max_retries=3)
    with pytest.raises(GeminiRateLimitError) as caught:
        post_google_model(
            live_settings,
            "gemini-3.1-flash-image",
            "generateContent",
            {"contents": [{"role": "user", "parts": [{"text": "make a remix"}]}]},
        )

    assert attempts == 1
    assert is_credit_depletion_error(caught.value)
    assert ai_circuit_status(live_settings).is_open

    with pytest.raises(GeminiCreditCircuitOpenError):
        post_google_model(
            live_settings,
            "gemini-3.1-flash-image",
            "generateContent",
            {"contents": [{"role": "user", "parts": [{"text": "try again"}]}]},
        )
    assert attempts == 1


def test_credit_probe_obeys_cooldown_and_single_lease(monkeypatch, settings) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        ai_credit_probe_interval_seconds=21_600,
    )
    open_ai_credit_circuit(live_settings, now=now)

    def unexpected_urlopen(*_args, **_kwargs):
        raise AssertionError("cooldown must not make a provider request")

    monkeypatch.setattr("urllib.request.urlopen", unexpected_urlopen)
    result = probe_google_credit(live_settings, now=now + timedelta(hours=1))
    assert result.outcome == "cooldown"
    assert result.request_made is False

    first = acquire_ai_credit_probe(live_settings, force=True, now=now + timedelta(hours=1))
    second = acquire_ai_credit_probe(live_settings, force=True, now=now + timedelta(hours=1))
    assert first.acquired is True
    assert second.acquired is False
    assert second.outcome == "probe_in_progress"


def test_successful_credit_probe_closes_circuit_with_one_request(monkeypatch, settings) -> None:
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        ai_max_retries=3,
    )
    open_ai_credit_circuit(live_settings)
    requests: list[dict[str, object]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}).encode()

    def fake_urlopen(request, timeout):
        requests.append({"body": json.loads(request.data), "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = probe_google_credit(live_settings, force=True)

    assert result.outcome == "recovered"
    assert result.request_made is True
    assert result.recovered is True
    assert len(requests) == 1
    assert requests[0]["body"]["generationConfig"]["maxOutputTokens"] == 1
    assert ai_circuit_status(live_settings).is_open is False


def test_failed_credit_probe_is_not_retried(monkeypatch, settings) -> None:
    body = json.dumps(
        {
            "error": {
                "code": 429,
                "message": "Your prepayment credits are depleted.",
                "status": "RESOURCE_EXHAUSTED",
            }
        }
    )
    live_settings = replace(
        settings,
        ai_provider="google",
        google_api_key="test-google-key",
        ai_max_retries=3,
    )
    open_ai_credit_circuit(live_settings)
    attempts = 0

    def fake_urlopen(request, timeout):
        nonlocal attempts
        attempts += 1
        raise HTTPError(request.full_url, 429, "Too Many Requests", {}, BytesIO(body.encode()))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = probe_google_credit(live_settings, force=True)

    assert result.outcome == "failed"
    assert result.request_made is True
    assert result.recovered is False
    assert attempts == 1
    status = ai_circuit_status(live_settings)
    assert status.is_open
    assert status.next_probe_at is not None
    assert status.probe_lease_until is None


def test_post_google_model_uses_current_v1beta_endpoint(monkeypatch, settings) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.load(BytesIO(request.data))
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = post_google_model(
        replace(settings, google_api_key="test-google-key"),
        "gemini-3.1-flash-image",
        "generateContent",
        {
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "responseFormat": {"image": {"aspectRatio": "1:1", "imageSize": "1K"}},
            }
        },
    )

    assert response == {"ok": True}
    assert captured["url"] == (
        "https://generativelanguage.googleapis.com/v1beta/"
        "models/gemini-3.1-flash-image:generateContent"
    )
    assert captured["body"] == {
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"],
            "responseFormat": {"image": {"aspectRatio": "1:1", "imageSize": "1K"}},
        }
    }


def test_google_usage_logs_tokens_with_job_context_without_content(monkeypatch, settings) -> None:
    messages = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def read(self):
            return json.dumps({"candidates": [{"text": "private-result"}],
                               "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 20,
                                                 "totalTokenCount": 30, "private": "hidden"}}).encode()

    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: FakeResponse())
    monkeypatch.setattr("meme_arsenal.logging.console.print", lambda message, **_kwargs: messages.append(message))
    with google_usage_context(job_id="job-123", operation="remix"):
        post_google_model(replace(settings, google_api_key="secret-key"), "test-model", "generateContent", {})
    post_google_model(replace(settings, google_api_key="secret-key"), "test-model", "generateContent", {})
    usage = [json.loads(message.removeprefix("ai_usage ")) for message in messages if message.startswith("ai_usage ")]
    assert usage[0]["job_id"] == "job-123"
    assert usage[0]["tokens"]["totalTokenCount"] == 30
    assert "job_id" not in usage[1]
    assert not any(value in json.dumps(usage) for value in ("secret-key", "private-result", "hidden"))


def test_post_google_model_logs_gemini_http_errors(monkeypatch, settings) -> None:
    body = json.dumps(
        {
            "error": {
                "code": 400,
                "message": "Unable to process input image. Please retry.",
                "status": "INVALID_ARGUMENT",
            }
        }
    )

    def fake_urlopen(request, timeout):
        raise HTTPError(request.full_url, 400, "Bad Request", {}, BytesIO(body.encode("utf-8")))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    prompt_text = "describe this " + ("x" * 1500)
    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt_text},
                    {"inline_data": {"mime_type": "image/png", "data": "x" * 2048}},
                ],
            }
        ]
    }

    with pytest.raises(GeminiApiError):
        post_google_model(
            replace(settings, google_api_key="test-google-key", ai_max_retries=0),
            "gemini-3.1-flash-image",
            "generateContent",
            payload,
        )

    with session_scope(settings) as session:
        record = session.scalars(select(GeminiCallError)).one()

    assert record.model == "gemini-3.1-flash-image"
    assert record.method == "generateContent"
    assert record.attempt == 1
    assert record.max_attempts == 1
    assert record.will_retry is False
    assert record.status_code == 400
    assert record.status == "INVALID_ARGUMENT"
    assert "Unable to process input image" in record.error_message
    assert record.response_json["error"]["status"] == "INVALID_ARGUMENT"
    assert record.request_json["contents"][0]["parts"][0]["text"] == prompt_text
    inline_data = record.request_json["contents"][0]["parts"][1]["inline_data"]
    assert inline_data["mime_type"] == "image/png"
    assert inline_data["data"] == "<inline data omitted: 2048 chars>"


def test_post_google_model_logs_blocked_success_responses(monkeypatch, settings) -> None:
    response_body = {
        "candidates": [
            {
                "finishReason": "IMAGE_SAFETY",
                "content": {"parts": []},
            }
        ]
    }

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(response_body).encode("utf-8")

    def fake_urlopen(request, timeout):
        return FakeResponse()

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    response = post_google_model(
        replace(settings, google_api_key="test-google-key", ai_max_retries=0),
        "gemini-3.1-flash-image",
        "generateContent",
        {"contents": [{"role": "user", "parts": [{"text": "make it safe"}]}]},
    )

    with session_scope(settings) as session:
        record = session.scalars(select(GeminiCallError)).one()

    assert response == response_body
    assert record.status_code == 200
    assert record.status == "IMAGE_SAFETY"
    assert record.error_message == "Gemini API generateContent returned blocked response: IMAGE_SAFETY"
    assert record.response_json["candidates"][0]["finishReason"] == "IMAGE_SAFETY"


def test_classify_stops_early_on_gemini_quota(monkeypatch, settings, fixture_dir) -> None:
    live_settings = replace(
        settings,
        ai_provider="google",
        ai_model="gemini-3.5-flash",
        google_api_key="test-google-key",
        ai_request_delay_seconds=0,
        ai_stop_on_rate_limit=True,
    )
    attempts: list[int] = []

    def fake_classify_media_item(session, settings, media_item, force=False):
        attempts.append(media_item.id)
        raise GeminiRateLimitError(
            "quota exhausted",
            retry_after_seconds=11.0,
            quota_metric="generativelanguage.googleapis.com/generate_content_free_tier_requests",
            status_code=429,
            status="RESOURCE_EXHAUSTED",
        )

    monkeypatch.setattr("meme_arsenal.ai.classify_media_item", fake_classify_media_item)

    with session_scope(live_settings) as session:
        import_fixture_messages(session, live_settings, fixture_dir)
        process_pending_media(session, live_settings)
        report = classify_pending_media(session, live_settings)

    assert len(attempts) == 1
    assert report["classified"] == 0
    assert report["failed"] == 1
    assert report["rate_limited"] is True
    assert report["stopped_early"] is True
    assert report["remaining_candidates"] == 1
    assert report["retry_after_seconds"] == 11.0
    assert report["failed_items"][0]["quota_metric"] == (
        "generativelanguage.googleapis.com/generate_content_free_tier_requests"
    )
