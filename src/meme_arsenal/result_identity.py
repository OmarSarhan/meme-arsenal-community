"""Small private result views derived only from retained execution evidence."""

import re

from .remix_execution import RemixExecutionCapture, RemixGenerationProvenance
from .style_guidance import STYLE_GUIDANCE_VERSION, style_guidance_digest


def generation_identity(provenance=None, *, capture=None):
    try:
        if provenance is not None:
            parsed = RemixGenerationProvenance.parse(provenance)
            capture = parsed.capture
        else:
            capture = RemixExecutionCapture.parse(capture)
        if capture is None:
            return None
        return {
            "format": capture.format,
            "provider": capture.provider,
            "model": capture.model,
            "algorithm_version": capture.algorithm_version,
            "policy_digest": capture.policy_digest,
            "style_version": STYLE_GUIDANCE_VERSION,
            "style_digest": style_guidance_digest(capture.remix_style_guidance),
        }
    except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
        return None


def saved_result_identity(row, saved_metadata):
    """Retained generation is independent of the mutable selected classification."""
    generation = saved_metadata.get("generation") if isinstance(saved_metadata, dict) else None
    provenance = generation.get("provenance") if isinstance(generation, dict) else None
    classification = None
    if (row is not None and row.execution_provider in {"mock", "google", "openai"}
            and isinstance(row.compatibility_key, str)
            and re.fullmatch(r"[0-9a-f]{64}", row.compatibility_key)):
        classification = {
            "provider": row.execution_provider, "model": row.model,
            "prompt_version": row.prompt_version, "compatibility_key": row.compatibility_key,
        }
    return {"generation": generation_identity(provenance), "classification": classification}
