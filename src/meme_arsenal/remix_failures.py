from __future__ import annotations

import math

from .google_api import GeminiApiError, GeminiRateLimitError, is_credit_depletion_error
from .remix import RemixBlockedError, RemixError, RemixNoImageError


DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS = 60.0


def is_credential_error(exc: Exception) -> bool:
    if not isinstance(exc, GeminiApiError):
        return False
    return exc.status_code in (401, 403) or (
        exc.status_code == 400
        and any(value in str(exc).lower() for value in ("api key not valid", "api_key_invalid", "invalid api key"))
    )


def can_retry_with_mitigation(exc: Exception) -> bool:
    return isinstance(exc, (RemixBlockedError, RemixNoImageError)) or (
        isinstance(exc, RemixError) and "did not include an image" in str(exc)
    )


def _attempt_message(exc: Exception) -> str:
    if is_credential_error(exc):
        return "API credentials rejected"
    if isinstance(exc, RemixBlockedError):
        return "Safety filtered"
    if isinstance(exc, GeminiRateLimitError):
        return "Rate limited"
    if isinstance(exc, GeminiApiError):
        return "Gemini rejected the request"
    if isinstance(exc, RemixNoImageError):
        return "No image returned"
    if isinstance(exc, RemixError) and "did not include an image" in str(exc):
        return "No image returned"
    if isinstance(exc, RemixError) and "readable image preview" in str(exc):
        return "Missing preview"
    return "Attempt failed"


def _failure_explanation(exc: Exception) -> str:
    if is_credential_error(exc):
        return "Gemini rejected the API credentials or their permissions. No content retries were made."
    if is_credit_depletion_error(exc):
        return (
            "Gemini rejected the remix because the API account's prepayment credits are depleted. "
            "The selected sources are not the problem."
        )
    if isinstance(exc, GeminiRateLimitError):
        return "Gemini rate-limited the remix request. The selected sources are fine; the service asked us to wait before trying again."
    if isinstance(exc, RemixBlockedError):
        if exc.failure_explanation:
            return (
                f"{exc.failure_explanation} Automatic retry attempts used progressively safer source metadata, "
                "ending with a visual-only request that relies on the attached stills when needed."
            )
        return (
            "Gemini safety-filtered the remix. I retried automatically with progressively softer source metadata, "
            "ending with a visual-only request that relies on the attached stills instead of risky wording."
        )
    if isinstance(exc, GeminiApiError):
        status = f"HTTP {exc.status_code}" if exc.status_code else "Gemini"
        return (
            f"{status} rejected the remix request before an image was returned. This is usually caused by request "
            "shape, model availability, or content the service refused to process."
        )
    if isinstance(exc, RemixNoImageError) or (isinstance(exc, RemixError) and "did not include an image" in str(exc)):
        return (
            "Gemini responded without an image. That can happen when the model produces text only or silently refuses "
            "the generated output after reading the sources."
        )
    if isinstance(exc, RemixError) and "readable image preview" in str(exc):
        return "One selected source does not have a readable preview or still frame that can be sent to the remixer."
    return "The remix could not complete. The final error is shown below for debugging."


def _rate_limit_retry_after_seconds(exc: GeminiRateLimitError) -> float:
    value = exc.retry_after_seconds
    if value is None:
        return DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS
    return seconds if seconds > 0 else DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS


def _format_retry_after(seconds: float) -> str:
    total = max(1, int(math.ceil(seconds)))
    minutes, remainder = divmod(total, 60)
    if minutes and remainder:
        return f"{minutes}m {remainder:02d}s"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"


def _suggested_retry(exc: Exception, *, exhausted: bool) -> str:
    if is_credential_error(exc):
        return "Update the Gemini API key or its permissions, then retry."
    if isinstance(exc, GeminiApiError) and not isinstance(exc, GeminiRateLimitError):
        return "Check the provider error before retrying the request."
    if is_credit_depletion_error(exc):
        return "Add credits or update Gemini billing in AI Studio, then retry the same pair."
    if isinstance(exc, GeminiRateLimitError):
        return f"Wait {_format_retry_after(_rate_limit_retry_after_seconds(exc))}, then retry the same pair."
    if isinstance(exc, RemixBlockedError):
        if exhausted:
            return (
                "Try swapping the source that looks edgier, more text-heavy, or more news/disaster-coded. "
                "I already tried targeted softening, both-side softening, and visual-only prompting."
            )
        return "Retry again after the cooldown; the next automatic attempt will soften the likely risky source first."
    if isinstance(exc, RemixNoImageError) or (isinstance(exc, RemixError) and "did not include an image" in str(exc)):
        return "Retry once, or swap one source for a clearer still image so Gemini has less ambiguity."
    if isinstance(exc, RemixError) and "readable image preview" in str(exc):
        return "Pick an item with a thumbnail, preview, original image, or extracted video keyframe."
    return "Try again with one source swapped, preferably replacing the busier or riskier-looking input."
