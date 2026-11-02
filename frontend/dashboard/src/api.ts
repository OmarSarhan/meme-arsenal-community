import type { CatalogItem, PublicShareState, RemixAttempt, RemixJob, RemixJobStatus, RemixResultPayload, RemixSource, RemixTweaks } from "./types";

export type RemixSaveResponse = {
  status: "saving" | "saved" | "failed";
  job?: RemixJob;
  item?: CatalogItem;
  embedding_error?: string | null;
};

const MOBILE_PLAYBACK_PATH_VERSION = "mobile-v2";

function asText(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asTextOrNull(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function versionedPlaybackPath(value: unknown): string | null {
  const path = asTextOrNull(value);
  if (!path) return null;
  const match = path.match(/^(\/api\/items\/\d+\/playback)(?:\?.*)?$/);
  return match ? `${match[1]}/${MOBILE_PLAYBACK_PATH_VERSION}` : path;
}

export function apiErrorMessage(payload: unknown, fallbackStatus: number): string {
  if (payload && typeof payload === "object") {
    const value = payload as Record<string, unknown>;
    const detail = value.detail;
    if (detail && typeof detail === "object") {
      const message = asText((detail as Record<string, unknown>).message);
      if (message) return message;
    }
    const detailText = asText(detail);
    if (detailText) return detailText;
    const error = asText(value.error);
    if (error) return error;
  }
  return String(fallbackStatus);
}

export class ApiRequestError extends Error {
  readonly status: number;
  readonly retryAfterSeconds: number | null;

  constructor(payload: unknown, response: Response) {
    super(apiErrorMessage(payload, response.status));
    this.name = "ApiRequestError";
    this.status = response.status;
    const detail = payload && typeof payload === "object" ? (payload as Record<string, unknown>).detail : null;
    const retry = detail && typeof detail === "object" ? (detail as Record<string, unknown>).retry_after_seconds : null;
    this.retryAfterSeconds = asNullableNumber(retry) ?? asNullableNumber(response.headers.get("Retry-After"));
  }
}

function asNumber(value: unknown, fallback: number = 0): number {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : fallback;
  }
  return fallback;
}

export function asNullableNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is string => typeof entry === "string");
}

function asRemixSource(raw: unknown): RemixSource {
  if (!raw || typeof raw !== "object") {
    return { id: 0, title: "", media_type: "", thumbnail_path: null, preview_path: null, original_path: null };
  }
  const item = raw as Record<string, unknown>;
  return {
    id: asNumber(item.id, 0),
    title: asText(item.title),
    media_type: asText(item.media_type),
    thumbnail_path: asTextOrNull(item.thumbnail_path),
    preview_path: asTextOrNull(item.preview_path),
    original_path: asTextOrNull(item.original_path),
  };
}

