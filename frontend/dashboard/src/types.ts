export type CatalogIndex = {
  version: number;
  generated_at: string;
  item_count: number;
  chunk_size: number;
  chunks: string[];
  tags_path: string;
  search_index_path: string;
  search_results_base: string;
};

export type DashboardConfig = {
  mode?: "online" | "static";
  catalogIndex?: string;
  apiBase?: string;
  remixApiBase?: string;
};

export type ApiItemsResponse = {
  items: CatalogItem[];
  next_cursor: string | number | null;
  total?: number | null;
};

export type PublicShareState = {
  enabled?: boolean;
  variant?: "output" | "recipe";
  active: boolean;
  url: string | null;
  media_url: string | null;
  card_url?: string | null;
  created_at: string | null;
  revoked_at: string | null;
};

export type CatalogItem = {
  id: number;
  telegram_message_id: number | string;
  chat_id: string;
  telegram_post_url: string | null;
  date: string | null;
  saved_at?: string | null;
  added_at?: string | null;
  caption: string;
  source_url: string | null;
  source_display_url: string | null;
  source_site: string | null;
  source_title: string | null;
  source_description: string | null;
  source_embed_url: string | null;
  grouped_id?: string | null;
  forwarded_from?: string | null;
  origin: string;
  link_preview: Record<string, unknown>;
  media_type: string;
  mime_type: string | null;
  thumbnail_path: string | null;
  preview_path: string | null;
  hover_preview_path: string | null;
  playback_path: string | null;
  original_path: string | null;
  duration_seconds: number | string | null;
  width: number | string | null;
  height: number | string | null;
  size_bytes: number | string | null;
  duplicate_group_id: string | null;
  processing_status: string;
  public_status: string;
  classification_status: string;
  visibility: string;
  classification_block_reason: string | null;
  summary: string;
  visible_text: string;
  narration: string;
  language: string | null;
  meme_format: string | null;
  tone: string[];
  reaction_type: string[];
  topics: string[];
  keywords: string[];
  people_or_characters: string[];
  safety_flags: string[];
  search_phrases: string[];
  sha256?: string;
  perceptual_hash?: string;
  server_tags?: string[];
  favorite: boolean;
  archived: boolean;
  manual_tags: string[];
  notes: string;
  remix_sources: RemixSource[];
  remix_used_by: RemixSource[];
  remix_tweaks?: RemixTweaks | null;
};

export type RemixSource = {
  id: number;
  title: string;
  media_type: string;
  thumbnail_path: string | null;
  preview_path: string | null;
  original_path: string | null;
};

export type TagsCatalog = Record<string, Array<{ value: string; count: number }>>;

export type SearchResult = {
  media_item_id: number;
  score: number;
  semantic_score: number;
  lexical_score: number;
  explanation: string;
};

export type RemixHealth = {
  enabled: boolean;
  provider: string;
  model: string;
  aspect_ratio: string;
  image_size: string;
  message: string;
};

export type RemixTweakControl = {
  id: string;
  label: string;
  description: string;
  low_label: string;
  high_label: string;
  default_value: number;
};

export type RemixTweakAnalysis = {
  prompt_version: string;
  summary: string;
  controls: RemixTweakControl[];
  source?: string;
  notice?: string;
};

export type RemixTweaks = {
  analysis_prompt_version: string;
  summary: string;
  controls: RemixTweakControl[];
  values: Record<string, number>;
  schizo_level?: number;
  chronically_online_level?: number;
  no_text?: boolean;
  comic_strip?: boolean;
  anti_slop?: boolean;
};

export type RemixResultPayload = {
  summary: string;
  visible_text: string;
  narration: string;
  language: string | null;
  meme_format: string | null;
  tone: string[];
  reaction_type: string[];
  topics: string[];
  keywords: string[];
  people_or_characters: string[];
  safety_flags: string[];
  search_phrases: string[];
  confidence: number;
  image_path: string;
  safety_mode?: string;
};

export type RemixAttempt = {
  attempt: number;
  mitigation_id: string;
  label: string;
  description: string;
  status: string;
  message: string;
  error: string | null;
  block_reason: string | null;
  rejection_reason: string | null;
};

export type RemixJobStatus =
  | "queued"
  | "preparing_media"
  | "calling_gemini"
  | "ready"
  | "rate_limited"
  | "blocked"
  | "failed"
  | "saving"
  | "saved";

export type RemixJob = {
  id: string;
  left_id: number | string;
  right_id: number | string;
  safety_mode: string;
  status: RemixJobStatus;
  message: string;
  created_at: string | null;
  updated_at: string | null;
  retry_after_seconds: number | string | null;
  block_reason: string | null;
  rejection_reason: string | null;
  error: string | null;
  embedding_error: string | null;
  attempts: RemixAttempt[];
  failure_explanation: string | null;
  suggested_retry: string | null;
  saved_media_item_id?: number | string | null;
  tweaks?: RemixTweaks;
  result?: RemixResultPayload;
  image_url?: string;
  saved_item?: CatalogItem;
};
