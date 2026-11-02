from __future__ import annotations

from dataclasses import dataclass, replace
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
import json
import math
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .ai_circuit import (
    AiCircuitStatus,
    acquire_ai_credit_probe,
    ai_circuit_status,
    complete_ai_credit_probe,
    open_ai_credit_circuit,
)
from .config import Settings
from .remix_rejection_request import remix_rejection_request


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
RETRY_IN_RE = re.compile(r"retry in\s+([0-9]+(?:\.[0-9]+)?)s", re.IGNORECASE)
UNPROCESSABLE_INPUT_IMAGE_RE = re.compile(r"unable to process input image", re.IGNORECASE)
CREDIT_DEPLETION_RE = re.compile(
    r"\b(prepayment credits? (?:are )?depleted|credits? (?:are )?depleted|insufficient (?:prepaid )?credits?)\b",
    re.IGNORECASE,
)
GEMINI_BLOCK_FINISH_REASONS = {
    "BLOCKLIST",
    "IMAGE_SAFETY",
    "PROHIBITED_CONTENT",
    "RECITATION",
    "SAFETY",
    "SPII",
}
LOG_TEXT_LIMIT = 8000
LOG_STRING_LIMIT = 1200
LOG_LIST_LIMIT = 24
_LOCAL_CREDIT_CIRCUIT_UNPERSISTED = threading.Event()
_USAGE_CONTEXT: ContextVar[dict[str, Any]] = ContextVar("gemini_usage_context", default={})


@contextmanager
def google_usage_context(**context: Any):
    token = _USAGE_CONTEXT.set(context)
    try:
        yield
    finally:
        _USAGE_CONTEXT.reset(token)


def _record_usage(response: dict[str, Any], model: str, method: str, duration_ms: int) -> None:
    from .logging import console

    usage = response.get("usageMetadata")
    if not isinstance(usage, dict):
        return
    tokens = {key: value for key, value in usage.items() if key.endswith("TokenCount") and isinstance(value, int)}
    if tokens:
        console.print(
            "ai_usage " + json.dumps({"model": model, "method": method, "duration_ms": duration_ms,
                                     **_USAGE_CONTEXT.get(), "tokens": tokens}),
            markup=False, highlight=False, soft_wrap=True,
        )


class GeminiApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        status: str | None = None,
        response: dict[str, Any] | None = None,
        raw_body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.status = status
        self.response = response or {}
        self.raw_body = raw_body or ""


class GeminiRateLimitError(GeminiApiError):
    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        quota_metric: str | None = None,
        status_code: int | None = None,
        status: str | None = None,
        response: dict[str, Any] | None = None,
        raw_body: str | None = None,
    ) -> None:
        super().__init__(message, status_code=status_code, status=status, response=response, raw_body=raw_body)
        self.retry_after_seconds = retry_after_seconds
        self.quota_metric = quota_metric


class GeminiCreditCircuitOpenError(GeminiRateLimitError):
    pass


@dataclass(frozen=True)
class GeminiCreditProbeResult:
    outcome: str
    request_made: bool
    recovered: bool
    circuit: AiCircuitStatus | None
    operation: str | None = None
    model: str | None = None

    def payload(self) -> dict[str, object]:
        return {
            "outcome": self.outcome,
            "request_made": self.request_made,
            "recovered": self.recovered,
            "operation": self.operation,
            "model": self.model,
            "circuit": self.circuit.payload() if self.circuit else None,
        }


def is_unprocessable_input_image_error(error: BaseException) -> bool:
    if not isinstance(error, GeminiApiError):
        return False
    text = " ".join([str(error), error.raw_body, str(error.response)])
    return (
        error.status_code == 400
        and error.status == "INVALID_ARGUMENT"
        and bool(UNPROCESSABLE_INPUT_IMAGE_RE.search(text))
    )


def is_credit_depletion_error(error: BaseException) -> bool:
    if isinstance(error, GeminiCreditCircuitOpenError):
        return True
    if not isinstance(error, GeminiRateLimitError):
        return False
    text = " ".join([str(error), error.raw_body, str(error.response)])
    return bool(CREDIT_DEPLETION_RE.search(text))