export function asCatalogItem(raw: unknown): CatalogItem {
  const value = raw as Record<string, unknown>;
  if (!value || typeof value !== "object") {
    return {
      id: 0,
      telegram_message_id: 0,
      chat_id: "",
      telegram_post_url: null,
      date: null,
      caption: "",
      source_url: null,
      source_display_url: null,
      source_site: null,
      source_title: null,
      source_description: null,
      source_embed_url: null,
      origin: "telegram",
      link_preview: {},
      media_type: "image",
      mime_type: null,
      thumbnail_path: null,
      preview_path: null,
      hover_preview_path: null,
      playback_path: null,
      original_path: null,
      duration_seconds: 0,
      width: 0,
      height: 0,
      size_bytes: 0,
      duplicate_group_id: null,
      processing_status: "processed",
      public_status: "published",
      classification_status: "missing",
      visibility: "public",
      classification_block_reason: null,
      summary: "",
      visible_text: "",
      narration: "",
      language: null,
      meme_format: null,
      tone: [],
      reaction_type: [],
      topics: [],
      keywords: [],
      people_or_characters: [],
      safety_flags: [],
      search_phrases: [],
      favorite: false,
      archived: false,
      manual_tags: [],
      notes: "",
      server_tags: [],
      remix_sources: [],
      remix_used_by: [],
      remix_tweaks: null,
    };
  }
  return {
    id: asNumber(value.id, 0),
    telegram_message_id: asNumber(value.telegram_message_id, 0),
    chat_id: asText(value.chat_id),
    telegram_post_url: asTextOrNull(value.telegram_post_url),
    date: asTextOrNull(value.date),
    saved_at: asTextOrNull(value.saved_at),
    added_at: asTextOrNull(value.added_at),
    caption: asText(value.caption),
    source_url: asTextOrNull(value.source_url),
    source_display_url: asTextOrNull(value.source_display_url),
    source_site: asTextOrNull(value.source_site),
    source_title: asTextOrNull(value.source_title),
    source_description: asTextOrNull(value.source_description),
    source_embed_url: asTextOrNull(value.source_embed_url),
    grouped_id: value.grouped_id === null ? null : asText(value.grouped_id),
    forwarded_from: value.forwarded_from === null ? null : asText(value.forwarded_from),
    origin: asText(value.origin),
    link_preview:
      typeof value.link_preview === "object" && value.link_preview !== null
        ? (value.link_preview as Record<string, unknown>)
        : {},
    media_type: asText(value.media_type),
    mime_type: asTextOrNull(value.mime_type),
    thumbnail_path: asTextOrNull(value.thumbnail_path),
    preview_path: asTextOrNull(value.preview_path),
    hover_preview_path: asTextOrNull(value.hover_preview_path),
    playback_path: versionedPlaybackPath(value.playback_path),
    original_path: asTextOrNull(value.original_path),
    duration_seconds: asNullableNumber(value.duration_seconds) ?? 0,
    width: asNullableNumber(value.width) ?? 0,
    height: asNullableNumber(value.height) ?? 0,
    size_bytes: asNullableNumber(value.size_bytes) ?? 0,
    duplicate_group_id: asTextOrNull(value.duplicate_group_id),
    processing_status: asText(value.processing_status),
    public_status: asText(value.public_status),
    classification_status: asText(value.classification_status),
    visibility: asText(value.visibility),
    classification_block_reason: asTextOrNull(value.classification_block_reason),
    summary: asText(value.summary),
    visible_text: asText(value.visible_text),
    narration: asText(value.narration),
    language: asTextOrNull(value.language),
    meme_format: asTextOrNull(value.meme_format),
    tone: asStringList(value.tone),
    reaction_type: asStringList(value.reaction_type),
    topics: asStringList(value.topics),
    keywords: asStringList(value.keywords),
    people_or_characters: asStringList(value.people_or_characters),
    safety_flags: asStringList(value.safety_flags),
    search_phrases: asStringList(value.search_phrases),
    sha256: asText(value.sha256),
    perceptual_hash: asText(value.perceptual_hash),
    favorite: value.favorite === true,
    archived: value.archived === true,
    manual_tags: asStringList(value.manual_tags),
    notes: asText(value.notes),
    server_tags: asStringList(value.server_tags),
    remix_sources: asRemixSources(value.remix_sources),
    remix_used_by: asRemixSources(value.remix_used_by),
    remix_tweaks: asRemixTweaks(value.remix_tweaks),
  };
}

function asRemixSources(value: unknown): RemixSource[] {
  return Array.isArray(value) ? value.map(asRemixSource) : [];
}

function asRemixAttempt(raw: unknown): RemixAttempt {
  const value = raw as Record<string, unknown>;
  if (!value || typeof value !== "object") {
    return {
      attempt: 0,
      mitigation_id: "",
      label: "",
      description: "",
      status: "failed",
      message: "",
      error: null,
      block_reason: null,
      rejection_reason: null,
    };
  }
  return {
    attempt: asNumber(value.attempt, 0),
    mitigation_id: asText(value.mitigation_id),
    label: asText(value.label),
    description: asText(value.description),
    status: asText(value.status) || "failed",
    message: asText(value.message),
    error: asTextOrNull(value.error),
    block_reason: asTextOrNull(value.block_reason),
    rejection_reason: asTextOrNull(value.rejection_reason),
  };
}

function asRemixResult(raw: unknown): RemixResultPayload | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const value = raw as Record<string, unknown>;
  return {
    summary: asText(value.summary),
    visible_text: asText(value.visible_text),
    narration: asText(value.narration),
    language: asTextOrNull(value.language),
    meme_format: asTextOrNull(value.meme_format),
    tone: asStringList(value.tone),
    reaction_type: asStringList(value.reaction_type),
    topics: asStringList(value.topics),
    keywords: asStringList(value.keywords),
    people_or_characters: asStringList(value.people_or_characters),
    safety_flags: asStringList(value.safety_flags),
    search_phrases: asStringList(value.search_phrases),
    confidence: asNumber(value.confidence, 0),
    image_path: asText(value.image_path),
    safety_mode: asText(value.safety_mode),
  };
}

