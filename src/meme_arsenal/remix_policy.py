"""Actual generation text. Bump ALGORITHM_VERSION when procedural filtering,
rendering, selection or normalization changes compatibility. The digest is a
compatibility declaration, not automatic detection of arbitrary code changes.
"""

import hashlib
import json

ALGORITHM_VERSION = "remix-generation-1"
LEFT_LABEL = "Left"
RIGHT_LABEL = "Right"
VISUAL_SAMPLE = "visual sample"
SOURCE_MEME = "source meme {0}"
PREVIEW_IMAGE = "preview image"
THUMBNAIL_IMAGE = "thumbnail image"
ORIGINAL_IMAGE = "original image"
VIDEO_KEYFRAME = "video keyframe"
VIDEO_KEYFRAME_AT = "video keyframe around {0:.1f}s"
VISUAL_LABEL = "{0} source visual {1}. {2}."
METADATA_RETRY = "{0}\n\nThe source visuals were omitted on retry because Gemini could not process one attached image. Generate from the source metadata only."

SOFTENED_SOURCE_SUMMARY = "Harmless visual meme source {0}: {1} format, use only safe visual cues."
METADATA_ONLY_INSTRUCTION = (
    "No source visual is attached. Generate from these safe abstract cues only."
)
VISUAL_ONLY_INSTRUCTION = "Use only safe visual composition cues from the attached stills."
SURREAL_LEVEL_1 = "\n\nSurreal association level 1:\nMake the remix more surreal, associative, and oddly overconnected. Let the joke follow stranger meme logic, but keep the final image readable and harmless."
SURREAL_LEVEL_2 = "\n\nSurreal association level 2:\nPush the remix into a denser absurdist collage style with stronger associative jump-cuts and stranger visual bridges. Keep it playful and still resolvable at a glance."
SURREAL_LEVEL_3 = "\n\nSurreal association level 3:\nPush the remix into a highly associative maximal meme collage. Use unexpected juxtapositions, dense side-details, and playful dream-logic while keeping it non-graphic, non-hateful, and coherent enough to land as one image."
INTERNET_LEVEL_1 = "\n\nInternet-culture density level 1:\nAdd more internet-culture fluency: meme-native timing, platform-coded visual cues, posting-brain phrasing, and current-reference energy. Keep it accessible and avoid turning the image into a checklist of references."
INTERNET_LEVEL_2 = "\n\nInternet-culture density level 2:\nAdd layered internet-native details and references with stronger rhythm in the meme logic. Mix platform-coded cues, irony, and niche callbacks without overcrowding the image."
INTERNET_LEVEL_3 = "\n\nInternet-culture density level 3:\nMake the remix deeply internet-native in a playful way: layered irony, niche meme lore, fast-scrolling caption energy, and reference stacking. Let the culture density guide the joke while keeping one clear readable meme image."
TEXT_QUALITY = "\n\nVisible text quality when text is allowed:\nIf the remix uses any visible words, draft the exact wording before rendering it. Run an internal copy-edit and proofreading pass: check spelling, grammar, punctuation, sentence clarity, and meme timing; revise weak or awkward wording once; then render only the final proofread text in the image. Keep visible text short, legible, and intentional. Do not render draft text, proofreading notes, or revision notes. Set JSON visible_text to the exact final text rendered in the image."
NO_TEXT = "- No text: the generated image must contain no visible words, letters, captions, subtitles, labels, UI text, speech-bubble text, or watermarks. Carry the joke through composition, expression, action, and visual contrast. Set the returned JSON visible_text value to an empty string unless unavoidable source artifacts remain."
COMIC_STRIP = "- As a comic strip: compose the generated image as a comic strip with clear panels, gutters, sequential visual beats, and an obvious reading order. The panels should tell a coherent narrative from start to finish, with a setup, escalation or turn, and payoff. If No text is also selected, make it a silent comic strip with no visible words."
ANTI_SLOP = "- Anti-slop: make the generated meme feel deliberately composed rather than generic AI output. Use one clear focal point, readable silhouettes or gestures, purposeful visual hierarchy, coherent spatial layout, and source-specific details from both memes. Prefer meme-native framing, grounded texture, and joke-serving visual choices over glossy generic polish, meaningless background clutter, warped filler objects, over-smoothed faces, vague neon gradients, or random decoration. Keep the image funny and specific, not portfolio-art generic."
COMPOSITION_CONSTRAINTS = "\n\nHigh-priority user composition constraints:\nFollow these before ordinary caption, layout, and style preferences. They override the default instruction to include text when useful, while still returning the requested JSON metadata outside the image.\n{0}"
TWEAK_SLIDERS = "\n\nUser-selected remix tweak sliders:\nInterpret each value from 0 to 100 as creative direction, where 0 matches low_label and 100 matches high_label. Do not render slider labels as visible text unless they naturally improve the meme. These should only form a part of the thinking in remixing and complement the understanding of the topics covered by each source. Sliders complement.\n{0}"
MITIGATION = "\n\nRetry mitigation:\n{0}\nFollow this mitigation quietly. Do not mention blocked content, safety policy, or the mitigation in visible text."
FAILURE_CONTEXT = "\n\nRetry context:\n{0}\nUse this context only to adjust prompt safety and avoid repeating the previous failure pattern. Do not mention this context in the visible image or caption."
MAIN_PROMPT = "Create one new meme image that remixes the two source memes. Blend visual ideas from both inputs into a coherent single image. Use meme-style composition and include text only when it improves the joke. Avoid copying long text verbatim unless it is central to the remix. Keep the result playful and non-graphic. Harmless absurdity is key. The remix should be guided by both the slider values as well as a self-awareness of the ephemeral nature of memes. Be bold in structure and joke construction while staying harmless, non-graphic, and non-hateful. Prefer absurdity, parody, and visual surprise over shock value or real-world harm. Also return concise JSON metadata for the new archive card with this exact shape: summary, visible_text, narration, language, meme_format, tone, reaction_type, topics, keywords, people_or_characters, safety_flags, search_phrases, confidence. Make the tags useful for search and describe the generated image, not just the inputs.\n\nThe attached visuals may include multiple stills from a video; use them as context for one coherent final image, not as a storyboard. When original video inputs are attached, use them for motion, timing, and audio context; treat the stills as sampled visual anchors and fallback context.\n\nSource metadata:\n{0}{1}{2}{3}{4}{5}{6}{7}"
VISUALS_LABEL = "{0} source visuals follow. Media type: {1}. Visual count: {2}."
VIDEO_LABEL = "{0} source original video follows. Use it for motion, timing, and audio context. If Gemini blocks this request, the retry will use stills only."