def google_api_key(settings: Settings) -> str:
    key = settings.google_api_key
    if not key:
        raise RuntimeError("Google operations require GEMINI_API_KEY, GOOGLE_API_KEY, or AI_API_KEY")
    return key


def google_model_resource(model: str) -> str:
    name = model.strip()
    if not name:
        raise ValueError("Google model name cannot be empty")
    if name.startswith(("models/", "tunedModels/")):
        return name
    return f"models/{name}"


def _parse_json_error(body: str) -> dict[str, Any]:
    try:
        loaded = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _retry_delay_from_text(text: str) -> float | None:
    match = RETRY_IN_RE.search(text)
    return float(match.group(1)) if match else None


def _retry_delay_from_details(details: list[dict[str, Any]]) -> float | None:
    for detail in details:
        if detail.get("@type") != "type.googleapis.com/google.rpc.RetryInfo":
            continue
        retry_delay = detail.get("retryDelay")
        if isinstance(retry_delay, str) and retry_delay.endswith("s"):
            try:
                return float(retry_delay[:-1])
            except ValueError:
                return None
    return None


def _quota_metric_from_details(details: list[dict[str, Any]]) -> str | None:
    for detail in details:
        if detail.get("@type") != "type.googleapis.com/google.rpc.QuotaFailure":
            continue
        violations = detail.get("violations")
        if isinstance(violations, list) and violations:
            metric = violations[0].get("quotaMetric")
            if metric:
                return str(metric)
    return None


def _retry_after_from_headers(headers: Any) -> float | None:
    value = headers.get("Retry-After") if headers else None
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _error_from_http(method: str, exc: urllib.error.HTTPError, body: str) -> GeminiApiError:
    parsed = _parse_json_error(body)
    error = parsed.get("error") if isinstance(parsed.get("error"), dict) else {}
    message = str(error.get("message") or body[:1000])
    status = str(error.get("status") or "")
    details = error.get("details") if isinstance(error.get("details"), list) else []
    retry_after = (
        _retry_after_from_headers(exc.headers)
        or _retry_delay_from_details(details)
        or _retry_delay_from_text(message)
    )
    quota_metric = _quota_metric_from_details(details)
    rendered = f"Gemini API {method} failed with HTTP {exc.code}: {body[:1000]}"
    if exc.code == 429 or status == "RESOURCE_EXHAUSTED":
        return GeminiRateLimitError(
            rendered,
            retry_after_seconds=retry_after,
            quota_metric=quota_metric,
            status_code=exc.code,
            status=status,
            response=parsed,
            raw_body=body,
        )
    return GeminiApiError(
        rendered,
        status_code=exc.code,
        status=status,
        response=parsed,
        raw_body=body,
    )


def _backoff_seconds(settings: Settings, attempt: int, error: GeminiApiError | None = None) -> float:
    configured = getattr(error, "retry_after_seconds", None)
    if configured is None:
        configured = min(2 ** (attempt - 1), settings.ai_max_retry_delay_seconds)
    jitter = random.uniform(0.1, 0.9)
    return min(float(configured) + jitter, settings.ai_max_retry_delay_seconds)


def _truncate_for_log(value: str, limit: int = LOG_STRING_LIMIT) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit]}... <truncated {len(value) - limit} chars>"