function asRemixTweaks(raw: unknown): RemixTweaks | null {
  if (!raw || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  const controls = Array.isArray(value.controls) ? value.controls : [];
  const values = value.values && typeof value.values === "object" ? value.values as Record<string, number> : {};
  return {
    analysis_prompt_version: asText(value.analysis_prompt_version),
    summary: asText(value.summary),
    controls: controls as RemixTweaks["controls"],
    values,
    schizo_level: asNullableNumber(value.schizo_level) ?? undefined,
    chronically_online_level: asNullableNumber(value.chronically_online_level) ?? undefined,
    no_text: value.no_text === true,
    comic_strip: value.comic_strip === true,
    anti_slop: value.anti_slop === true,
  };
}

function asRemixJobStatus(value: unknown): RemixJobStatus {
  const next = asText(value);
  const options: Set<string> = new Set([
    "queued",
    "preparing_media",
    "calling_gemini",
    "ready",
    "rate_limited",
    "blocked",
    "failed",
    "saving",
    "saved",
  ]);
  return (options.has(next) ? next : "failed") as RemixJobStatus;
}

export function asRemixJob(raw: unknown): RemixJob {
  const value = raw as Record<string, unknown>;
  if (!value || typeof value !== "object") {
    return {
      id: "",
      left_id: 0,
      right_id: 0,
      safety_mode: "standard",
      status: "failed",
      message: "Invalid job payload",
      created_at: null,
      updated_at: null,
      retry_after_seconds: null,
      block_reason: null,
      rejection_reason: null,
      error: null,
      embedding_error: null,
      attempts: [],
      failure_explanation: null,
      suggested_retry: null,
      tweaks: undefined,
      result: undefined,
      image_url: undefined,
      saved_item: undefined,
    };
  }
  return {
    id: asText(value.id),
    left_id: asNumber(value.left_id, 0),
    right_id: asNumber(value.right_id, 0),
    safety_mode: asText(value.safety_mode),
    status: asRemixJobStatus(value.status),
    message: asText(value.message),
    created_at: asTextOrNull(value.created_at),
    updated_at: asTextOrNull(value.updated_at),
    retry_after_seconds: asNullableNumber(value.retry_after_seconds),
    block_reason: asTextOrNull(value.block_reason),
    rejection_reason: asTextOrNull(value.rejection_reason),
    error: asTextOrNull(value.error),
    embedding_error: asTextOrNull(value.embedding_error),
    attempts: Array.isArray(value.attempts) ? value.attempts.map(asRemixAttempt) : [],
    failure_explanation: asTextOrNull(value.failure_explanation),
    suggested_retry: asTextOrNull(value.suggested_retry),
    saved_media_item_id: asNullableNumber(value.saved_media_item_id),
    tweaks: (value.tweaks as unknown) as RemixTweaks | undefined,
    result: asRemixResult(value.result),
    image_url: asTextOrNull(value.image_url) || undefined,
    saved_item: value.saved_item ? asCatalogItem(value.saved_item) : undefined,
  };
}

export function asUploadSaveResponse(raw: unknown): RemixSaveResponse {
  if (!raw || typeof raw !== "object") {
    return { status: "failed" };
  }
  const value = raw as Record<string, unknown>;
  const status = asText(value.status);
  const nextStatus = status === "saved" || status === "saving" ? status : "failed";
  return {
    status: nextStatus,
    item: value.item ? asCatalogItem(value.item) : undefined,
    job: value.job ? asRemixJob(value.job) : undefined,
    embedding_error: asTextOrNull(value.embedding_error),
  };
}

export function asPublicShareState(raw: unknown): PublicShareState {
  if (!raw || typeof raw !== "object") {
    throw new Error("Invalid public share response");
  }
  const value = raw as Record<string, unknown>;
  const active = value.active === true;
  const url = asTextOrNull(value.url);
  return {
    enabled: value.enabled === true,
    variant: value.variant === "recipe" ? "recipe" : "output",
    active,
    url,
    media_url: asTextOrNull(value.media_url),
    card_url: asTextOrNull(value.card_url),
    created_at: asTextOrNull(value.created_at),
    revoked_at: asTextOrNull(value.revoked_at),
  };
}