MITIGATION_DESCRIPTION_1 = (
    "Starting with both sources reduced to safe abstract visual and meme-format cues."
)
MITIGATION_DESCRIPTION_2 = "Retrying with minimal metadata and relying on the attached visuals."
MITIGATION_DESCRIPTION_3 = "Metadata-only fallback"
MITIGATION_DESCRIPTION_4 = (
    "Retrying without attached visuals, using only safe abstract source cues."
)
MITIGATION_DESCRIPTION_5 = (
    "Retrying with the likely risky {0} source rewritten into safer abstract cues."
)
MITIGATION_DESCRIPTION_6 = (
    "Retrying with both sources reduced to harmless visual and meme-format cues."
)
MITIGATION_DESCRIPTION_7 = "Retrying with minimal metadata and relying on the attached visuals."
MITIGATION_DESCRIPTION_8 = "Metadata-only fallback"
MITIGATION_DESCRIPTION_9 = (
    "Retrying without attached visuals, using only safe abstract source cues."
)


MITIGATION_TARGETED_SOFT_RETRY = 'Targeted soft retry'
MITIGATION_SOFTEN_LEFT = 'soften-left'
MITIGATION_SOFTEN_RIGHT = 'soften-right'
MITIGATION_SOFTEN_BOTH = 'soften-both'
MITIGATION_SAFER_RETRY = 'Safer retry'
MITIGATION_VISUAL_ONLY = 'visual-only'
MITIGATION_VISUAL_ONLY_RETRY = 'Visual-only retry'
MITIGATION_METADATA_ONLY = 'metadata-only'
MITIGATION_SAFER_START = 'safer-start'
MITIGATION_SAFER_REQUEST = 'Safer request'
MITIGATION_LEFT = 'left'
MITIGATION_RIGHT = 'right'

def policy_digest() -> str:
    manifest = {
        key: value for key, value in globals().items() if key.isupper() and isinstance(value, str)
    }
    return hashlib.sha256(
        json.dumps(manifest, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