def _sanitize_for_log(value: Any, *, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, child in value.items():
            key_text = str(key)
            lower_key = key_text.lower()
            if lower_key in {"x-goog-api-key", "api_key", "apikey", "key"}:
                sanitized[key_text] = "<redacted>"
                continue
            if lower_key == "data" and parent_key in {"inline_data", "inlineData"}:
                sanitized[key_text] = f"<inline data omitted: {len(str(child))} chars>"
                continue
            sanitized[key_text] = _sanitize_for_log(child, parent_key=key_text)
        return sanitized
    if isinstance(value, list):
        items = [_sanitize_for_log(item, parent_key=parent_key) for item in value[:LOG_LIST_LIMIT]]
        if len(value) > LOG_LIST_LIMIT:
            items.append({"_omitted_items": len(value) - LOG_LIST_LIMIT})
        return items
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value:
            return f"<data url omitted: {len(value)} chars>"
        return _truncate_for_log(value, LOG_TEXT_LIMIT if parent_key == "text" else LOG_STRING_LIMIT)
    return value


def _gemini_response_block_reason(response: dict[str, Any]) -> str | None:
    prompt_feedback = response.get("promptFeedback")
    if isinstance(prompt_feedback, dict):
        block_reason = prompt_feedback.get("blockReason")
        if block_reason:
            return str(block_reason)

    candidates = response.get("candidates")
    if not isinstance(candidates, list):
        return None
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        finish_reason = candidate.get("finishReason")
        if finish_reason and str(finish_reason) in GEMINI_BLOCK_FINISH_REASONS:
            return str(finish_reason)
    return None


def _record_gemini_call_error(
    settings: Settings,
    *,
    model: str,
    method: str,
    url: str,
    payload: dict[str, Any],
    error: BaseException,
    attempt: int,
    max_attempts: int,
    will_retry: bool,
    duration_ms: int | None,
) -> None:
    try:
        from sqlalchemy.orm import sessionmaker

        from .db import ensure_schema, get_engine
        from .models import GeminiCallError

        status_code = error.status_code if isinstance(error, GeminiApiError) else None
        status = error.status if isinstance(error, GeminiApiError) else None
        raw_body = error.raw_body if isinstance(error, GeminiApiError) else ""
        response = error.response if isinstance(error, GeminiApiError) else {}
        retry_after_seconds = (
            error.retry_after_seconds if isinstance(error, GeminiRateLimitError) else None
        )
        quota_metric = error.quota_metric if isinstance(error, GeminiRateLimitError) else None

        engine = get_engine(settings)
        ensure_schema(settings, engine)
        SessionLocal = sessionmaker(bind=engine, future=True, expire_on_commit=False)
        with SessionLocal() as session:
            session.add(
                GeminiCallError(
                    model=model,
                    method=method,
                    url=url,
                    attempt=attempt,
                    max_attempts=max_attempts,
                    will_retry=will_retry,
                    duration_ms=duration_ms,
                    status_code=status_code,
                    status=status or None,
                    error_type=type(error).__name__,
                    error_message=_truncate_for_log(str(error), LOG_TEXT_LIMIT),
                    raw_body=_truncate_for_log(raw_body, LOG_TEXT_LIMIT),
                    retry_after_seconds=retry_after_seconds,
                    quota_metric=quota_metric,
                    request_json=_sanitize_for_log(payload),
                    response_json=_sanitize_for_log(response),
                )
            )
            session.commit()
    except Exception:
        return


def _credit_circuit_error(status: AiCircuitStatus) -> GeminiCreditCircuitOpenError:
    return GeminiCreditCircuitOpenError(
        "Gemini requests are paused until the credit recovery probe succeeds.",
        retry_after_seconds=status.retry_after_seconds(),
        status="CREDIT_CIRCUIT_OPEN",
    )


def _assert_credit_circuit_closed(settings: Settings) -> None:
    if _LOCAL_CREDIT_CIRCUIT_UNPERSISTED.is_set():
        try:
            status = open_ai_credit_circuit(settings)
        except Exception as exc:
            raise GeminiCreditCircuitOpenError(
                "Gemini requests are paused because the credit circuit state is unavailable.",
                status="CREDIT_CIRCUIT_UNAVAILABLE",
            ) from exc
        _LOCAL_CREDIT_CIRCUIT_UNPERSISTED.clear()
        raise _credit_circuit_error(status)
    try:
        status = ai_circuit_status(settings)
    except Exception as exc:
        raise GeminiCreditCircuitOpenError(
            "Gemini requests are paused because the credit circuit state is unavailable.",
            status="CREDIT_CIRCUIT_UNAVAILABLE",
        ) from exc
    if status.is_open:
        raise _credit_circuit_error(status)


def google_work_paused(settings: Settings) -> bool:
    """Conservatively check the common gate, including unpersisted depletion."""
    try:
        _assert_credit_circuit_closed(settings)
    except GeminiCreditCircuitOpenError:
        return True
    return False


def _open_credit_circuit(settings: Settings) -> None:
    try:
        open_ai_credit_circuit(settings)
    except Exception:
        _LOCAL_CREDIT_CIRCUIT_UNPERSISTED.set()
        return
    _LOCAL_CREDIT_CIRCUIT_UNPERSISTED.clear()


def post_google_model(settings: Settings, model: str, method: str, payload: dict[str, Any]) -> dict[str, Any]:
    _assert_credit_circuit_closed(settings)
    return _post_google_model_unchecked(settings, model, method, payload)


def probe_google_credit(
    settings: Settings,
    *,
    force: bool = False,
    now: datetime | None = None,
    operation: str | None = None,
) -> GeminiCreditProbeResult:
    providers = {
        "classification": settings.effective_classification_provider,
        "embedding": settings.effective_embedding_provider,
        "remix": settings.effective_remix_provider,
    }
    if operation is None:
        operation = next((name for name, provider in providers.items()
                          if provider == "google" and
                          (name != "embedding" or settings.online_embeddings_enabled)), None)
        if operation is None:
            return GeminiCreditProbeResult("disabled", False, False, None)
    elif providers.get(operation) != "google":
        return GeminiCreditProbeResult("unsupported", False, False, None, operation)

    model = {"classification": settings.ai_model, "embedding": settings.embedding_model,
             "remix": settings.remix_model}[operation]

    def result(
        outcome: str,
        made: bool = False,
        recovered: bool = False,
        status: AiCircuitStatus | None = None,
    ) -> GeminiCreditProbeResult:
        return GeminiCreditProbeResult(outcome, made, recovered, status, operation, model)

    try:
        if not google_api_key(settings).strip():
            raise ValueError("Empty Google credential")
        resource = google_model_resource(model)
    except Exception:
        return result("unavailable")
    if operation == "embedding":
        method = "embedContent"
        payload = {"model": resource, "content": {"parts": [{"text": "Credit probe."}]},
                   "outputDimensionality": settings.embedding_dimensions}
    elif operation == "remix":
        method = "generateContent"
        payload = remix_rejection_request({"failure_type": "text_only_response"})
    else:
        method = "generateContent"
        payload = {
            "contents": [{"role": "user", "parts": [{"text": "Reply with OK."}]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 1},
        }
    try:
        lease = acquire_ai_credit_probe(settings, force=force, now=now)
    except Exception:
        return result("unavailable")
    if not lease.acquired or lease.token is None:
        return result(lease.outcome, status=lease.status)

    probe_settings = replace(settings, ai_max_retries=0)
    try:
        response = _post_google_model_unchecked(probe_settings, model, method, payload)
        if not _credit_probe_response_valid(operation, response):
            raise ValueError("Inconclusive credit probe response")
    except Exception as exc:  # noqa: BLE001 - probe failures extend the persisted pause.
        try:
            if not is_credit_depletion_error(exc):
                completion = complete_ai_credit_probe(settings, lease.token, recovered=False, now=now)
                status = completion.status
            else:
                status = ai_circuit_status(settings)
        except Exception:
            return result("unavailable", True)
        return result("failed", True, status=status)

    try:
        completion = complete_ai_credit_probe(settings, lease.token, recovered=True, now=now)
    except Exception:
        return result("unavailable", True)
    if not completion.applied:
        return result("stale", True, status=completion.status)
    _LOCAL_CREDIT_CIRCUIT_UNPERSISTED.clear()
    return result("recovered", True, True, completion.status)


def _credit_probe_response_valid(operation: str, response: Any) -> bool:
    if not isinstance(response, dict) or "error" in response or _gemini_response_block_reason(response):
        return False
    if operation == "embedding":
        embedding = response.get("embedding")
        values = embedding.get("values") if isinstance(embedding, dict) else None
        return (isinstance(values, list) and bool(values) and
                all(type(v) in (int, float) and math.isfinite(v) for v in values))
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        return False
    valid = False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            return False
        content = candidate.get("content", {})
        if not isinstance(content, dict):
            return False
        parts = content.get("parts", [])
        if not isinstance(parts, list) or any(not isinstance(p, dict) for p in parts):
            return False
        if any("text" in p and not isinstance(p["text"], str) for p in parts):
            return False
        text = "\n".join(p["text"] for p in parts if isinstance(p.get("text"), str)).strip()
        if operation == "classification":
            if text or candidate.get("finishReason") == "MAX_TOKENS":
                valid = True
        elif text:
            try:
                parsed = json.loads(text)
            except ValueError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("reason"), str) and parsed["reason"].strip():
                valid = True
    return valid


def _post_google_model_unchecked(
    settings: Settings,
    model: str,
    method: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    model_path = urllib.parse.quote(google_model_resource(model), safe="/")
    url = f"{GEMINI_API_BASE}/{model_path}:{method}"
    body = json.dumps(payload).encode("utf-8")
    max_attempts = max(1, settings.ai_max_retries + 1)
    try:
        key = google_api_key(settings)
    except Exception as exc:
        _record_gemini_call_error(
            settings,
            model=model,
            method=method,
            url=url,
            payload=payload,
            error=exc,
            attempt=0,
            max_attempts=max_attempts,
            will_retry=False,
            duration_ms=None,
        )
        raise
    for attempt in range(1, max_attempts + 1):
        request = urllib.request.Request(
            url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": key,
            },
            method="POST",
        )
        started_at = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                response_text = response.read().decode("utf-8")
                try:
                    parsed_response = json.loads(response_text)
                except json.JSONDecodeError as exc:
                    error = GeminiApiError(
                        f"Gemini API {method} returned non-JSON response: {response_text[:1000]}",
                        raw_body=response_text,
                    )
                    _record_gemini_call_error(
                        settings,
                        model=model,
                        method=method,
                        url=url,
                        payload=payload,
                        error=error,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        will_retry=False,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                    raise error from exc

                _record_usage(parsed_response, model, method, int((time.monotonic() - started_at) * 1000))
                block_reason = _gemini_response_block_reason(parsed_response)
                if block_reason:
                    _record_gemini_call_error(
                        settings,
                        model=model,
                        method=method,
                        url=url,
                        payload=payload,
                        error=GeminiApiError(
                            f"Gemini API {method} returned blocked response: {block_reason}",
                            status_code=getattr(response, "status", 200),
                            status=block_reason,
                            response=parsed_response,
                            raw_body=response_text,
                        ),
                        attempt=attempt,
                        max_attempts=max_attempts,
                        will_retry=False,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                    )
                return parsed_response
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            error = _error_from_http(method, exc, error_body)
            if is_credit_depletion_error(error):
                _open_credit_circuit(settings)
            will_retry = (
                exc.code in TRANSIENT_STATUS_CODES
                and attempt < max_attempts
                and not is_credit_depletion_error(error)
            )
            _record_gemini_call_error(
                settings,
                model=model,
                method=method,
                url=url,
                payload=payload,
                error=error,
                attempt=attempt,
                max_attempts=max_attempts,
                will_retry=will_retry,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            if will_retry:
                time.sleep(_backoff_seconds(settings, attempt, error))
                continue
            raise error from exc
        except urllib.error.URLError as exc:
            error = GeminiApiError(f"Gemini API {method} request failed: {exc.reason}")
            will_retry = attempt < max_attempts
            _record_gemini_call_error(
                settings,
                model=model,
                method=method,
                url=url,
                payload=payload,
                error=error,
                attempt=attempt,
                max_attempts=max_attempts,
                will_retry=will_retry,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            if will_retry:
                time.sleep(_backoff_seconds(settings, attempt))
                continue
            raise error from exc
    error = GeminiApiError(f"Gemini API {method} request failed after retries")
    _record_gemini_call_error(
        settings,
        model=model,
        method=method,
        url=url,
        payload=payload,
        error=error,
        attempt=max_attempts,
        max_attempts=max_attempts,
        will_retry=False,
        duration_ms=None,
    )
    raise error
