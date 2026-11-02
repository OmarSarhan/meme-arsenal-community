from __future__ import annotations

import json
from typing import Any

REMIX_REJECTION_REASON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reason": {"type": "string"},
    },
    "required": ["reason"],
    "propertyOrdering": ["reason"],
}


def _remix_rejection_reason_prompt(context: dict[str, Any]) -> str:
    return (
        "A Gemini image remix attempt failed or was rejected. Infer the likely failure ground from "
        "this sanitized request context and return only concise JSON.\n\n"
        "Output shape: {\"reason\": \"2 to 5 words\"}.\n"
        "The reason must be user-facing, specific, and short enough for a small status chip. "
        "Do not return OTHER, do not mention Gemini, do not write a sentence, and do not give "
        "mitigation advice. Prefer concrete categories like Political mockery, Identity targeting, "
        "Violence cue, Sexual content, Risky source image, Text-only response, No image output, "
        "or Ambiguous safety policy.\n\n"
        f"Context:\n{json.dumps(context, ensure_ascii=False, indent=2)}"
    )


def remix_rejection_request(context: dict[str, Any]) -> dict[str, Any]:
    return {
        "contents": [
            {"role": "user", "parts": [{"text": _remix_rejection_reason_prompt(context)}]}
        ],
        "generationConfig": {
            "responseFormat": {
                "text": {
                    "mimeType": "APPLICATION_JSON",
                    "schema": REMIX_REJECTION_REASON_SCHEMA,
                }
            },
        },
    }
