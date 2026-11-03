import { version } from "../package.json";
import { useRemixSession, readRemixOpen, persistRemixOpen } from "./remixSession";
import { usePlaybackDiagnostics } from "./playbackDiagnostics";
import { asCatalogItem, asRemixJob, asUploadSaveResponse, asPublicShareState, apiErrorMessage, ApiRequestError } from "./api";
import { apiUrl, apiItemParams, useCatalog, normalizeDisplayText, isDashboardVisibleItem, type Filters } from "./catalog";
import { MediaPreview } from "./MediaPreview";
import { RemixPresentation } from "./RemixPresentation";
import { MetadataEditor } from "./MetadataEditor";
import { claimMediaPlayback, releaseMediaPlayback, formatDuration } from "./mediaPlayback";
import {
  Archive,
  ArrowLeft,
  Check,
  ChevronDown,
  Download,
  Eye,
  EyeOff,
  ExternalLink,
  Heart,
  Image as ImageIcon,
  LayoutGrid,
  Maximize2,
  Plus,
  Pencil,
  RefreshCw,
  Search,
  Share2,
  ShieldAlert,
  Shuffle,
  Sparkles,
  SlidersHorizontal,
  Trash2,
  Upload,
  Unlink,
  X,
} from "lucide-react";
import {
  useCallback,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ChangeEvent as ReactChangeEvent,
  type DragEvent as ReactDragEvent,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type Ref as ReactRef,
  type RefObject,
  type SyntheticEvent as ReactSyntheticEvent,
} from "react";
import type {
  CatalogItem,
  PublicShareState,
  RemixHealth,
  RemixJob,
  RemixSource,
  RemixTweakAnalysis,
  RemixTweaks,
} from "./types";
import arsenalMarkIcon from "./assets/dashboard-icons/arsenal-mark.png";
import remixSparkIcon from "./assets/dashboard-icons/remix-spark.png";

type View = "gallery";

const GALLERY_EAGER_MEDIA_COUNT = 12;
const LOAD_MORE_ROOT_MARGIN = "1800px 0px";
const APP_NAME = "Meme Arsenal";
const APP_SLUG = "meme-arsenal";
const REMIX_DRAG_MIME = `application/x-${APP_SLUG}-item-id`;
const REMIX_DRAG_START_EVENT = `${APP_SLUG}:remix-drag-start`;
const REMIX_DRAG_END_EVENT = `${APP_SLUG}:remix-drag-end`;
const RANDOM_SCOPE_STORAGE_KEY = `${APP_SLUG}.randomScope`;
const RESULT_ARRIVAL_POP_MS = 1100;
const REMIX_UPLOAD_EXTS = new Set(["png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "mp4", "webm", "mov", "m4v", "mkv", "avi", "gifv", "mpg", "mpeg"]);
const MIN_SEARCH_QUERY_LENGTH = 3;
const SEARCH_QUERY_DEBOUNCE_MS = 300;
const SAVE_NOTICE_HIDE_MS = 4200;
const SAVE_POLL_MS = 850;
const DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS = 60;
const DETAIL_PANEL_WIDTH_STORAGE_KEY = `${APP_SLUG}.detailPanelWidth`;
const DETAIL_PANEL_DEFAULT_WIDTH = 780;
const DETAIL_PANEL_MIN_WIDTH = 300;
const DETAIL_PANEL_MAX_WIDTH = 920;
const DETAIL_PANEL_VIEWPORT_RESERVE = 520;
const DETAIL_PANEL_KEYBOARD_STEP = 24;
const MOBILE_BREAKPOINT = 760;

type RandomScope = "all" | "filtered";
type RemixSide = "left" | "right";
type RemixPromptModeLevel = 0 | 1 | 2 | 3;

type RemixTaskStatus = "saving" | "saved" | "failed";

type RemixTask = {
  jobId: string;
  status: RemixTaskStatus;
  message: string;
  detail: string;
  updatedAt: string;
  item?: CatalogItem | null;
};

type DraftUploadSaveStatus = {
  itemId: number;
  status: "saving" | "saved" | "failed";
  message: string;
};

type PublicShareNotice = {
  itemId: number;
  message: string;
  error: boolean;
};

type MobileGalleryPosition = {
  scrollY: number;
  itemId: number | null;
  cardTop: number | null;
};

type RemixSeed = {
  item?: CatalogItem;
  key: number;
  side?: RemixSide;
  behavior: "replace" | "append" | "clear";
};

function randomScopeStorageKey(side: RemixSide) {
  return `${RANDOM_SCOPE_STORAGE_KEY}.${side}`;
}

function formatCountdownSeconds(seconds: number) {
  const total = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  if (minutes && remainder) return `${minutes}m ${String(remainder).padStart(2, "0")}s`;
  if (minutes) return `${minutes}m`;
  return `${total}s`;
}

function rateLimitCooldownDuration(job: RemixJob | null) {
  if (job?.status !== "rate_limited") return 0;
  const seconds = Number(job.retry_after_seconds ?? DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS;
}

function rateLimitCooldownRemaining(job: RemixJob | null, now: number) {
  const duration = rateLimitCooldownDuration(job);
  if (!duration) return 0;
  const updatedAt = Date.parse(job?.updated_at || "");
  const elapsed = Number.isFinite(updatedAt) ? Math.max(0, (now - updatedAt) / 1000) : 0;
  return Math.max(0, Math.ceil(duration - elapsed));
}

function storedRandomScopeValue(value: string | null): RandomScope | null {
  return value === "filtered" || value === "all" ? value : null;
}

function readStoredRandomScope(side: RemixSide): RandomScope {
  if (typeof window === "undefined") return "all";
  try {
    return (
      storedRandomScopeValue(window.localStorage.getItem(randomScopeStorageKey(side))) ||
      storedRandomScopeValue(window.localStorage.getItem(RANDOM_SCOPE_STORAGE_KEY)) ||
      "all"
    );
  } catch {
    return "all";
  }
}

function isDefaultRemixFilter(filters: Filters) {
  return (
    filters.mediaType === "all" &&
    filters.topic === "all" &&
    filters.tone === "all" &&
    filters.reactionType === "all" &&
    filters.memeFormat === "all" &&
    filters.language === "all" &&
    !filters.favorite &&
    !filters.archived
  );
}

function maxDetailPanelWidth() {
  if (typeof window === "undefined") return DETAIL_PANEL_MAX_WIDTH;
  return Math.max(
    DETAIL_PANEL_MIN_WIDTH,
    Math.min(DETAIL_PANEL_MAX_WIDTH, window.innerWidth - DETAIL_PANEL_VIEWPORT_RESERVE),
  );
}

function clampDetailPanelWidth(width: number) {
  return Math.round(Math.min(Math.max(width, DETAIL_PANEL_MIN_WIDTH), maxDetailPanelWidth()));
}

function readStoredDetailPanelWidth() {
  if (typeof window === "undefined") return DETAIL_PANEL_DEFAULT_WIDTH;
  try {
    const raw = window.localStorage.getItem(DETAIL_PANEL_WIDTH_STORAGE_KEY);
    if (!raw) return DETAIL_PANEL_DEFAULT_WIDTH;
    const stored = Number(raw);
    return Number.isFinite(stored) ? clampDetailPanelWidth(stored) : DETAIL_PANEL_DEFAULT_WIDTH;
  } catch {
    return DETAIL_PANEL_DEFAULT_WIDTH;
  }
}

function persistDetailPanelWidth(width: number) {
  try {
    window.localStorage.setItem(DETAIL_PANEL_WIDTH_STORAGE_KEY, String(width));
  } catch {
    // Non-essential preference.
  }
}

async function copyPublicShareUrl(url: string) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(url);
    return;
  }
  const field = document.createElement("textarea");
  field.value = url;
  field.readOnly = true;
  field.style.position = "fixed";
  field.style.opacity = "0";
  document.body.appendChild(field);
  field.select();
  const copied = document.execCommand("copy");
  field.remove();
  if (!copied) throw new Error("Could not copy link");
}

async function presentPublicShare(url: string, title: string): Promise<"shared" | "copied" | "ready"> {
  if (navigator.share) {
    try {
      await navigator.share({ title, url });
      return "shared";
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") return "ready";
    }
  }
  try {
    await copyPublicShareUrl(url);
    return "copied";
  } catch {
    return "ready";
  }
}

function randomScopeLabel(scope: RandomScope) {
  return scope === "filtered" ? "Filter/search" : "All items";
}

function pickUploadError(payload: unknown, fallback: string) {
  if (payload && typeof payload === "object") {
    const body = payload as { error?: unknown; detail?: unknown; message?: unknown };
    if (typeof body.error === "string" && body.error.trim()) return body.error.trim();
    if (typeof body.detail === "string" && body.detail.trim()) return body.detail.trim();
    if (typeof body.message === "string" && body.message.trim()) return body.message.trim();
  }
  return fallback;
}

async function responseError(response: Response, fallback: string) {
  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    // The status text is the useful fallback for non-JSON proxy errors.
  }
  return pickUploadError(payload, fallback);
}

function isRemixUploadFile(file: File) {
  const type = file.type.toLowerCase();
  const name = (file.name || "").toLowerCase();
  if (type.startsWith("image/") || type.startsWith("video/")) return true;
  const ext = name.includes(".") ? name.split(".").pop() : "";
  return REMIX_UPLOAD_EXTS.has(ext ?? "");
}

function setRemixDragData(event: ReactDragEvent<HTMLElement>, itemId: number) {
  event.dataTransfer.effectAllowed = "copy";
  event.dataTransfer.setData(REMIX_DRAG_MIME, String(itemId));
  event.dataTransfer.setData("text/plain", String(itemId));
  window.dispatchEvent(new CustomEvent(REMIX_DRAG_START_EVENT, { detail: { itemId } }));
}

function clearRemixDragData() {
  window.dispatchEvent(new Event(REMIX_DRAG_END_EVENT));
}

const emptyFilters: Filters = {
  mediaType: "all",
  topic: "all",
  tone: "all",
  reactionType: "all",
  memeFormat: "all",
  language: "all",
  favorite: false,
  archived: false,
};

const genericChipValues = new Set([
  "blocked",
  "caption",
  "classification",
  "content",
  "gemini blocked",
  "gif",
  "image",
  "media",
  "meme",
  "photo",
  "prohibited content",
  "saved",
  "screenshot",
  "sticker",
  "text",
  "unknown",
  "video",
  "visible text",
]);

const tokenStopWords = new Set([
  "and",
  "for",
  "from",
  "has",
  "into",
  "just",
  "meme",
  "not",
  "that",
  "the",
  "this",
  "with",
]);

function displayTokens(value: string | null | undefined) {
  return normalizeDisplayText(value)
    .split(" ")
    .filter((token) => token.length > 2 && !tokenStopWords.has(token));
}

function hasSameText(left: string | null | undefined, right: string | null | undefined) {
  const leftText = normalizeDisplayText(left);
  const rightText = normalizeDisplayText(right);
  return Boolean(leftText && rightText && leftText === rightText);
}

function normalizedUrl(value: string | null | undefined) {
  return String(value || "").trim().replace(/\/+$/, "").toLowerCase();
}

function shouldShowTelegramLink(item: CatalogItem) {
  const url = item.telegram_post_url || "";
  if (!url) return false;
  const normalizedTelegram = normalizedUrl(url);
  if (!/^https:\/\/t\.me\/[a-z0-9_]{5,32}\/\d+$/.test(normalizedTelegram)) return false;
  return normalizedTelegram !== normalizedUrl(item.source_url);
}

function isDraftUploadItem(item: CatalogItem | null) {
  if (!item) return false;
  return item.origin === "dashboard_upload" && item.public_status === "draft" && item.visibility === "private";
}

function tweakAnalysisNeedsAttention(analysis: RemixTweakAnalysis | null, status = "") {
  const text = `${analysis?.notice || ""} ${status}`.toLowerCase();
  return Boolean(text.match(/\b(blocked|filtered|failed|could not|cooling down|safe default|conservative)\b/));
}

function tweakAnalysisReadyStatus(analysis: RemixTweakAnalysis, controlCount: number) {
  if (!controlCount) return "No tweak sliders suggested";
  if (tweakAnalysisNeedsAttention(analysis)) {
    if ((analysis.notice || "").toLowerCase().includes("blocked")) return "Gemini blocked analysis";
    return "Fallback tweaks shown";
  }
  if (analysis.source === "local_fallback") return "Safe default tweaks";
  if (analysis.source === "conservative") return "Conservative tweaks ready";
  return "Tweaks ready";
}

function useDebouncedValue<T>(value: T, delayMs: number) {
  const [debouncedValue, setDebouncedValue] = useState(value);

  useEffect(() => {
    const timeoutId = window.setTimeout(() => setDebouncedValue(value), delayMs);
    return () => window.clearTimeout(timeoutId);
  }, [delayMs, value]);

  return debouncedValue;
}

type RemixSliderStyle = CSSProperties & {
  "--slider-a": string;
  "--slider-b": string;
  "--slider-c": string;
  "--slider-thumb": string;
  "--slider-fill": string;
};

type RemixGeneratingStyle = CSSProperties & {
  "--generate-a": string;
  "--generate-b": string;
  "--generate-c": string;
  "--generate-d": string;
};

function hashText(value: string) {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) % 360;
  }
  return hash;
}

function mixHue(start: number, end: number, amount: number) {
  const delta = ((end - start + 540) % 360) - 180;
  return Math.round((start + delta * amount + 360) % 360);
}

function sliderHueStops(id: string, index: number) {
  const base = (hashText(id) + index * 71) % 360;
  return [base, (base + 86) % 360, (base + 176) % 360];
}

function sampledSliderHue(stops: number[], value: number) {
  const clamped = Math.min(100, Math.max(0, value));
  if (clamped <= 50) return mixHue(stops[0], stops[1], clamped / 50);
  return mixHue(stops[1], stops[2], (clamped - 50) / 50);
}

function sliderThumbColor(id: string, index: number, value: number) {
  const clamped = Math.min(100, Math.max(0, value));
  const thumbHue = sampledSliderHue(sliderHueStops(id, index), clamped);
  return `hsl(${thumbHue} 88% 50%)`;
}

function remixSliderStyle(id: string, index: number, value: number): RemixSliderStyle {
  const clamped = Math.min(100, Math.max(0, value));
  const stops = sliderHueStops(id, index);
  const thumbColor = sliderThumbColor(id, index, clamped);
  return {
    "--slider-a": `hsl(${stops[0]} 88% 52%)`,
    "--slider-b": `hsl(${stops[1]} 90% 54%)`,
    "--slider-c": `hsl(${stops[2]} 84% 50%)`,
    "--slider-thumb": thumbColor,
    "--slider-fill": thumbColor.replace(")", " / 0.2)"),
  };
}

function remixGeneratingStyle(
  analysis: RemixTweakAnalysis | null,
  values: Record<string, number>,
): RemixGeneratingStyle {
  const fallbackIds = ["generate-fidelity", "generate-chaos", "generate-humor", "generate-intensity"];
  const fallbackValues = [34, 52, 68, 84];
  const controls = analysis?.controls.slice(0, 4) || [];
  const colors = fallbackIds.map((fallbackId, index) => {
    const control = controls[index];
    const id = control?.id || fallbackId;
    const value = control ? values[control.id] ?? control.default_value ?? fallbackValues[index] : fallbackValues[index];
    return sliderThumbColor(id, index, value);
  });
  return {
    "--generate-a": colors[0],
    "--generate-b": colors[1],
    "--generate-c": colors[2],
    "--generate-d": colors[3],
  };
}

function formatResolution(item: CatalogItem) {
  if (!item.width || !item.height) return "";
  return `${item.width}x${item.height}`;
}

function isBlockedClassification(item: CatalogItem) {
  return item.classification_status === "blocked";
}

function itemContextText(item: CatalogItem) {
  return [
    item.caption,
    item.source_title || "",
    item.source_description || "",
    item.summary,
    item.visible_text,
    item.narration,
    item.meme_format || "",
    ...(item.reaction_type || []),
  ].join(" ");
}

function chipScore(
  item: CatalogItem,
  value: string,
  manualTags: Set<string>,
  context: string,
  contextTokens: Set<string>,
) {
  const normalized = normalizeDisplayText(value);
  if (!normalized || genericChipValues.has(normalized) || normalized === normalizeDisplayText(item.media_type)) {
    return -1;
  }

  if (manualTags.has(normalized)) {
    return 100;
  }

  const tokens = displayTokens(normalized);
  if (!tokens.length) {
    return -1;
  }

  let score = context.includes(normalized) ? 20 : 0;
  tokens.forEach((token) => {
    if (contextTokens.has(token)) score += 10;
  });

  return score;
}

function displayChipsForItem(item: CatalogItem, limit = 5) {
  const manualTags = new Set((item.manual_tags || []).map(normalizeDisplayText));
  const context = normalizeDisplayText(itemContextText(item));
  const contextTokens = new Set(displayTokens(context));
  const candidates = [
    ...(item.manual_tags || []),
    ...(item.topics || []),
    ...(item.keywords || []),
    ...(item.reaction_type || []),
  ];
  const seen = new Set<string>();
  return candidates
    .map((value, index) => ({
      value,
      index,
      normalized: normalizeDisplayText(value),
      score: chipScore(item, value, manualTags, context, contextTokens),
    }))
    .filter((chip) => {
      if (!chip.normalized || seen.has(chip.normalized) || chip.score <= 0) return false;
      seen.add(chip.normalized);
      return true;
    })
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, limit)
    .map((chip) => chip.value);
}

function itemDisplayTitle(item: CatalogItem | null | undefined) {
  return item?.summary || item?.caption || item?.source_title || (item ? `Media ${item.id}` : "Generated remix");
}

function useMobileLayout() {
  const [mobileLayout, setMobileLayout] = useState(() => {
    if (typeof window === "undefined") return false;
    return window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`).matches;
  });

  useEffect(() => {
    const query = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT}px)`);
    const update = () => setMobileLayout(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return mobileLayout;
}

function Chips({ values, limit = 5 }: { values: string[]; limit?: number }) {
  return (
    <div className="chips">
      {values.slice(0, limit).map((value) => (
        <span className="chip" key={value}>
          {value}
        </span>
      ))}
    </div>
  );
}

function ItemCard({
  item,
  selected,
  priority,
  fetchPriority,
  onSelect,
  remixSide,
  onClearRemixSide,
  removing = false,
  onRemix,
  remixBusy,
}: {
  item: CatalogItem;
  selected: boolean;
  priority: boolean;
  fetchPriority: boolean;
  onSelect: () => void;
  remixSide?: RemixSide | null;
  onClearRemixSide?: (side: RemixSide, item: CatalogItem) => void;
  removing?: boolean;
  onRemix: (side?: RemixSide) => void;
  remixBusy: boolean;
}) {
  const blocked = isBlockedClassification(item);
  const title = item.summary || item.caption || item.source_title || "Saved meme";
  const chips = useMemo(() => displayChipsForItem(item, 4), [item]);
  function startDrag(event: ReactDragEvent<HTMLElement>) {
    setRemixDragData(event, item.id);
  }

  return (
    <article
      className={`itemCard ${selected ? "selected" : ""} ${blocked ? "blocked" : ""} ${removing ? "is-removing" : ""} ${item.archived ? "is-hidden" : ""}`}
      data-item-id={item.id}
      draggable
      onDragStart={startDrag}
      onDragEnd={clearRemixDragData}
      onClick={onSelect}
    >
      <div
        className="itemCardMain"
      >
        <MediaPreview item={item} priority={priority} fetchPriority={fetchPriority} />
        {remixSide ? (
          <button
            className={`remixCorner remixCorner${remixSide === "left" ? "A" : "B"}`}
            type="button"
            aria-label={`Remove from remix ${remixSide === "left" ? "A" : "B"}`}
            onClick={(event) => {
              event.stopPropagation();
              onClearRemixSide?.(remixSide, item);
            }}
          >
            <span>{remixSide === "left" ? "A" : "B"}</span>
          </button>
        ) : null}
        <button
          className="itemBody itemDetailButton"
          type="button"
          onClick={(event) => {
            event.stopPropagation();
            onSelect();
          }}
        >
          <span className="itemTopline">
            <span>{item.media_type}</span>
            <span className="itemStates">
              {blocked ? (
                <span className="blockPill" title={item.classification_block_reason || "Blocked by classifier"}>
                  <ShieldAlert size={13} />
                  Blocked
                </span>
              ) : null}
              {item.favorite ? <Heart size={15} fill="currentColor" /> : null}
              {item.archived ? <span className="hiddenIndicator"><EyeOff size={13} />Hidden</span> : null}
            </span>
          </span>
          <strong>{title}</strong>
          <Chips values={chips} limit={4} />
        </button>
        <div className="cardRemixActions" onClick={(event) => event.stopPropagation()}>
            <button type="button" title="Use this image as remix source A" disabled={remixBusy || remixSide === "left"} onClick={() => onRemix("left")}><RefreshCw size={14} />{remixSide === "left" ? "Selected A" : "Use as A"}</button>
            <button type="button" title="Use this image as remix source B" disabled={remixBusy || remixSide === "right"} onClick={() => onRemix("right")}><RefreshCw size={14} />{remixSide === "right" ? "Selected B" : "Use as B"}</button>
        </div>
      </div>
    </article>
  );
}

function sourceTraceImage(source: RemixSource) {
  return source.thumbnail_path || source.preview_path || source.original_path || "";
}

function SavedRemixCard({ item, apiBase, onShareChange }: { item: CatalogItem; apiBase?: string; onShareChange: (id: number, share: PublicShareState) => void }) {
  const [open, setOpen] = useState(false);
  if (item.origin !== "remix" || !apiBase) return null;
  return <>
    <button className="secondaryAction" type="button" onClick={() => setOpen(true)}><ImageIcon size={17} />Remix Page</button>
    {open ? <RemixPresentation item={item} apiBase={apiBase} card onShareChange={onShareChange} onClose={() => setOpen(false)} /> : null}
  </>;
}

function DetailView({
  item,
  apiBase,
  onShareChange,
  remixBusy,
  showRemixAction,
  showPublicShareAction,
  publicShare,
  publicShareLoading,
  publicShareBusy,
  publicShareNotice,
  onStartRemix,
  onCreatePublicShare,
  onPresentPublicShare,
  onRecallPublicShare,
  onRetryClassification,
  onHide,
  onDeleteRemix,
  onSelectSource,
  onSaveUpload,
  onUpdateItem,
  uploadSaveStatus,
}: {
  item: CatalogItem | null;
  apiBase?: string;
  onShareChange: (id: number, share: PublicShareState) => void;
  remixBusy: boolean;
  showRemixAction: boolean;
  showPublicShareAction: boolean;
  publicShare: PublicShareState | null;
  publicShareLoading: boolean;
  publicShareBusy: boolean;
  publicShareNotice?: PublicShareNotice | null;
  onStartRemix: (item: CatalogItem, side?: RemixSide) => void;
  onCreatePublicShare: (item: CatalogItem) => void;
  onPresentPublicShare: (item: CatalogItem, share: PublicShareState) => void;
  onRecallPublicShare: (item: CatalogItem) => void;
  onRetryClassification: (item: CatalogItem) => void;
  onHide: (item: CatalogItem, archived: boolean) => void;
  onDeleteRemix: (item: CatalogItem) => void;
  onSelectSource: (id: number) => void;
  onSaveUpload: (item: CatalogItem) => void;
  onUpdateItem: (item: CatalogItem) => void;
  uploadSaveStatus?: DraftUploadSaveStatus | null;
}) {
  const [usedByOpen, setUsedByOpen] = useState(false);
  const [editing, setEditing] = useState(false);
  const [detailPlaybackError, setDetailPlaybackError] = useState<string | null>(null);
  const detailVideoRef = useRef<HTMLVideoElement>(null);
  const detailItem = item;
  usePlaybackDiagnostics(detailVideoRef, detailItem?.id, "detail");
  const remixUsedBy = detailItem?.remix_used_by || [];

  useEffect(() => {
    setUsedByOpen(false);
    setEditing(false);
    setDetailPlaybackError(null);
    const playbackId = detailItem ? `detail-${detailItem.id}` : null;
    return () => {
      if (playbackId) releaseMediaPlayback(playbackId);
    };
  }, [detailItem?.id]);

  if (!detailItem) {
    return (
      <section className="detail empty">
        <ImageIcon size={26} />
        <strong>{APP_NAME}</strong>
      </section>
    );
  }

  const detailVideoSource =
    detailItem.media_type === "video"
      ? detailItem.playback_path || detailItem.original_path || detailItem.hover_preview_path || ""
      : "";
  const mediaSource = detailItem.preview_path || detailItem.original_path || "";
  const blocked = isBlockedClassification(detailItem);
  const showNarration = Boolean(detailItem.narration && !hasSameText(detailItem.narration, detailItem.visible_text));
  const duration = formatDuration(detailItem.duration_seconds);
  const resolution = formatResolution(detailItem);
  const isUploadDraft = isDraftUploadItem(detailItem);
  const isSavingUpload = uploadSaveStatus?.status === "saving";
  const isSavedUpload = uploadSaveStatus?.status === "saved";
  const uploadErrorMessage = uploadSaveStatus?.status === "failed" ? uploadSaveStatus.message : null;
  const detailItemId = detailItem.id;
  function startDetailDrag(event: ReactDragEvent<HTMLElement>) {
    setRemixDragData(event, detailItemId);
  }

  function finishDetailVideo(event: ReactSyntheticEvent<HTMLVideoElement>) {
    const video = event.currentTarget;
    const tail = video.duration - video.currentTime;
    if (Number.isFinite(video.duration) && video.duration > 0 && tail > 0 && tail <= 0.35) {
      try {
        video.currentTime = video.duration;
      } catch {
        // Some browsers reject end-of-media seeks on unusual containers.
      }
    }
  }

  return (
    <section className="detail">
      <div
        className="detailMedia"
        draggable
        onDragStart={startDetailDrag}
        onDragEnd={clearRemixDragData}
        title="Drag to remix"
        aria-label="Drag media to remix"
      >
        {detailVideoSource ? (
          <video
            ref={detailVideoRef}
            src={detailVideoSource}
            controls
            playsInline
            preload="metadata"
            disablePictureInPicture
            disableRemotePlayback
            controlsList="nodownload noremoteplayback"
            poster={detailItem.preview_path || undefined}
            draggable={false}
            onPlay={(event) => {
              setDetailPlaybackError(null);
              claimMediaPlayback(event.currentTarget, `detail-${detailItem.id}`);
            }}
            onError={() => setDetailPlaybackError("This video could not play. Try again in a moment.")}
            onEnded={finishDetailVideo}
          />
        ) : mediaSource ? (
          <img src={mediaSource} alt="" draggable={false} />
        ) : (
          <div className="detailMediaMissing">
            <ImageIcon size={28} />
          </div>
        )}
        {detailPlaybackError ? (
          <span className="detailPlaybackError" role="status">
            {detailPlaybackError}
          </span>
        ) : null}
      </div>
      <div className="detailBody">
        <div className="detailTitle">
          <h2>{detailItem.summary || detailItem.caption || detailItem.source_title || "Saved meme"}</h2>
          <span className="mediaPill">{detailItem.media_type}</span>
        </div>
        <SavedRemixCard key={detailItem.id} item={detailItem} apiBase={apiBase} onShareChange={onShareChange} />
        {editing && apiBase ? <MetadataEditor key={detailItem.id} item={detailItem} apiBase={apiBase} onSave={onUpdateItem} onClose={() => setEditing(false)} /> : null}
        {blocked ? (
          <span className="blockNotice">
            <ShieldAlert size={16} />
            Gemini blocked classification{detailItem.classification_block_reason ? `: ${detailItem.classification_block_reason}` : ""}
            <button className="classificationRetry" type="button" title="Retry classification" aria-label="Retry classification" onClick={() => onRetryClassification(detailItem)}>
              <RefreshCw size={16} />
            </button>
            {apiBase ? <button className="classificationRetry" type="button" title="Edit details manually" aria-label="Edit details manually" onClick={() => setEditing(true)}><Pencil size={16} /></button> : null}
            {publicShareNotice?.itemId === detailItem.id ? <small className={publicShareNotice.error ? "is-error" : ""}>{publicShareNotice.message}</small> : null}
          </span>
        ) : null}
        <p>{detailItem.caption}</p>
        {detailItem.source_url ? (
          <div className="sourceBox">
            <span>{detailItem.source_site || detailItem.source_display_url || "Link preview"}</span>
            <strong>{detailItem.source_title || detailItem.source_url}</strong>
            {detailItem.source_description ? <p>{detailItem.source_description}</p> : null}
          </div>
        ) : null}
        {showNarration ? (
          <section className="textPanel">
            <span>Narration</span>
            <p>{detailItem.narration}</p>
          </section>
        ) : null}
        {detailItem.visible_text ? (
          <section className="textPanel">
            <span>Visible text</span>
            <p>{detailItem.visible_text}</p>
          </section>
        ) : null}
        <Chips values={displayChipsForItem(detailItem, 12)} limit={12} />
        <dl className="facts">
          <div>
            <dt>Format</dt>
            <dd>{detailItem.meme_format || "unknown"}</dd>
          </div>
          <div>
            <dt>Reaction</dt>
            <dd>{detailItem.reaction_type.join(", ") || "unknown"}</dd>
          </div>
          {duration ? (
            <div>
              <dt>Duration</dt>
              <dd>{duration}</dd>
            </div>
          ) : null}
          {resolution ? (
            <div>
              <dt>Resolution</dt>
              <dd>{resolution}</dd>
            </div>
          ) : null}
        </dl>
        <div className="detailActions">
          {detailItem.original_path ? (
            <a className="primaryButton" href={detailItem.original_path} download>
              <Download size={17} />
              Download
            </a>
          ) : null}
          {detailItem.origin === "remix" ? (
            <button className="secondaryAction dangerAction" type="button" onClick={() => onDeleteRemix(detailItem)}>
              <Trash2 size={16} /> Delete remix
            </button>
          ) : (
            <button className="secondaryAction" type="button" onClick={() => onHide(detailItem, !detailItem.archived)}>
              {detailItem.archived ? <Eye size={16} /> : <EyeOff size={16} />}
              {detailItem.archived ? "Unhide" : "Hide"}
            </button>
          )}
          {apiBase ? <button className="secondaryAction" type="button" onClick={() => setEditing(true)}><Pencil size={16} />Edit details</button> : null}
          {showRemixAction ? <>
            <button className="secondaryAction" type="button" title="Use this image as remix source A" disabled={remixBusy} onClick={() => onStartRemix(detailItem, "left")}><RefreshCw size={16} />Use as A</button>
            <button className="secondaryAction" type="button" title="Use this image as remix source B" disabled={remixBusy} onClick={() => onStartRemix(detailItem, "right")}><RefreshCw size={16} />Use as B</button>
          </> : null}
          {showPublicShareAction && (!publicShare?.active || !publicShare.url) ? (
            <button
              className="secondaryAction publicShareAction"
              type="button"
              onClick={() => onCreatePublicShare(detailItem)}
              disabled={publicShareLoading || publicShareBusy || publicShare?.enabled !== true}
            >
              {publicShareLoading || publicShareBusy ? <RefreshCw size={17} className="spinIcon" /> : <Share2 size={17} />}
              {publicShareLoading ? "Checking share" : publicShare?.enabled !== true ? "Public sharing is disabled" : publicShareBusy ? "Creating link" : "Share publicly"}
            </button>
          ) : null}
          {showPublicShareAction && publicShare?.active ? (
            <>
              <button
                className="secondaryAction publicShareAction is-active"
                type="button"
                onClick={() => onPresentPublicShare(detailItem, publicShare)}
                disabled={publicShareBusy || publicShare?.enabled !== true || !publicShare.url}
              >
                <Share2 size={17} />
                Share
              </button>
              <button
                className="secondaryAction publicShareRecall"
                type="button"
                onClick={() => onRecallPublicShare(detailItem)}
                disabled={publicShareBusy}
              >
                {publicShareBusy ? <RefreshCw size={17} className="spinIcon" /> : <Unlink size={17} />}
                {publicShareBusy ? "Recalling" : "Recall share"}
              </button>
            </>
          ) : null}
          {showPublicShareAction && publicShareNotice?.itemId === detailItem.id ? (
            <span
              className={`publicShareNotice ${publicShareNotice.error ? "is-error" : ""}`}
              role={publicShareNotice.error ? "alert" : "status"}
            >
              {publicShareNotice.message}
            </span>
          ) : null}
          {isUploadDraft ? (
            <button
              className={`secondaryAction detailSaveAction ${isSavedUpload ? "is-saved" : ""}`}
              type="button"
              onClick={() => onSaveUpload(detailItem)}
              disabled={isSavingUpload}
            >
              {isSavingUpload ? <RefreshCw size={17} className="spinIcon" /> : <Plus size={17} />}
              {isSavingUpload ? "Saving to arsenal" : isSavedUpload ? "Saved to arsenal" : "Save to arsenal"}
            </button>
          ) : null}
          {uploadErrorMessage ? <span className="uploadSaveNotice">{uploadErrorMessage}</span> : null}
          {detailItem.source_url ? (
            <a className="secondaryAction" href={detailItem.source_url}>
              <ExternalLink size={17} />
              Source
            </a>
          ) : null}
          {shouldShowTelegramLink(detailItem) ? (
            <a className="secondaryAction" href={detailItem.telegram_post_url || undefined}>
              <ExternalLink size={17} />
              Telegram
            </a>
          ) : null}
          {remixUsedBy.length ? (
            <button className="secondaryAction" type="button" onClick={() => setUsedByOpen((current) => !current)}>
              <img className="assetIcon" src={remixSparkIcon} alt="" aria-hidden="true" />
              Used by
              <span>{remixUsedBy.length}</span>
            </button>
          ) : null}
          {detailItem.favorite ? (
            <span className="statePill">
              <Heart size={15} fill="currentColor" />
              Favorite
            </span>
          ) : null}
          {detailItem.archived ? (
            <span className="statePill">
              <Archive size={15} />
              Archived
            </span>
          ) : null}
          {blocked ? (
            <span className="statePill blockedState">
              <ShieldAlert size={15} />
              {detailItem.classification_block_reason || "Blocked"}
            </span>
          ) : null}
        </div>
        {usedByOpen && remixUsedBy.length ? (
          <section className="sourceTraceList" aria-label="Remixes using this item">
            {remixUsedBy.map((usedBy) => {
              const image = sourceTraceImage(usedBy);
              return (
                <button
                  className="sourceTraceItem"
                  type="button"
                  key={usedBy.id}
                  draggable
                  onDragStart={(event) => setRemixDragData(event, usedBy.id)}
                  onDragEnd={clearRemixDragData}
                  onClick={() => onSelectSource(usedBy.id)}
                  title={usedBy.title}
                >
                  {image ? <img src={image} alt="" loading="lazy" decoding="async" /> : <span>{usedBy.media_type}</span>}
                  <strong>{usedBy.title || `Media ${usedBy.id}`}</strong>
                </button>
              );
            })}
          </section>
        ) : null}
      </div>
    </section>
  );
}



function RemixSourceSlot({
  label,
  item,
  onRandom,
  onUpload,
  canChooseRandom,
  uploadBusy,
  randomScope,
  randomScopeLabel,
  randomScopeOpen,
  randomAllCount,
  randomFilteredCount,
  randomScopeRef,
  onToggleRandomScope,
  onChooseRandomScope,
  onFocus,
  onBrowse,
  onDropItem,
  isDragOver,
  showDropHint,
  dropDisabled,
  onDragOver,
  onDragLeave,
}: {
  label: string;
  item: CatalogItem | null;
  onRandom: () => void;
  onUpload: (file: File) => void | Promise<void>;
  canChooseRandom: boolean;
  uploadBusy: boolean;
  randomScope: RandomScope;
  randomScopeLabel: string;
  randomScopeOpen: boolean;
  randomAllCount: number;
  randomFilteredCount: number;
  randomScopeRef?: RefObject<HTMLSpanElement | null>;
  onToggleRandomScope: () => void;
  onChooseRandomScope: (scope: RandomScope) => void;
  onFocus: (item: CatalogItem) => void;
  onBrowse: () => void;
  onDropItem: (event: ReactDragEvent<HTMLElement>) => void;
  isDragOver: boolean;
  showDropHint: boolean;
  dropDisabled: boolean;
  onDragOver: (event: ReactDragEvent<HTMLElement>) => void;
  onDragLeave: (event: ReactDragEvent<HTMLElement>) => void;
}) {
  const sideDisabled = dropDisabled || uploadBusy;
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const slotClass = `remixSlot ${isDragOver ? "dragOver" : ""} ${showDropHint ? "dropHint" : ""} ${sideDisabled ? "dropLocked" : ""} ${uploadBusy ? "isBusy" : ""}`;

  function triggerFileInput() {
    if (sideDisabled) return;
    fileInputRef.current?.click();
  }

  function pickUploadedFile(event: ReactChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    void onUpload(file);
  }

  return (
    <section
      className={slotClass}
      aria-label={label}
      aria-disabled={sideDisabled}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDropItem}
    >
      <div className="remixSlotHeader">
        <strong>{label}</strong>
        <span className="remixSlotActions">
          <button className="secondaryAction compactAction" type="button" title={`Upload ${label}`} aria-label={`Upload ${label}`} onClick={triggerFileInput} disabled={sideDisabled}>
            {uploadBusy ? <RefreshCw size={15} className="spinIcon" /> : <Upload size={15} />}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            className="remixUploadInput"
            accept="image/*,video/*"
            onChange={pickUploadedFile}
          />
          <span
            className={`randomScopeControl remixRandomScopeControl ${randomScope === "filtered" ? "is-filtered" : ""} ${randomScopeOpen ? "is-open" : ""}`}
            ref={randomScopeOpen ? randomScopeRef : undefined}
          >
            <button
              className="remixRandomAction randomActionButton"
              type="button"
              onClick={onRandom}
              disabled={sideDisabled || !canChooseRandom}
              title={`Random from ${randomScopeLabel}`}
              aria-label={`Random ${label} from ${randomScopeLabel}`}
            >
              <Shuffle size={16} />
            </button>
            <button
              className="randomScopeToggle"
              type="button"
              onClick={onToggleRandomScope}
              disabled={sideDisabled}
              aria-label={`Random remix scope: ${randomScopeLabel}`}
              aria-haspopup="menu"
              aria-expanded={randomScopeOpen}
            >
              <ChevronDown size={14} />
            </button>
            {randomScopeOpen ? (
              <div className="randomScopeMenu remixRandomScopeMenu" role="menu" aria-label="Random remix source scope">
                <button
                  className={randomScope === "all" ? "active" : ""}
                  type="button"
                  role="menuitemradio"
                  aria-checked={randomScope === "all"}
                  onClick={() => onChooseRandomScope("all")}
                >
                  <span>All items</span>
                  <small>{`${randomAllCount} usable`}</small>
                  {randomScope === "all" ? <Check size={14} /> : null}
                </button>
                <button
                  className={randomScope === "filtered" ? "active" : ""}
                  type="button"
                  role="menuitemradio"
                  aria-checked={randomScope === "filtered"}
                  onClick={() => onChooseRandomScope("filtered")}
                >
                  <span>Filter/search</span>
                  <small>{`${randomFilteredCount} visible`}</small>
                  {randomScope === "filtered" ? <Check size={14} /> : null}
                </button>
              </div>
            ) : null}
          </span>
        </span>
      </div>
      {item ? (
        <button
          className="remixSource"
          type="button"
          onClick={() => onFocus(item)}
          aria-label={`Open ${label.toLowerCase()} remix source details`}
        >
          <MediaPreview item={item} compact />
          <span className="remixSourceText">
            <span>{item.media_type}</span>
            <strong>{item.summary || item.caption || item.source_title || "Saved meme"}</strong>
            <Chips values={displayChipsForItem(item, 4)} limit={4} />
          </span>
        </button>
      ) : (
        <button type="button" className="remixEmpty" onClick={onBrowse} disabled={sideDisabled}>
          <span>{uploadBusy ? "Preparing upload" : "Select an item to remix"}</span>
          {uploadBusy ? (
            <span className="remixSlotBusyText" aria-live="polite">
              <RefreshCw size={13} className="spinIcon" aria-hidden="true" />
              Waiting for media processing
            </span>
          ) : null}
        </button>
      )}
    </section>
  );
}

function RemixAttemptHistory({ job }: { job: RemixJob | null }) {
  const attempts = job?.attempts || [];
  const failed = Boolean(job && ["blocked", "failed", "rate_limited"].includes(job.status));
  const failureExplanation = failed
    ? job?.failure_explanation || job?.rejection_reason || job?.error || job?.message || "Remix could not finish."
    : "";
  const suggestedRetry = job?.suggested_retry !== failureExplanation && job?.suggested_retry !== "Check the Remix server, then retry this pair."
    ? job?.suggested_retry || "" : "";

  if (!job || ["queued", "preparing_media", "calling_gemini"].includes(job.status)) return null;
  return (
    <section className="remixAttemptPanel" aria-label="Remix attempts">
      {!failed ? <p className="remixProgressSummary" role="status">{["ready", "saving", "saved"].includes(job.status) && !job.error ? "Generation complete" : job.message || job.status}</p> : null}
      {failureExplanation ? (
        <div className="remixFailurePanel" role="alert">
          <p>{failureExplanation}</p>
          {suggestedRetry ? <p>{suggestedRetry}</p> : null}
        </div>
      ) : null}
      {attempts.length ? (
        <details className="remixAttemptList">
          <summary>Generation history ({attempts.length})</summary>
          <div className="remixAttemptTrack">
            {attempts.map((attempt) => (
              <div className={`remixAttempt ${attempt.status}`} key={`${attempt.attempt}-${attempt.mitigation_id}`}>
                <span>{attempt.attempt}</span>
                <div>
                  <strong>{attempt.label}</strong>
                  <p>{attempt.message || attempt.description}</p>
                  {attempt.rejection_reason || attempt.block_reason ? (
                    <small title={attempt.block_reason || attempt.rejection_reason || undefined}>
                      {attempt.rejection_reason || attempt.block_reason}
                    </small>
                  ) : null}
                </div>
              </div>
            ))}
          </div>
        </details>
      ) : null}
    </section>
  );
}

const REMIX_PROMPT_MODE_LABELS = ["off", "low", "medium", "high"] as const satisfies ReadonlyArray<string>;
const REMIX_PROMPT_MODE_SCALE_LABELS = ["off", "high"] as const satisfies ReadonlyArray<string>;

function RemixTweakPanel({
  panelRef,
  analysis,
  values,
  status,
  analyzing,
  collapsed,
  setCollapsed,
  schizoLevel,
  chronicallyOnlineLevel,
  noText,
  comicStrip,
  antiSlop,
  onChange,
  onSchizoLevelChange,
  onChronicallyOnlineLevelChange,
  onNoTextChange,
  onComicStripChange,
  onAntiSlopChange,
}: {
  panelRef?: ReactRef<HTMLElement>;
  analysis: RemixTweakAnalysis | null;
  values: Record<string, number>;
  status: string;
  analyzing: boolean;
  collapsed: boolean;
  setCollapsed: (value: boolean) => void;
  schizoLevel: RemixPromptModeLevel;
  chronicallyOnlineLevel: RemixPromptModeLevel;
  noText: boolean;
  comicStrip: boolean;
  antiSlop: boolean;
  onChange: (id: string, value: number) => void;
  onSchizoLevelChange: (level: RemixPromptModeLevel) => void;
  onChronicallyOnlineLevelChange: (level: RemixPromptModeLevel) => void;
  onNoTextChange: (enabled: boolean) => void;
  onComicStripChange: (enabled: boolean) => void;
  onAntiSlopChange: (enabled: boolean) => void;
}) {
  const controls = analysis?.controls.slice(0, 4) || [];
  const [activeSliderId, setActiveSliderId] = useState<string | null>(null);
  const upperControls = controls.slice(0, 2);
  const lowerControls = controls.slice(2, 4);
  const statusClass = tweakAnalysisNeedsAttention(analysis, status)
    ? "remixTweakStatus is-warning"
    : "remixTweakStatus";

  function startSliderInteraction(id: string) {
    setActiveSliderId(id);
  }

  function settleSliderInteraction(id: string) {
    setActiveSliderId((current) => (current === id ? null : current));
  }

  function renderSlider(control: (typeof controls)[number], visualIndex: number) {
    const value = values[control.id] ?? control.default_value ?? 50;
    const interactionClass = activeSliderId === control.id ? "is-changing" : "";
    return (
      <label
        className={`remixSlider ${interactionClass}`}
        key={control.id}
        style={remixSliderStyle(control.id, visualIndex, value)}
      >
        <span className="remixSliderTop">
          <strong>{control.label}</strong>
          <output className="remixSliderValue">{value}</output>
        </span>
        {control.description ? <span className="remixSliderDescription">{control.description}</span> : null}
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={value}
          disabled={analyzing}
          onPointerDown={() => startSliderInteraction(control.id)}
          onPointerUp={() => settleSliderInteraction(control.id)}
          onPointerCancel={() => settleSliderInteraction(control.id)}
          onFocus={() => startSliderInteraction(control.id)}
          onBlur={() => settleSliderInteraction(control.id)}
          onChange={(event) => {
            onChange(control.id, Number(event.target.value));
          }}
        />
        <span className="remixSliderScale">
          <span title={control.low_label}>{control.low_label}</span>
          <span title={control.high_label}>{control.high_label}</span>
        </span>
      </label>
    );
  }

  function renderPromptModeSlider(
    label: string,
    level: RemixPromptModeLevel,
    onChange: (value: RemixPromptModeLevel) => void,
    variant: "schizo" | "chronically",
  ) {
    const sliderId = variant === "schizo" ? "prompt-mode-schizophrenic" : "prompt-mode-chronically-online";
    const visualIndex = variant === "schizo" ? 0 : 1;
    const styleValue = Math.round((level / 3) * 100);
    const interactionClass = activeSliderId === sliderId ? "is-changing" : "";
    const modeClass = variant === "schizo" ? "schizoButton" : "chronicallyOnlineButton";
    return (
      <label
        className={`remixSlider remixPromptModeSlider ${interactionClass} level-${level} ${variant} ${modeClass}`}
        key={`${variant}-slider`}
        style={remixSliderStyle(sliderId, visualIndex, styleValue)}
      >
        <span className="remixSliderTop">
          <strong>{label}</strong>
          <output className="remixSliderValue remixPromptModeValue">{level === 0 ? "off" : REMIX_PROMPT_MODE_LABELS[level]}</output>
        </span>
        <input
          type="range"
          min="0"
          max="3"
          step="1"
          value={level}
          disabled={analyzing}
          onPointerDown={() => startSliderInteraction(sliderId)}
          onPointerUp={() => settleSliderInteraction(sliderId)}
          onPointerCancel={() => settleSliderInteraction(sliderId)}
          onFocus={() => startSliderInteraction(sliderId)}
          onBlur={() => settleSliderInteraction(sliderId)}
          onChange={(event) => {
            const nextLevel = Number(event.target.value) as RemixPromptModeLevel;
            onChange(nextLevel);
          }}
        />
        <span className="remixSliderScale">
          <span title={REMIX_PROMPT_MODE_SCALE_LABELS[0]}>{REMIX_PROMPT_MODE_SCALE_LABELS[0]}</span>
          <span title={REMIX_PROMPT_MODE_SCALE_LABELS[1]}>{REMIX_PROMPT_MODE_SCALE_LABELS[1]}</span>
        </span>
      </label>
    );
  }

  return (
    <section ref={panelRef} className={`remixTweakPanel ${collapsed ? "is-collapsed" : ""}`} aria-label="Remix tweaks">
      <div className="remixTweakHeader">
        <button
          className="remixTweakToggle"
          type="button"
          onClick={() => setCollapsed(!collapsed)}
          aria-expanded={!collapsed}
        >
          <span>
            <strong>Tweaks</strong>
            {status ? <span className={statusClass}>{status}</span> : null}
          </span>
          <ChevronDown size={16} aria-hidden="true" />
        </button>
      </div>
      <div className="remixTweakContent">
        {analysis?.summary ? <p className="remixTweakSummary">{analysis.summary}</p> : null}
        {analysis?.notice ? <p className="remixTweakNotice">{analysis.notice}</p> : null}
        <div className="remixPromptModeColumns">
          <div>{renderPromptModeSlider("Schizophrenic", schizoLevel, onSchizoLevelChange, "schizo")}</div>
          <div className="remixInstructionChecks" aria-label="Hard remix instructions">
            <label className="remixInstructionCheck">
              <input
                type="checkbox"
                checked={noText}
                disabled={analyzing}
                onChange={(event) => onNoTextChange(event.target.checked)}
              />
              <span>Text Free</span>
            </label>
            <label className="remixInstructionCheck">
              <input
                type="checkbox"
                checked={antiSlop}
                disabled={analyzing}
                onChange={(event) => onAntiSlopChange(event.target.checked)}
              />
              <span>Anti-Slop</span>
            </label>
            <label className="remixInstructionCheck">
              <input
                type="checkbox"
                checked={comicStrip}
                disabled={analyzing}
                onChange={(event) => onComicStripChange(event.target.checked)}
              />
              <span>Comic Strip</span>
            </label>
          </div>
          <div>
            {renderPromptModeSlider(
              "Chronically online",
              chronicallyOnlineLevel,
              onChronicallyOnlineLevelChange,
              "chronically",
            )}
          </div>
        </div>
        {controls.length ? (
          <>
            <div className="remixControls">
              {upperControls.map((control, index) => renderSlider(control, index))}
            </div>
            {Boolean(lowerControls.length) ? (
              <div className="remixControls">
                {lowerControls.map((control, index) => renderSlider(control, index + upperControls.length))}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </section>
  );
}

function GenerationOverlay({ style, job }: { style: CSSProperties; job: RemixJob | null }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    ref.current?.showModal();
    return () => { document.body.style.overflow = overflow; };
  }, []);
  return <dialog ref={ref} className="generationOverlay" aria-label="Generating remix" onCancel={(event) => event.preventDefault()}>
    <div className="remixGeneratingPanel" style={style}><strong className="remixGeneratingWord">Generating</strong></div>
    <div className="generationStage" role="status" aria-live="polite">
      <span>{job?.message || "Queued"}</span>
      {job?.attempts.length ? <small>Attempt {job.attempts[job.attempts.length - 1].attempt}: {job.attempts[job.attempts.length - 1].label}</small> : null}
    </div>
  </dialog>;
}

function RemixPane({
  open,
  seed,
  apiBase,
  catalogApiBase,
  onShareChange,
  visibleItems,
  allItems,
  mobileLayout = false,
  onSaveTaskChange,
  onDeleteSaved,
  deletedItemId,
  retrySaveRequest,
  onFocusItem,
  onBrowse,
  onUploadComplete,
  onBusyChange,
  onSourcesChange,
  contactSheetIsFiltered,
}: {
  open: boolean;
  seed: RemixSeed | null;
  apiBase: string | undefined;
  catalogApiBase?: string;
  onShareChange: (id: number, share: PublicShareState) => void;
  visibleItems: CatalogItem[];
  allItems: CatalogItem[];
  mobileLayout?: boolean;
  onSaveTaskChange: (task: RemixTask) => void;
  onDeleteSaved: (item: CatalogItem) => Promise<boolean>;
  deletedItemId: number | null;
  retrySaveRequest: { jobId: string; key: number } | null;
  onFocusItem: (item: CatalogItem) => void;
  onBrowse: () => void;
  onUploadComplete: (item: CatalogItem) => void;
  onBusyChange?: (busy: boolean) => void;
  onSourcesChange?: (left: CatalogItem | null, right: CatalogItem | null) => void;
  contactSheetIsFiltered: boolean;
}) {
  const base = apiBase || "../../api/remix";
  const {
    left, setLeft, right, setRight, job, setJob, clearJob, recovering, recoveryError, selectJob,
    tweakAnalysis, setTweakAnalysis, tweakValues, setTweakValues, schizoLevel, setSchizoLevel,
    chronicallyOnlineLevel, setChronicallyOnlineLevel, noText, setNoText, comicStrip, setComicStrip,
    antiSlop, setAntiSlop, saferMode, setSaferMode,
  } = useRemixSession(base);
  const [health, setHealth] = useState<RemixHealth | null>(null);
  const [healthError, setHealthError] = useState<string | null>(null);
  const [cooldownNow, setCooldownNow] = useState(() => Date.now());
  const [dragOverSide, setDragOverSide] = useState<"left" | "right" | null>(null);
  const [draggingRemixItemId, setDraggingRemixItemId] = useState<number | null>(null);
  const [tweakStatus, setTweakStatus] = useState("");
  const [analyzingTweaks, setAnalyzingTweaks] = useState(false);
  const [resultArrivalPop, setResultArrivalPop] = useState(false);
  const [createButtonPop, setCreateButtonPop] = useState(false);
  const [tweakCollapsedAfterCreate, setTweakCollapsedAfterCreate] = useState(false);
  const [saveNotice, setSaveNotice] = useState<RemixTask | null>(null);
  const [resultLightboxOpen, setResultLightboxOpen] = useState(false);
  const [deletingResult, setDeletingResult] = useState(false);
  const [presentation, setPresentation] = useState<{ jobId: string; card: boolean } | null>(null);
  const [uploadBusy, setUploadBusy] = useState<Record<RemixSide, boolean>>({ left: false, right: false });
  const [randomScopes, setRandomScopes] = useState<Record<RemixSide, RandomScope>>(() => ({
    left: readStoredRandomScope("left"),
    right: readStoredRandomScope("right"),
  }));
  const [randomScopeOpenSide, setRandomScopeOpenSide] = useState<RemixSide | null>(null);
  const resultArrivalTimeoutRef = useRef<number | null>(null);
  const createButtonTimeoutRef = useRef<number | null>(null);
  const resultArrivalJobRef = useRef<string | null>(null);
  const consumedSavedJobRef = useRef<string | null>(null);
  const saveInFlightRef = useRef(false);
  const analysisAbortRef = useRef<AbortController | null>(null);
  const consumedSeedKeyRef = useRef<number | null>(null);
  const randomScopeRef = useRef<HTMLSpanElement | null>(null);
  const tweakPanelRef = useRef<HTMLElement | null>(null);

  const result = job?.result;
  const imageUrl = job && result ? `${base}/jobs/${job.id}/image?v=${encodeURIComponent(job.updated_at || "")}` : "";
  const activeJob = job && ["queued", "preparing_media", "calling_gemini"].includes(job.status);
  const retryCooldownRemaining = rateLimitCooldownRemaining(job, cooldownNow);
  const cooldownLocked = job?.status === "rate_limited" && retryCooldownRemaining > 0;
  const sourceSelectionLocked = Boolean(activeJob || analyzingTweaks || recovering || deletingResult || uploadBusy.left || uploadBusy.right);
  const slotLocked = (side: RemixSide) => sourceSelectionLocked || uploadBusy[side];
  const allRandomSourcePool = allItems.filter(
    (item) => isDashboardVisibleItem(item) && !item.archived && (item.thumbnail_path || item.preview_path || item.original_path),
  );
  const filteredRandomSourcePool = visibleItems.filter(
    (item) => !item.archived && isDashboardVisibleItem(item) && (item.thumbnail_path || item.preview_path || item.original_path),
  );
  const canCreate = Boolean(health?.enabled && left && right && !sourceSelectionLocked && !cooldownLocked);
  const canAnalyze = Boolean(health?.enabled && left && right && !sourceSelectionLocked);

  useEffect(() => {
    onBusyChange?.(sourceSelectionLocked);
  }, [sourceSelectionLocked, onBusyChange]);

  useEffect(() => {
    onSourcesChange?.(left, right);
  }, [left, onSourcesChange, right]);

  useEffect(() => {
    if (deletedItemId === null) return;
    if (job?.saved_item?.id === deletedItemId) clearJob();
    if (left?.id === deletedItemId || right?.id === deletedItemId) {
      if (left?.id === deletedItemId) setLeft(null);
      if (right?.id === deletedItemId) setRight(null);
      clearJob();
      resetTweaks();
    }
  }, [deletedItemId]);

  useEffect(() => () => analysisAbortRef.current?.abort(), []);

  useEffect(() => {
    if (!retrySaveRequest) return;
    const controller = new AbortController();
    void fetch(`${base}/jobs/${retrySaveRequest.jobId}`, { signal: controller.signal }).then(async (response) => {
      const payload = await response.json();
      if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
      const target = asRemixJob(payload);
      if (target.status === "saved") completeSavedJob(target);
      else if (target.status === "ready") await saveJob(target);
      else throw new Error("This remix is not ready to save yet");
    }).catch((error) => {
      if (!controller.signal.aborted) failSavedJob(retrySaveRequest.jobId, error instanceof Error ? error.message : "Save failed");
    });
    return () => controller.abort();
  }, [retrySaveRequest]);

  function randomSourcePool(side: RemixSide) {
    return randomScopes[side] === "filtered" ? filteredRandomSourcePool : allRandomSourcePool;
  }

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setHealthError(null);
    fetch(`${base}/health`)
      .then((response) => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      })
      .then((payload: RemixHealth) => {
        if (!cancelled) setHealth(payload);
      })
      .catch(() => {
        if (!cancelled) {
          setHealth(null);
          setHealthError("Cannot reach Remix. Check your connection and try again.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [base, open]);

  useEffect(() => {
    try {
      window.localStorage.setItem(randomScopeStorageKey("left"), randomScopes.left);
      window.localStorage.setItem(randomScopeStorageKey("right"), randomScopes.right);
    } catch {
      // Ignore storage failures; the in-memory preference still works.
    }
  }, [randomScopes]);

  useEffect(() => {
    if (!randomScopeOpenSide) return;

    function closeOnOutsidePointer(event: PointerEvent) {
      if (!randomScopeRef.current?.contains(event.target as Node)) setRandomScopeOpenSide(null);
    }

    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setRandomScopeOpenSide(null);
    }

    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [randomScopeOpenSide]);

  useEffect(() => {
    if (job?.status !== "rate_limited") return;
    setCooldownNow(Date.now());
    const intervalId = window.setInterval(() => {
      setCooldownNow(Date.now());
    }, 1000);
    return () => window.clearInterval(intervalId);
  }, [job?.id, job?.retry_after_seconds, job?.status, job?.updated_at]);

  useEffect(() => {
    const targetScope: RandomScope = contactSheetIsFiltered ? "filtered" : "all";
    setRandomScopes((current) => {
      if (current.left === targetScope && current.right === targetScope) return current;
      return { left: targetScope, right: targetScope };
    });
  }, [contactSheetIsFiltered]);

  useEffect(() => {
    if (!job || job.id === "pending" || !["queued", "preparing_media", "calling_gemini"].includes(job.status)) return;
    let cancelled = false;
    let timeoutId: number | null = null;
    let failures = 0;
    const jobId = job.id;

    const schedulePoll = (delayMs: number) => {
      timeoutId = window.setTimeout(pollJob, delayMs);
    };

    async function pollJob() {
      try {
        const nextJob = await fetch(`${base}/jobs/${jobId}`).then(async (response) => {
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(pickUploadError(payload, `${response.status} ${response.statusText}`.trim()));
          }
          return asRemixJob(payload);
        });
        if (cancelled) return;
        failures = 0;
        setJob(nextJob);
        if (["queued", "preparing_media", "calling_gemini"].includes(nextJob.status)) schedulePoll(900);
      } catch {
        if (cancelled) return;
        failures += 1;
        setJob((current) =>
          current?.id === jobId && ["queued", "preparing_media", "calling_gemini"].includes(current.status)
            ? { ...current, message: "Reconnecting to Remix server" }
            : current,
        );
        schedulePoll(Math.min(5000, 900 * 2 ** Math.min(failures, 3)));
      }
    }

    schedulePoll(900);
    return () => {
      cancelled = true;
      if (timeoutId !== null) window.clearTimeout(timeoutId);
    };
  }, [base, job?.id, job?.status]);

  useEffect(() => {
    if (job?.status !== "saved" || !job.saved_item || consumedSavedJobRef.current === job.id) return;
    completeSavedJob(job);
  }, [job?.id, job?.saved_item, job?.status]);

  useEffect(() => {
    if (!saveNotice || saveNotice.status !== "saving") return;
    const saveJobId = saveNotice.jobId;
    let cancelled = false;
    let consecutiveErrors = 0;

    async function pollSave() {
      try {
        const nextJob = await fetch(`${base}/jobs/${saveJobId}`).then(async (response) => {
          const payload = await response.json();
          if (!response.ok) {
            throw new Error(pickUploadError(payload, `${response.status} ${response.statusText}`.trim()));
          }
          return asRemixJob(payload);
        });
        if (cancelled) return;
        consecutiveErrors = 0;
        if (nextJob.status === "saved" && nextJob.saved_item) {
          completeSavedJob(nextJob);
          return;
        }
        if (nextJob.status === "ready" && (nextJob.error || /save failed/i.test(nextJob.message))) {
          failSavedJob(nextJob.id, nextJob.error || nextJob.message || "Save failed");
          return;
        }
        if (nextJob.status === "failed") {
          failSavedJob(nextJob.id, nextJob.error || nextJob.message || "Save failed");
          return;
        }
        // A pre-save "ready" response must not overwrite a completed save.
      } catch (error) {
        if (cancelled) return;
        consecutiveErrors += 1;
        if (consecutiveErrors >= 5) {
          failSavedJob(saveJobId, error instanceof Error ? error.message : "Save failed");
        }
      }
    }

    const intervalId = window.setInterval(pollSave, SAVE_POLL_MS);
    pollSave();
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [base, job?.id, saveNotice?.jobId, saveNotice?.status]);

  useEffect(() => {
    if (!saveNotice || saveNotice.status === "saving") return;
    const timeoutId = window.setTimeout(() => {
      setSaveNotice((current) => (current === saveNotice ? null : current));
    }, SAVE_NOTICE_HIDE_MS);
    return () => window.clearTimeout(timeoutId);
  }, [saveNotice]);

  useEffect(() => {
    return () => {
      if (resultArrivalTimeoutRef.current !== null) window.clearTimeout(resultArrivalTimeoutRef.current);
      if (createButtonTimeoutRef.current !== null) window.clearTimeout(createButtonTimeoutRef.current);
    };
  }, []);

  useEffect(() => {
    function startDrag(event: Event) {
      if (sourceSelectionLocked) {
        setDraggingRemixItemId(null);
        setDragOverSide(null);
        return;
      }
      const itemId = (event as CustomEvent<{ itemId?: number }>).detail?.itemId;
      setDraggingRemixItemId(Number.isFinite(itemId) ? itemId || null : null);
    }

    function endDrag() {
      setDraggingRemixItemId(null);
      setDragOverSide(null);
    }

    window.addEventListener(REMIX_DRAG_START_EVENT, startDrag);
    window.addEventListener(REMIX_DRAG_END_EVENT, endDrag);
    window.addEventListener("dragend", endDrag);
    return () => {
      window.removeEventListener(REMIX_DRAG_START_EVENT, startDrag);
      window.removeEventListener(REMIX_DRAG_END_EVENT, endDrag);
      window.removeEventListener("dragend", endDrag);
    };
  }, [sourceSelectionLocked]);

  useEffect(() => {
    if (!sourceSelectionLocked) return;
    setDragOverSide(null);
  }, [sourceSelectionLocked]);

  useEffect(() => {
    if (!open || job?.status !== "ready" || !job.result || resultArrivalJobRef.current === job.id) return;
    resultArrivalJobRef.current = job.id;
    if (resultArrivalTimeoutRef.current !== null) window.clearTimeout(resultArrivalTimeoutRef.current);
    setResultArrivalPop(true);
    const panel = document.querySelector<HTMLElement>(".remixResult");
    const image = panel?.querySelector("img");
    let cancelled = false;
    let frame = 0;
    const focusResult = () => {
      if (cancelled) return;
      frame = window.requestAnimationFrame(() => {
        panel?.focus({ preventScroll: true });
        panel?.scrollIntoView({ block: "start", behavior: "auto" });
      });
    };
    // The loaded image determines the page height and available scroll distance.
    if (image) void image.decode().catch(() => {}).then(focusResult);
    else focusResult();
    resultArrivalTimeoutRef.current = window.setTimeout(() => {
      setResultArrivalPop(false);
      resultArrivalTimeoutRef.current = null;
    }, RESULT_ARRIVAL_POP_MS);
    return () => {
      cancelled = true;
      window.cancelAnimationFrame(frame);
    };
  }, [open, job?.id, job?.result, job?.status]);

  useEffect(() => {
    if (!resultLightboxOpen) return undefined;
    function closeOnEscape(event: KeyboardEvent) {
      if (event.key === "Escape") setResultLightboxOpen(false);
    }
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [resultLightboxOpen]);

  useEffect(() => {
    if (!result || !imageUrl) setResultLightboxOpen(false);
  }, [imageUrl, result]);

  useEffect(() => {
    if (recovering || !seed || consumedSeedKeyRef.current === seed.key) return;
    consumedSeedKeyRef.current = seed.key;
    if (seed.behavior === "clear" && seed.side === "left") {
      setLeft(null);
    } else if (seed.behavior === "clear" && seed.side === "right") {
      setRight(null);
    } else if (!seed.item) {
      return;
    } else if (seed.side === "left") {
      setLeft(seed.item);
      if (right?.id === seed.item.id) setRight(null);
    } else if (seed.side === "right") {
      setRight(seed.item);
      if (left?.id === seed.item.id) setLeft(null);
    } else if (seed.behavior === "append") {
      if (!left || left.id === seed.item.id) {
        setLeft(seed.item);
      } else {
        setRight(seed.item);
      }
    } else {
      setLeft(seed.item);
      setRight(null);
    }
    clearJob();
    setDragOverSide(null);
    setResultArrivalPop(false);
    setResultLightboxOpen(false);
    resultArrivalJobRef.current = null;
    resetTweaks();
  }, [left, recovering, right, seed]);

  function flashCreateButton() {
    if (createButtonTimeoutRef.current !== null) window.clearTimeout(createButtonTimeoutRef.current);
    setCreateButtonPop(true);
    createButtonTimeoutRef.current = window.setTimeout(() => {
      setCreateButtonPop(false);
      createButtonTimeoutRef.current = null;
    }, 360);
  }

  function resetTweaks() {
    analysisAbortRef.current?.abort();
    analysisAbortRef.current = null;
    setTweakAnalysis(null);
    setTweakValues({});
    setTweakStatus("");
    setSchizoLevel(0);
    setChronicallyOnlineLevel(0);
    setNoText(false);
    setComicStrip(false);
    setAntiSlop(false);
    setSaferMode(false);
    setTweakCollapsedAfterCreate(false);
    setPresentation(null);
    setAnalyzingTweaks(false);
  }

  function chooseRandom(side: RemixSide) {
    if (slotLocked(side)) return;
    const other = side === "left" ? right : left;
    const candidates = randomSourcePool(side).filter((item) => item.id !== other?.id);
    if (!candidates.length) return;
    const item = candidates[Math.floor(Math.random() * candidates.length)];
    if (side === "left") setLeft(item);
    else setRight(item);
    clearJob();
    resetTweaks();
  }


  function toggleRandomScope(side: RemixSide) {
    setRandomScopeOpenSide((current) => (current === side ? null : side));
  }

  function chooseRandomScope(side: RemixSide, scope: RandomScope) {
    setRandomScopes((current) => (current[side] === scope ? current : { ...current, [side]: scope }));
    setRandomScopeOpenSide(null);
  }

  function chooseSource(side: "left" | "right", item: CatalogItem) {
    if (slotLocked(side)) return;
    const other = side === "left" ? right : left;
    if (item.id === other?.id) {
      if (side === "left") setRight(null);
      else setLeft(null);
    }
    if (side === "left") setLeft(item);
    else setRight(item);
    clearJob();
    resetTweaks();
  }

  function droppedItem(event: ReactDragEvent<HTMLElement>) {
    const rawId = event.dataTransfer.getData(REMIX_DRAG_MIME) || event.dataTransfer.getData("text/plain");
    const id = Number(rawId);
    return Number.isFinite(id) ? allItems.find((item) => item.id === id) || null : null;
  }

  function dragOver(side: "left" | "right", event: ReactDragEvent<HTMLElement>) {
    if (slotLocked(side)) {
      setDragOverSide(null);
      event.dataTransfer.dropEffect = "none";
      return;
    }
    const files = Array.from(event.dataTransfer.files || []);
    const hasSupportedFile = files.some(isRemixUploadFile);
    const hasItem =
      event.dataTransfer.types.includes(REMIX_DRAG_MIME) || event.dataTransfer.types.includes("text/plain") || hasSupportedFile;
    if (!hasItem) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = "copy";
    setDragOverSide(side);
  }

  function leaveDropZone(event: ReactDragEvent<HTMLElement>) {
    const nextTarget = event.relatedTarget;
    if (nextTarget instanceof Node && event.currentTarget.contains(nextTarget)) return;
    setDragOverSide(null);
  }

  function dropSource(side: "left" | "right", event: ReactDragEvent<HTMLElement>) {
    event.preventDefault();
    setDragOverSide(null);
    setDraggingRemixItemId(null);
    if (slotLocked(side)) return;
    const droppedFile = Array.from(event.dataTransfer.files || []).find(isRemixUploadFile);
    if (droppedFile) {
      void uploadToSlot(side, droppedFile);
      return;
    }
    const item = droppedItem(event);
    if (item) chooseSource(side, item);
  }

  function setSlotBusy(side: RemixSide, busy: boolean) {
    setUploadBusy((current) => ({ ...current, [side]: busy }));
  }

  async function uploadToSlot(side: RemixSide, file: File) {
    if (slotLocked(side)) return;
    setSlotBusy(side, true);
    setHealthError(null);
    try {
      const formData = new FormData();
      formData.append("file", file);
      const uploaded = (await fetch(`${base}/uploads`, {
        method: "POST",
        body: formData,
      }).then(async (response) => {
        const payload = await response.json();
        if (!response.ok) {
          const message = pickUploadError(payload, `${response.status} ${response.statusText}`.trim());
          throw new Error(message);
        }
        return asCatalogItem(payload);
      }));

      if (side === "left") {
        setLeft(uploaded);
      } else {
        setRight(uploaded);
      }
      clearJob();
      resetTweaks();
      onUploadComplete(uploaded);
    } catch (error) {
      setHealthError(error instanceof Error ? error.message : "Could not upload file");
    } finally {
      setSlotBusy(side, false);
    }
  }

  async function analyzeTweaks() {
    if (!canAnalyze || !left || !right) return;
    analysisAbortRef.current?.abort();
    const controller = new AbortController();
    analysisAbortRef.current = controller;
    setAnalyzingTweaks(true);
    setTweakCollapsedAfterCreate(false);
    setTweakStatus("Analyzing source pair");
    setHealthError(null);
    scrollToTweaks();
    clearJob();
    try {
      const analysis = (await fetch(`${base}/analysis`, {
        signal: controller.signal,
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ left_id: left.id, right_id: right.id }),
      }).then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new ApiRequestError(payload, response);
        return payload;
      })) as RemixTweakAnalysis;
      if (controller.signal.aborted) return;
      const controls = (analysis.controls || []).slice(0, 4);
      setTweakAnalysis({ ...analysis, controls });
      setTweakValues(Object.fromEntries(controls.map((control) => [control.id, control.default_value ?? 50])));
      setTweakStatus(tweakAnalysisReadyStatus(analysis, controls.length));
      scrollToTweaks();
    } catch (error) {
      if (controller.signal.aborted) return;
      setTweakAnalysis(null);
      setTweakValues({});
      setTweakStatus(
        error instanceof ApiRequestError && error.status === 429
          ? `Analysis limit reached. Try again in ${formatCountdownSeconds(error.retryAfterSeconds ?? DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS)}. Your sources are kept.`
          : error instanceof ApiRequestError && error.status === 503
          ? error.message
          : error instanceof Error && error.message.includes("Choose two different")
          ? error.message
          : "Could not prepare tweak suggestions for this pair. You can still create a remix or swap one source.",
      );
    } finally {
      if (analysisAbortRef.current === controller) {
        analysisAbortRef.current = null;
        setAnalyzingTweaks(false);
      }
    }
  }

  function scrollToTweaks() {
    window.requestAnimationFrame(() => {
      window.requestAnimationFrame(() => {
        tweakPanelRef.current?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
      });
    });
  }

  function selectedTweaks(): RemixTweaks | null {
    const controls = tweakAnalysis?.controls.slice(0, 4) || [];
    if (!controls.length && schizoLevel <= 0 && chronicallyOnlineLevel <= 0 && !noText && !comicStrip && !antiSlop) {
      return null;
    }
    return {
      analysis_prompt_version: tweakAnalysis?.prompt_version || "manual-remix-controls-v1",
      summary: tweakAnalysis?.summary || "",
      controls,
      values: tweakValues,
      schizo_level: schizoLevel,
      chronically_online_level: chronicallyOnlineLevel,
      no_text: noText,
      comic_strip: comicStrip,
      anti_slop: antiSlop,
    };
  }

  function changeTweakValue(id: string, value: number) {
    setTweakValues((current) => ({ ...current, [id]: value }));
  }

  function completeSavedJob(savedJob: RemixJob) {
    if (savedJob.status !== "saved" || !savedJob.saved_item) return;
    if (consumedSavedJobRef.current !== savedJob.id) {
      consumedSavedJobRef.current = savedJob.id;
      publishSaveTask({
        jobId: savedJob.id,
        status: "saved",
        message: "Saved to arsenal",
        detail: itemDisplayTitle(savedJob.saved_item),
        item: savedJob.saved_item,
      });
    }
    saveInFlightRef.current = false;
    setJob((current) => (current?.id === savedJob.id ? { ...current, ...savedJob } : current));
  }

  function failSavedJob(jobId: string, message: string) {
    saveInFlightRef.current = false;
    publishSaveTask({
      jobId,
      status: "failed",
      message: "Save failed",
      detail: message,
    });
  }

  function publishSaveTask(task: Omit<RemixTask, "updatedAt"> & { updatedAt?: string }) {
    const nextTask: RemixTask = {
      ...task,
      updatedAt: task.updatedAt || new Date().toISOString(),
    };
    setSaveNotice(nextTask);
    onSaveTaskChange(nextTask);
  }

  async function createJob() {
    if (!canCreate) return;
    if (!left || !right) return;
    const leftSource = left;
    const rightSource = right;
    const tweaks = selectedTweaks();
    const safetyMode = saferMode ? "safer" : "standard";
    setHealthError(null);
    flashCreateButton();
    setTweakCollapsedAfterCreate(true);
    setResultLightboxOpen(false);
    setJob({
      id: "pending",
      left_id: leftSource.id,
      right_id: rightSource.id,
      safety_mode: safetyMode,
      tweaks: tweaks || undefined,
      status: "queued",
      message: "Queued",
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      retry_after_seconds: null,
      block_reason: null,
      rejection_reason: null,
      error: null,
      embedding_error: null,
      attempts: [],
      failure_explanation: null,
      suggested_retry: null,
    });
    try {
      const created = (await fetch(`${base}/jobs`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          left_id: leftSource.id,
          right_id: rightSource.id,
          safety_mode: safetyMode,
          ...(tweaks ? { tweaks } : {}),
        }),
      }).then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new ApiRequestError(payload, response);
        return asRemixJob(payload);
      }));
      setJob(created);
    } catch (error) {
      const rateLimited = error instanceof ApiRequestError && error.status === 429;
      const message = rateLimited
        ? "Remix limit reached. Your sources and tweaks are kept."
        : error instanceof ApiRequestError ? error.message : "Could not reach Remix. Check your connection and retry.";
      setJob({
        id: "failed",
        left_id: leftSource.id,
        right_id: rightSource.id,
        safety_mode: safetyMode,
        status: rateLimited ? "rate_limited" : "failed",
        message,
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
        retry_after_seconds: rateLimited ? error.retryAfterSeconds ?? DEFAULT_RATE_LIMIT_RETRY_AFTER_SECONDS : null,
        block_reason: null,
        rejection_reason: null,
        error: message,
        embedding_error: null,
        attempts: [],
        failure_explanation: message,
        suggested_retry: null,
      });
    }
  }

  async function saveJob(target = job) {
    if (!target || target.status !== "ready" || saveInFlightRef.current) return;
    const savingJob = target;
    saveInFlightRef.current = true;
    publishSaveTask({
      jobId: savingJob.id,
      status: "saving",
      message: "Saving to arsenal",
      detail: savingJob.result?.summary || "Generated remix",
    });
    try {
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      const saved = await fetch(`${base}/jobs/${savingJob.id}/save`, { method: "POST" }).then(async (response) => {
        const payload = await response.json();
        if (!response.ok) throw new Error(apiErrorMessage(payload, response.status));
        return asUploadSaveResponse(payload);
      });
      if (saved.status === "saved" && saved.item) {
        completeSavedJob({
          ...savingJob,
          status: "saved",
          message: "Saved to arsenal",
          saved_item: saved.item,
          embedding_error: saved.embedding_error ?? null,
        });
        return;
      }
      if (saved.job) {
        if (saved.job.status === "saved" && saved.job.saved_item) {
          completeSavedJob(saved.job);
        }
      }
    } catch (error) {
      failSavedJob(savingJob.id, error instanceof Error ? error.message : "Save failed");
      setJob((current) =>
        current?.id === savingJob.id
          ? {
              ...savingJob,
              status: "ready",
              message: "Save failed",
              error: error instanceof Error ? error.message : "Save failed",
            }
          : current,
      );
    }
  }

  function presentResult(card: boolean) {
    if (!job) return;
    setPresentation({ jobId: job.id, card });
    if (!card && !job.saved_item) void saveJob();
  }

  async function deleteResult() {
    if (!job?.saved_item || deletingResult) return;
    setDeletingResult(true);
    try {
      if (await onDeleteSaved(job.saved_item)) dismissResult();
    } finally {
      setDeletingResult(false);
    }
  }

  function dismissResult() {
    if (!job) return;
    if (resultArrivalTimeoutRef.current !== null) {
      window.clearTimeout(resultArrivalTimeoutRef.current);
      resultArrivalTimeoutRef.current = null;
    }
    const dismissedJobId = job.id;
    resultArrivalJobRef.current = null;
    setResultArrivalPop(false);
    setResultLightboxOpen(false);
    setSaveNotice((current) => (current?.jobId === dismissedJobId && current.status !== "saving" ? null : current));
    clearJob();
  }

  if (!open) return null;

  const pickSourcesMessage = !left || !right ? "Pick two memes" : "";
  const statusMessage =
    recoveryError ||
    (recovering ? "Reconnecting to your remix" : "") ||
    healthError ||
    pickSourcesMessage ||
    (health?.enabled ? "" : health?.message || "Checking Remix");
  const actionStatusMessage = mobileLayout && statusMessage === pickSourcesMessage ? "" : statusMessage;
  const isCallingGemini = job?.status === "calling_gemini";
  const currentSaveStatus = saveNotice && saveNotice.jobId === job?.id ? saveNotice.status : null;
  const isSaving = currentSaveStatus === "saving";
  const isSaved = currentSaveStatus === "saved" || job?.status === "saved";
  const canSaveResult = Boolean(job?.status === "ready" && !isSaving && !isSaved);
  const generatingPanelStyle = remixGeneratingStyle(tweakAnalysis, tweakValues);
  const showCreateBusyState = Boolean(activeJob);
  const showStatusDock = Boolean(!showCreateBusyState && actionStatusMessage);

  return (
    <section className="remixPane" aria-label="Remix" aria-busy={isCallingGemini}>
      {base === "/api/remix" && <RecoveryPickers base={base} onJob={selectJob} onDraft={(item) => { clearJob(); setLeft(item); setRight(null); }} />}
      <div className="remixWorkspace">
        <RemixSourceSlot
          label="A"
          item={left}
          onRandom={() => chooseRandom("left")}
          onUpload={(file) => uploadToSlot("left", file)}
          canChooseRandom={randomSourcePool("left").some((item) => item.id !== right?.id)}
          randomScope={randomScopes.left}
          randomScopeLabel={randomScopeLabel(randomScopes.left)}
          randomScopeOpen={randomScopeOpenSide === "left"}
          randomAllCount={allRandomSourcePool.length}
          randomFilteredCount={filteredRandomSourcePool.length}
          randomScopeRef={randomScopeRef}
          onToggleRandomScope={() => toggleRandomScope("left")}
          onChooseRandomScope={(scope) => chooseRandomScope("left", scope)}
          onFocus={onFocusItem}
          onBrowse={onBrowse}
          onDropItem={(event) => dropSource("left", event)}
          isDragOver={!sourceSelectionLocked && dragOverSide === "left"}
          showDropHint={!sourceSelectionLocked && draggingRemixItemId !== null}
          dropDisabled={sourceSelectionLocked}
          uploadBusy={uploadBusy.left}
          onDragOver={(event) => dragOver("left", event)}
          onDragLeave={leaveDropZone}
        />
        <div className="remixCenter">
          <div className="remixActionGroup">
            <button className="secondaryAction remixAnalyze" type="button" onClick={analyzeTweaks} disabled={!canAnalyze}>
              {analyzingTweaks ? <RefreshCw size={16} /> : <SlidersHorizontal size={16} />}
              {analyzingTweaks ? "Analyzing" : "Analyze"}
            </button>
            <button
              className={`secondaryAction remixSafety ${saferMode ? "active" : ""}`}
              type="button"
              onClick={() => setSaferMode((current) => !current)}
              disabled={Boolean(activeJob)}
              aria-pressed={saferMode}
              title="Start Gemini with softened metadata and still-frame context"
            >
              <ShieldAlert size={16} />
              Safer
            </button>
          </div>
          {showStatusDock ? (
            <div className="remixStatusDock" aria-live="polite">
              {!showCreateBusyState && actionStatusMessage ? (
                <span className={`remixStatus ${job?.status || healthError ? "active" : ""}`}>{actionStatusMessage}</span>
              ) : null}
            </div>
          ) : null}
        </div>
        <RemixSourceSlot
          label="B"
          item={right}
          onRandom={() => chooseRandom("right")}
          onUpload={(file) => uploadToSlot("right", file)}
          canChooseRandom={randomSourcePool("right").some((item) => item.id !== left?.id)}
          randomScope={randomScopes.right}
          randomScopeLabel={randomScopeLabel(randomScopes.right)}
          randomScopeOpen={randomScopeOpenSide === "right"}
          randomAllCount={allRandomSourcePool.length}
          randomFilteredCount={filteredRandomSourcePool.length}
          randomScopeRef={randomScopeRef}
          onToggleRandomScope={() => toggleRandomScope("right")}
          onChooseRandomScope={(scope) => chooseRandomScope("right", scope)}
          onFocus={onFocusItem}
          onBrowse={onBrowse}
          onDropItem={(event) => dropSource("right", event)}
          isDragOver={!sourceSelectionLocked && dragOverSide === "right"}
          showDropHint={!sourceSelectionLocked && draggingRemixItemId !== null}
          dropDisabled={sourceSelectionLocked}
          uploadBusy={uploadBusy.right}
          onDragOver={(event) => dragOver("right", event)}
          onDragLeave={leaveDropZone}
        />
      </div>
      {left && right ? <RemixTweakPanel
        panelRef={tweakPanelRef}
        analysis={tweakAnalysis}
        values={tweakValues}
        status={tweakStatus}
        analyzing={sourceSelectionLocked}
        collapsed={tweakCollapsedAfterCreate}
        setCollapsed={setTweakCollapsedAfterCreate}
        schizoLevel={schizoLevel}
        chronicallyOnlineLevel={chronicallyOnlineLevel}
        noText={noText}
        comicStrip={comicStrip}
        antiSlop={antiSlop}
        onChange={changeTweakValue}
        onSchizoLevelChange={setSchizoLevel}
        onChronicallyOnlineLevelChange={setChronicallyOnlineLevel}
        onNoTextChange={setNoText}
        onComicStripChange={setComicStrip}
        onAntiSlopChange={setAntiSlop}
      /> : null}
      {left && right ? <div className="remixCreateRow">
        <button className={`primaryButton remixCreate ${createButtonPop && canCreate ? "is-popping" : ""}`} type="button" onClick={createJob} disabled={!canCreate}>
          <Sparkles size={18} />{cooldownLocked ? `Retry in ${formatCountdownSeconds(retryCooldownRemaining)}` : job && ["blocked", "failed", "rate_limited"].includes(job.status) ? "Retry remix" : "Create Remix"}
        </button>
      </div> : null}
      <RemixAttemptHistory job={job} />
      {result ? (
        <section tabIndex={-1} className={`remixResult ${resultArrivalPop ? "is-arriving" : ""}`}>
          <div className="remixResultMedia">
            <button
              className="remixResultImageButton"
              type="button"
              onClick={() => setResultLightboxOpen(true)}
              aria-label="Open generated remix full size"
            >
              <img src={imageUrl} alt="" />
              <span className="remixResultZoomHint" aria-hidden="true">
                <Maximize2 size={15} />
              </span>
            </button>
          </div>
          <div className="remixResultBody">
            <h2>{result.summary || "Generated remix"}</h2>
            <div className="detailActions remixResultActions">
              <button className="secondaryAction remixCancelResult" type="button" onClick={dismissResult} disabled={deletingResult}>
                <X size={17} />
                Scrap
              </button>
              <button className="secondaryAction" type="button" onClick={createJob} disabled={!canCreate}>
                <RefreshCw size={17} />
                Retry
              </button>
              <button
                className={`primaryButton remixSaveButton ${isSaved ? "dangerAction" : ""}`}
                type="button"
                onClick={() => isSaved ? deleteResult() : saveJob()}
                disabled={isSaved ? deletingResult : !canSaveResult}
                aria-label={isSaved ? "Delete from Arsenal" : "Save to Arsenal"}
                title={isSaved ? "Delete from Arsenal" : "Save to Arsenal"}
              >
                {deletingResult || isSaving ? <RefreshCw size={17} className="spinIcon" /> : isSaved ? <Trash2 size={17} /> : <Plus size={17} />}
                {deletingResult ? "Deleting" : isSaving ? "Saving" : isSaved ? "Delete" : "Save"}
              </button>
            </div>
            {catalogApiBase ? <div className="remixShareActions">
              <button className="secondaryAction" type="button" onClick={() => presentResult(false)} disabled={isSaving || deletingResult}><Share2 size={17} />Share media</button>
              <button className="secondaryAction" type="button" onClick={() => presentResult(true)} disabled={deletingResult}><ImageIcon size={17} />Remix Page</button>
              {presentation?.jobId === job?.id && !presentation?.card && !job?.saved_item ? <span role="status">{currentSaveStatus === "failed" ? "Save failed. Retry Save to prepare sharing." : "Saving before sharing"}</span> : null}
            </div> : null}
          </div>
        </section>
      ) : null}
      {catalogApiBase && presentation && presentation.jobId === job?.id && job?.saved_item ? <RemixPresentation key={`${job.id}-${presentation.card}`} item={job.saved_item} apiBase={catalogApiBase} card={presentation.card} onShareChange={onShareChange} onClose={() => setPresentation(null)} /> : null}
      {catalogApiBase && presentation?.card && presentation.jobId === job?.id && job && !job.saved_item ? <RemixPresentation key={`${job.id}-draft`} draft={{ id: job.id, image: `${base}/jobs/${job.id}/card`, saving: isSaving, error: currentSaveStatus === "failed" ? job.error || "Save failed. Please retry." : undefined, onSave: () => { void saveJob(); } }} apiBase={catalogApiBase} card onClose={() => setPresentation(null)} /> : null}
      {showCreateBusyState ? <GenerationOverlay style={generatingPanelStyle} job={job} /> : null}
      {resultLightboxOpen && imageUrl ? (
        <div
          className="remixLightbox"
          role="dialog"
          aria-modal="true"
          aria-label="Generated remix full size"
          onClick={(event) => {
            if (event.currentTarget === event.target) setResultLightboxOpen(false);
          }}
        >
          <button
            className="iconButton remixLightboxClose"
            type="button"
            onClick={() => setResultLightboxOpen(false)}
            aria-label="Close generated remix preview"
          >
            <X size={18} />
          </button>
          <img className="remixLightboxImage" src={imageUrl} alt="Generated remix full size" />
        </div>
      ) : null}
    </section>
  );
}

function Gallery({
  items,
  selectedId,
  onSelect,
  remixAssignments,
  onClearRemixSide,
  removingId,
  onRemix,
  remixBusy,
}: {
  items: CatalogItem[];
  selectedId: number | null;
  onSelect: (id: number) => void;
  remixAssignments: Record<RemixSide, CatalogItem | null>;
  onClearRemixSide: (side: RemixSide, item: CatalogItem) => void;
  removingId: number | null;
  onRemix: (item: CatalogItem, side?: RemixSide) => void;
  remixBusy: boolean;
}) {
  return (
    <section className="galleryGrid">
      {items.map((item, index) => (
        <ItemCard
          key={item.id}
          item={item}
          selected={item.id === selectedId}
          priority={index < GALLERY_EAGER_MEDIA_COUNT}
          fetchPriority={index === 0}
          onSelect={() => onSelect(item.id)}
          remixSide={remixAssignments.left?.id === item.id ? "left" : remixAssignments.right?.id === item.id ? "right" : null}
          onClearRemixSide={onClearRemixSide}
          removing={item.id === removingId}
          onRemix={(side) => onRemix(item, side)}
          remixBusy={remixBusy}
        />
      ))}
    </section>
  );
}

function LoadMore({
  shown,
  total,
  hasMore,
  loading,
  onLoadMore,
}: {
  shown: number;
  total: number | null;
  hasMore: boolean;
  loading: boolean;
  onLoadMore: () => void;
}) {
  const sentinelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!hasMore || loading) return;
    const sentinel = sentinelRef.current;
    if (!sentinel || !("IntersectionObserver" in window)) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) onLoadMore();
      },
      { rootMargin: LOAD_MORE_ROOT_MARGIN },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loading, onLoadMore, shown]);

  if (!hasMore) return null;
  return (
    <div className="loadMoreRow" ref={sentinelRef}>
      <button className="secondaryAction" type="button" onClick={onLoadMore} disabled={loading}>
        {loading ? "Loading" : "Load more"}
        <span>{total === null ? `${shown} loaded` : `${shown} of ${total}`}</span>
      </button>
    </div>
  );
}

export default function App() {
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<Filters>(emptyFilters);
  const debouncedQuery = useDebouncedValue(query, SEARCH_QUERY_DEBOUNCE_MS);
  const deferredQuery = useDeferredValue(debouncedQuery);
  const normalizedQuery = useMemo(() => normalizeDisplayText(deferredQuery), [deferredQuery]);
  const activeSearchQuery = normalizedQuery.length >= MIN_SEARCH_QUERY_LENGTH ? normalizedQuery : "";
  const {
    items,
    matchedItems,
    index,
    config,
    error,
    total,
    hasMore,
    loading,
    loadMore,
    upsertItem,
    removeItem,
  } = useCatalog(filters, activeSearchQuery);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [view, setView] = useState<View>("gallery");
  const [showHidden, setShowHidden] = useState(false);
  const [remixOpen, setRemixOpen] = useState(readRemixOpen);
  const [remixAssignments, setRemixAssignments] = useState<Record<RemixSide, CatalogItem | null>>({ left: null, right: null });
  const [mobilePanel, setMobilePanel] = useState<"browse" | "detail">("browse");
  const topbarRef = useRef<HTMLElement | null>(null);
  const mobileActionBarRef = useRef<HTMLElement | null>(null);
  const mobileGalleryPositionRef = useRef<MobileGalleryPosition>({ scrollY: 0, itemId: null, cardTop: null });
  const restoreMobileGalleryPositionRef = useRef(false);
  const pageScrollRef = useRef({ gallery: 0, remix: 0 });
  const detailOriginRef = useRef<"gallery" | "remix">("gallery");
  const [toast, setToast] = useState<{ message: string; error?: boolean; retryJobId?: string } | null>(null);
  const [deletedItemId, setDeletedItemId] = useState<number | null>(null);
  const [retrySaveRequest, setRetrySaveRequest] = useState<{ jobId: string; key: number } | null>(null);
  const [mobileChrome, setMobileChrome] = useState({ topbarHeight: 112, actionBarHeight: 68 });
  const [remixSeed, setRemixSeed] = useState<RemixSeed | null>(null);
  const [detailWidth, setDetailWidth] = useState(readStoredDetailPanelWidth);
  const [resizingDetail, setResizingDetail] = useState(false);
  const [remixDraftItems, setRemixDraftItems] = useState<Record<number, CatalogItem>>({});
  const [uploadSaveStatuses, setUploadSaveStatuses] = useState<Record<number, DraftUploadSaveStatus>>({});
  const [publicShares, setPublicShares] = useState<Record<number, PublicShareState>>({});
  const [publicShareLoadingId, setPublicShareLoadingId] = useState<number | null>(null);
  const [publicShareBusyId, setPublicShareBusyId] = useState<number | null>(null);
  const [publicShareNotice, setPublicShareNotice] = useState<PublicShareNotice | null>(null);
  const [remixBusy, setRemixBusy] = useState(false);
  const [deletingRemixId, setDeletingRemixId] = useState<number | null>(null);
  const uploadSaveStatusTimeouts = useRef<Map<number, number>>(new Map());
  const handleRemixBusyChange = useCallback((busy: boolean) => {
    setRemixBusy((current) => (current === busy ? current : busy));
  }, []);
  const handleRemixSourcesChange = useCallback((left: CatalogItem | null, right: CatalogItem | null) => {
    setRemixAssignments((current) => (
      current.left?.id === left?.id && current.right?.id === right?.id ? current : { left, right }
    ));
  }, []);
  const isMobile = useMobileLayout();

  useEffect(() => { persistRemixOpen(remixOpen); }, [remixOpen]);

  useEffect(() => {
    if (!toast || toast.error) return;
    const timeout = window.setTimeout(() => setToast(null), 2400);
    return () => window.clearTimeout(timeout);
  }, [toast]);

  useLayoutEffect(() => {
    const top = remixOpen ? pageScrollRef.current.remix : pageScrollRef.current.gallery;
    window.scrollTo({ top, behavior: "auto" });
  }, [remixOpen]);

  useEffect(() => {
    setFilters((current) => current.archived === showHidden ? current : { ...current, archived: showHidden });
  }, [showHidden]);

  const itemById = useMemo(() => new Map([...matchedItems, ...items].map((item) => [item.id, item])), [items, matchedItems]);
  const visibleRemixDraftItems = useMemo(() => Object.values(remixDraftItems), [remixDraftItems]);
  const currentViewItems = matchedItems;
  const allRemixItems = useMemo(() => {
    if (!visibleRemixDraftItems.length) return currentViewItems;
    const visibleDraftIds = new Set(visibleRemixDraftItems.map((item) => item.id));
    return [...visibleRemixDraftItems, ...currentViewItems.filter((item) => !visibleDraftIds.has(item.id))];
  }, [currentViewItems, visibleRemixDraftItems]);
  const selectedCatalogItem = selectedId !== null
    ? itemById.get(selectedId) || remixDraftItems[selectedId] || null
    : null;
  const detailItem = selectedCatalogItem;
  const detailCanPublicShare = Boolean(
    config.apiBase &&
      detailItem &&
      detailItem.processing_status === "processed" &&
      !isDraftUploadItem(detailItem),
  );
  const detailPublicShare = detailItem ? publicShares[detailItem.id] || null : null;
  const detailPublicShareKnown = Boolean(detailItem && Object.prototype.hasOwnProperty.call(publicShares, detailItem.id));
  const detailPublicShareLoading = Boolean(
    detailCanPublicShare && detailItem && (!detailPublicShareKnown || publicShareLoadingId === detailItem.id),
  );
  const detailPublicShareBusy = Boolean(detailItem && publicShareBusyId === detailItem.id);
  const visibleItems = currentViewItems;
  const galleryPageItems = items;
  const allRandomItems = useMemo(() => currentViewItems.filter((item) => !item.archived), [currentViewItems]);
  const canChooseRandom = Boolean(config.apiBase || allRandomItems.length);
  const filtersAreActive = !isDefaultRemixFilter(filters);
  const contactSheetIsFiltered = Boolean(activeSearchQuery) || filtersAreActive;
  const showGalleryPanel = !isMobile || mobilePanel === "browse";
  const showDetailPanel = !isMobile || mobilePanel === "detail";
  const workspaceStyle = useMemo(
    () => ({ "--detail-panel-width": `${detailWidth}px` }) as CSSProperties,
    [detailWidth],
  );
  const shellStyle = useMemo(
    () => ({
      "--topbar-height": `${mobileChrome.topbarHeight}px`,
      "--mobile-topbar-height": `${mobileChrome.topbarHeight}px`,
      "--mobile-actionbar-height": `${mobileChrome.actionBarHeight}px`,
    } as CSSProperties),
    [mobileChrome.actionBarHeight, mobileChrome.topbarHeight],
  );

  useLayoutEffect(() => {
    const measureChrome = () => {
      const topbarHeight = Math.ceil(topbarRef.current?.getBoundingClientRect().height || 112);
      const actionBarHeight = Math.ceil(mobileActionBarRef.current?.getBoundingClientRect().height || 68);
      setMobileChrome((current) =>
        current.topbarHeight === topbarHeight && current.actionBarHeight === actionBarHeight
          ? current
          : { topbarHeight, actionBarHeight },
      );
    };
    measureChrome();
    let frame = 0;
    const scheduleMeasure = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(measureChrome);
    };
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(scheduleMeasure);
    if (topbarRef.current) observer?.observe(topbarRef.current);
    if (mobileActionBarRef.current) observer?.observe(mobileActionBarRef.current);
    window.addEventListener("resize", scheduleMeasure);

    return () => {
      window.cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", scheduleMeasure);
    };
  }, []);

  useEffect(() => {
    if (!isMobile) {
      setMobilePanel("browse");
    }
  }, [isMobile]);

  useLayoutEffect(() => {
    if (!isMobile || view !== "gallery" || mobilePanel !== "browse" || !restoreMobileGalleryPositionRef.current) {
      return undefined;
    }
    restoreMobileGalleryPositionRef.current = false;
    const position = mobileGalleryPositionRef.current;
    const restorePosition = () => {
      const card = position.itemId
        ? document.querySelector<HTMLElement>(`.itemCard[data-item-id="${position.itemId}"]`)
        : null;
      const top =
        card && position.cardTop !== null
          ? window.scrollY + card.getBoundingClientRect().top - position.cardTop
          : position.scrollY;
      window.scrollTo({ top: Math.max(0, top), behavior: "auto" });
    };
    restorePosition();
    // content-visibility cards replace estimated heights after the first paint.
    let frame = window.requestAnimationFrame(() => {
      restorePosition();
      frame = window.requestAnimationFrame(restorePosition);
    });
    return () => window.cancelAnimationFrame(frame);
  }, [isMobile, mobilePanel, view, remixOpen]);

  useEffect(() => {
    if (!detailCanPublicShare || !detailItem || !config.apiBase) return undefined;
    const itemId = detailItem.id;
    const controller = new AbortController();
    let current = true;
    setPublicShareLoadingId(itemId);
    setPublicShareNotice((notice) => (notice?.itemId === itemId ? null : notice));

    fetch(apiUrl(config.apiBase, `/items/${itemId}/share`), { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) {
          throw new Error(await responseError(response, "Could not check public share"));
        }
        return asPublicShareState(await response.json());
      })
      .then((share) => {
        if (!current) return;
        setPublicShares((shares) => ({ ...shares, [itemId]: share }));
      })
      .catch((error) => {
        if (!current || controller.signal.aborted) return;
        setPublicShares((shares) => ({
          ...shares,
          [itemId]: { active: false, url: null, media_url: null, created_at: null, revoked_at: null },
        }));
        setPublicShareNotice({
          itemId,
          message: error instanceof Error ? error.message : "Could not check public share",
          error: true,
        });
      })
      .finally(() => {
        if (!current) return;
        setPublicShareLoadingId((loadingId) => (loadingId === itemId ? null : loadingId));
      });

    return () => {
      current = false;
      controller.abort();
    };
  }, [config.apiBase, detailCanPublicShare, detailItem?.id]);

  const mediaCount = total ?? visibleItems.length;

  useEffect(() => {
    return () => {
      uploadSaveStatusTimeouts.current.forEach((timeout) => {
        window.clearTimeout(timeout);
      });
      uploadSaveStatusTimeouts.current.clear();
    };
  }, []);

  function clearUploadSaveStatus(itemId: number) {
    const timeout = uploadSaveStatusTimeouts.current.get(itemId);
    if (timeout !== undefined) {
      window.clearTimeout(timeout);
      uploadSaveStatusTimeouts.current.delete(itemId);
    }
    setUploadSaveStatuses((current) => {
      if (!current[itemId]) return current;
      const next = { ...current };
      delete next[itemId];
      return next;
    });
  }

  function setUploadSaveStatus(itemId: number, status: DraftUploadSaveStatus["status"], message: string) {
    clearUploadSaveStatus(itemId);
    setUploadSaveStatuses((current) => ({
      ...current,
      [itemId]: { itemId, status, message },
    }));
  }

  function scheduleUploadSaveStatusClear(itemId: number, delayMs: number = SAVE_NOTICE_HIDE_MS) {
    clearUploadSaveStatus(itemId);
    const timeout = window.setTimeout(() => {
      clearUploadSaveStatus(itemId);
    }, delayMs);
    uploadSaveStatusTimeouts.current.set(itemId, timeout);
  }

  function upsertRemixDraft(item: CatalogItem) {
    setRemixDraftItems((current) => ({ ...current, [item.id]: item }));
  }

  function removeRemixDraft(itemId: number) {
    setRemixDraftItems((current) => {
      if (!current[itemId]) return current;
      const next = { ...current };
      delete next[itemId];
      return next;
    });
  }

  function rememberMobileGalleryPosition(itemId: number) {
    if (!isMobile || remixOpen || view !== "gallery" || mobilePanel !== "browse") return;
    pageScrollRef.current.gallery = window.scrollY;
    const card = document.querySelector<HTMLElement>(`.itemCard[data-item-id="${itemId}"]`);
    mobileGalleryPositionRef.current = {
      scrollY: window.scrollY,
      itemId,
      cardTop: card?.getBoundingClientRect().top ?? null,
    };
  }

  function selectFromCatalogItem(item: CatalogItem) {
    detailOriginRef.current = remixOpen ? "remix" : "gallery";
    if (remixOpen) {
      pageScrollRef.current.remix = window.scrollY;
      if (!itemById.has(item.id)) upsertRemixDraft(item);
    }
    setSelectedId(item.id);
    clearUploadSaveStatus(item.id);
    setView("gallery");
    setRemixOpen(false);
    if (isMobile) {
      rememberMobileGalleryPosition(item.id);
      setMobilePanel("detail");
    }
    window.setTimeout(() => {
      const detail = document.querySelector<HTMLElement>(".detail");
      if (isMobile) detail?.scrollTo({ top: 0 });
      else detail?.scrollIntoView({ block: "nearest" });
    }, 0);
  }

  function changeQuery(value: string) {
    setQuery(value);
    setView("gallery");
    if (isMobile) {
      setMobilePanel("browse");
    }
  }

  async function chooseRandom() {
    if (config.apiBase) {
      try {
        const response = await fetch(apiUrl(config.apiBase, "/items/random", apiItemParams({ ...filters, archived: false }, activeSearchQuery)), {
          cache: "no-store",
        });
        if (response.status === 404) return;
        if (!response.ok) throw new Error(String(response.status));
        const item = asCatalogItem(await response.json());
        upsertItem(item);
        selectFromCatalogItem(item);
      } catch {
        if (!allRandomItems.length) return;
        const item = allRandomItems[Math.floor(Math.random() * allRandomItems.length)];
        selectFromCatalogItem(item);
      }
      return;
    }
    if (!allRandomItems.length) return;
    const item = allRandomItems[Math.floor(Math.random() * allRandomItems.length)];
    selectFromCatalogItem(item);
  }

  function publicShareTitle(item: CatalogItem) {
    return item.summary || item.caption || item.source_title || "Shared media";
  }

  function setPublicShareOutcome(itemId: number, outcome: "shared" | "copied" | "ready") {
    const message = outcome === "shared" ? "Shared" : outcome === "copied" ? "Public link copied" : "Public link ready";
    setPublicShareNotice({ itemId, message, error: false });
  }

  function handleShareChange(itemId: number, share: PublicShareState) {
    if (share.variant === "recipe") {
      if (!config.apiBase) return;
      void fetch(apiUrl(config.apiBase, `/items/${itemId}/share`))
        .then(async response => { if (response.ok) handleShareChange(itemId, asPublicShareState(await response.json())); })
        .catch(() => {});
      return;
    }
    setPublicShares((current) => ({ ...current, [itemId]: share }));
  }

  async function createPublicShare(item: CatalogItem) {
    if (!config.apiBase || publicShareBusyId === item.id) return;
    setPublicShareBusyId(item.id);
    setPublicShareNotice(null);
    let share: PublicShareState | null = null;
    try {
      const response = await fetch(apiUrl(config.apiBase, `/items/${item.id}/share`), { method: "POST" });
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not create public link"));
      }
      share = asPublicShareState(await response.json());
      setPublicShares((shares) => ({ ...shares, [item.id]: share as PublicShareState }));
      setPublicShareNotice({ itemId: item.id, message: "Public link ready", error: false });
    } catch (error) {
      setPublicShareNotice({
        itemId: item.id,
        message: error instanceof Error ? error.message : "Could not create public link",
        error: true,
      });
    } finally {
      setPublicShareBusyId((busyId) => (busyId === item.id ? null : busyId));
    }
    if (share?.url) {
      setPublicShareOutcome(item.id, await presentPublicShare(share.url, publicShareTitle(item)));
    }
  }

  async function sharePublicLink(item: CatalogItem, share: PublicShareState) {
    if (!share.url) return;
    setPublicShareOutcome(item.id, await presentPublicShare(share.url, publicShareTitle(item)));
  }

  async function recallPublicShare(item: CatalogItem) {
    if (!config.apiBase || publicShareBusyId === item.id) return;
    const confirmed = window.confirm("Recall this public link? The current URL will stop working immediately.");
    if (!confirmed) return;
    setPublicShareBusyId(item.id);
    setPublicShareNotice(null);
    try {
      const response = await fetch(apiUrl(config.apiBase, `/items/${item.id}/share`), { method: "DELETE" });
      if (!response.ok) {
        throw new Error(await responseError(response, "Could not recall public link"));
      }
      const share = asPublicShareState(await response.json());
      setPublicShares((shares) => ({ ...shares, [item.id]: share }));
      setPublicShareNotice({ itemId: item.id, message: "Public link recalled", error: false });
    } catch (error) {
      setPublicShareNotice({
        itemId: item.id,
        message: error instanceof Error ? error.message : "Could not recall public link",
        error: true,
      });
    } finally {
      setPublicShareBusyId((busyId) => (busyId === item.id ? null : busyId));
    }
  }

  async function retryClassification(item: CatalogItem) {
    if (!config.apiBase) return;
    setPublicShareNotice({ itemId: item.id, message: "Retrying classification", error: false });
    try {
      const response = await fetch(apiUrl(config.apiBase, `/items/${item.id}/classification/retry`), { method: "POST" });
      if (!response.ok) throw new Error(await responseError(response, "Could not retry classification"));
      const updated = asCatalogItem(await response.json());
      upsertItem(updated);
      setSelectedId(updated.id);
      setPublicShareNotice({
        itemId: item.id,
        message: isBlockedClassification(updated) ? "Still blocked. You can try again later." : "Classification updated",
        error: isBlockedClassification(updated),
      });
    } catch (error) {
      setPublicShareNotice({ itemId: item.id, message: error instanceof Error ? error.message : "Could not retry classification", error: true });
    }
  }

  async function setItemHidden(item: CatalogItem, archived: boolean) {
    if (!config.apiBase) return;
    try {
      const response = await fetch(apiUrl(config.apiBase, `/session/items/${item.id}`), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ archived }),
      });
      if (!response.ok) throw new Error(await responseError(response, "Could not update item"));
      const updated = asCatalogItem(await response.json());
      setRemixDraftItems((current) => current[item.id] ? { ...current, [item.id]: updated } : current);
      if (archived && !showHidden) removeItem(item.id);
      else upsertItem(updated);
      setSelectedId(null);
      if (isMobile) showGalleryView();
    } catch (error) {
      setPublicShareNotice({ itemId: item.id, message: error instanceof Error ? error.message : "Could not update item", error: true });
    }
  }

  async function deleteRemix(item: CatalogItem): Promise<boolean> {
    if (!config.apiBase || deletingRemixId !== null) return false;
    if (!window.confirm("Delete this remix permanently? Its public share link will stop working.")) return false;
    setDeletingRemixId(item.id);
    try {
      const response = await fetch(apiUrl(config.apiBase, `/remix/items/${item.id}`), { method: "DELETE" });
      if (!response.ok) throw new Error(await responseError(response, "Could not delete remix"));
      await new Promise((resolve) => window.setTimeout(resolve, 220));
      removeItem(item.id);
      removeRemixDraft(item.id);
      setDeletedItemId(item.id);
      setSelectedId(null);
      setToast({ message: "Remix deleted" });
      if (isMobile && !remixOpen) showGalleryView();
      return true;
    } catch (error) {
      setPublicShareNotice({ itemId: item.id, message: error instanceof Error ? error.message : "Could not delete remix", error: true });
      setToast({ message: error instanceof Error ? error.message : "Could not delete remix", error: true });
      return false;
    } finally {
      setDeletingRemixId(null);
    }
  }

  function handleRemixTaskChange(task: RemixTask) {
    const normalizedTask = {
      ...task,
      updatedAt: task.updatedAt || new Date().toISOString(),
    };
    if (normalizedTask.status === "saved") setToast({ message: "Saved" });
    if (normalizedTask.status === "failed") setToast({ message: normalizedTask.detail || "Save failed. Please retry.", error: true, retryJobId: normalizedTask.jobId });
    if (normalizedTask.status === "saved" && normalizedTask.item) {
      upsertItem(normalizedTask.item);
      removeRemixDraft(normalizedTask.item.id);
    }
  }


  function focusItem(item: CatalogItem) {
    selectFromCatalogItem(item);
  }

  function focusItemById(id: number) {
    const source = itemById.get(id) || remixDraftItems[id];
    if (!source) return;
    selectFromCatalogItem(source);
  }

  function handleUploadComplete(item: CatalogItem) {
    upsertRemixDraft(item);
    clearUploadSaveStatus(item.id);
  }

  async function saveUploadedItemToArsenal(item: CatalogItem) {
    if (!isDraftUploadItem(item)) return;
    if (uploadSaveStatuses[item.id]?.status === "saving") return;
    setUploadSaveStatus(item.id, "saving", "Saving to arsenal");
    try {
      const payload = await fetch(`${config.remixApiBase || "../../api/remix"}/uploads/${item.id}/save`, { method: "POST" }).then(
        async (response) => {
          const body = await response.json();
          if (!response.ok) throw new Error(pickUploadError(body, `${response.status} ${response.statusText}`.trim()));
          return asUploadSaveResponse(body);
        },
      );
      if (!payload.item) throw new Error("Save completed without a saved item");
      upsertItem(payload.item);
      removeRemixDraft(item.id);
      setUploadSaveStatus(item.id, "saved", "Saved to arsenal");
      scheduleUploadSaveStatusClear(item.id);
      setSelectedId(item.id);
    } catch (error) {
      setUploadSaveStatus(item.id, "failed", error instanceof Error ? error.message : "Could not save upload");
      scheduleUploadSaveStatusClear(item.id);
    }
  }

  function startRemixFromDetail(item: CatalogItem, requestedSide?: RemixSide) {
    const side: RemixSide = !remixAssignments.left ? "left" : !remixAssignments.right ? "right" : "left";
    addItemToRemix(requestedSide || side, item);
  }

  function addItemToRemix(side: RemixSide, item: CatalogItem) {
    if (remixBusy) return;
    if (remixAssignments[side]?.id === item.id) return;
    setRemixSeed((current) => ({ item, key: (current?.key || 0) + 1, side, behavior: "replace" }));
    setRemixAssignments((current) => {
      const other = side === "left" ? "right" : "left";
      return { ...current, [side]: item, [other]: current[other]?.id === item.id ? null : current[other] };
    });
  }

  function clearRemixSide(side: RemixSide, item: CatalogItem) {
    if (remixBusy) return;
    setRemixSeed((current) => ({ key: (current?.key || 0) + 1, side, behavior: "clear" }));
    setRemixAssignments((current) => current[side]?.id === item.id ? { ...current, [side]: null } : current);
  }

  function showGalleryView() {
    if (remixOpen) {
      pageScrollRef.current.remix = window.scrollY;
      restoreMobileGalleryPositionRef.current = true;
    }
    if (isMobile && view === "gallery" && mobilePanel === "detail") {
      restoreMobileGalleryPositionRef.current = true;
    }
    setView("gallery");
    setRemixOpen(false);
    if (isMobile) setMobilePanel("browse");
  }

  function showRemixView() {
    if (!remixOpen && mobilePanel !== "detail") {
      pageScrollRef.current.gallery = window.scrollY;
      const card = [...document.querySelectorAll<HTMLElement>(".itemCard")].find((element) => element.getBoundingClientRect().bottom > mobileChrome.topbarHeight);
      if (card) rememberMobileGalleryPosition(Number(card.dataset.itemId));
    }
    setRemixOpen(true);
    setMobilePanel("browse");
  }

  function setDetailPanelWidth(width: number) {
    const clamped = clampDetailPanelWidth(width);
    setDetailWidth(clamped);
    persistDetailPanelWidth(clamped);
  }

  function startDetailResize(event: ReactPointerEvent<HTMLButtonElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    const startX = event.clientX;
    const startWidth = detailWidth;
    let nextWidth = startWidth;
    setResizingDetail(true);

    const move = (moveEvent: PointerEvent) => {
      nextWidth = clampDetailPanelWidth(startWidth - (moveEvent.clientX - startX));
      setDetailWidth(nextWidth);
    };
    const finish = () => {
      setResizingDetail(false);
      persistDetailPanelWidth(nextWidth);
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", finish);
      window.removeEventListener("pointercancel", finish);
    };

    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", finish);
    window.addEventListener("pointercancel", finish);
  }

  function resizeDetailWithKeyboard(event: ReactKeyboardEvent<HTMLButtonElement>) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      setDetailPanelWidth(detailWidth + DETAIL_PANEL_KEYBOARD_STEP);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      setDetailPanelWidth(detailWidth - DETAIL_PANEL_KEYBOARD_STEP);
    } else if (event.key === "Home") {
      event.preventDefault();
      setDetailPanelWidth(DETAIL_PANEL_MIN_WIDTH);
    } else if (event.key === "End") {
      event.preventDefault();
      setDetailPanelWidth(maxDetailPanelWidth());
    }
  }

  function resetToStart() {
    setQuery("");
    setFilters(emptyFilters);
    setShowHidden(false);
    setSelectedId(null);
    setView("gallery");
    setRemixOpen(false);
    setMobilePanel("browse");
    window.setTimeout(() => window.scrollTo({ top: 0, behavior: "smooth" }), 0);
  }

  if (error) {
    return (
      <main className="errorState">
        <strong>{APP_NAME}</strong>
        <span>{error}</span>
      </main>
    );
  }

  return (
    <main
      className={`appShell ${isMobile ? "isMobileShell" : ""} ${
        isMobile && mobilePanel === "detail" ? "isMobileDetail" : ""
      } ${remixOpen ? "isRemixOpen" : ""}`}
      style={shellStyle}
    >
      <header className="topbar" ref={topbarRef}>
        <button className="brand" type="button" onClick={resetToStart} aria-label="Reset Meme Arsenal">
          <img className="brandMark" src={arsenalMarkIcon} alt="" aria-hidden="true" />
          <div className="brandText">
            <h1>{APP_NAME} <small className="brandVersion">v{version}</small></h1>
            <span className="brandCount">{index ? `${total ?? items.length} items` : "Loading"}</span>
          </div>
        </button>
        <div className="searchCluster">
          <div className="searchBox">
            <Search size={18} />
            <input value={query} onChange={(event) => changeQuery(event.target.value)} placeholder="Search archive" />
          </div>
          <label className="includeHiddenToggle">
            <input type="checkbox" checked={showHidden} onChange={(event) => setShowHidden(event.target.checked)} />
            Include hidden
          </label>
        </div>
      </header>

      <div className={`workspace ${!detailItem ? "isGalleryOnly" : ""} ${resizingDetail ? "is-resizing-detail" : ""}`} style={workspaceStyle}>
        <section className="content">
          <RemixPane
            key={config.remixApiBase}
            open={remixOpen}
            seed={remixSeed}
            apiBase={config.remixApiBase}
            catalogApiBase={config.apiBase}
            onShareChange={handleShareChange}
            visibleItems={visibleItems}
            allItems={allRemixItems}
            contactSheetIsFiltered={contactSheetIsFiltered}
            onSaveTaskChange={handleRemixTaskChange}
            onDeleteSaved={deleteRemix}
            deletedItemId={deletedItemId}
            retrySaveRequest={retrySaveRequest}
            onFocusItem={focusItem}
            onBrowse={showGalleryView}
            onUploadComplete={handleUploadComplete}
            onBusyChange={handleRemixBusyChange}
            onSourcesChange={handleRemixSourcesChange}
            mobileLayout={isMobile}
          />
          {!remixOpen ? <>
          {view === "gallery" && isMobile && mobilePanel === "detail" ? (
            <div className="sectionBar mobileDetailBar">
              <button
                className="secondaryAction mobileBackButton"
                type="button"
                onClick={detailOriginRef.current === "remix" ? showRemixView : showGalleryView}
              >
                <ArrowLeft size={17} />
                {detailOriginRef.current === "remix" ? "Back to remix" : "Back to gallery"}
              </button>
            </div>
          ) : (
            <div className="sectionBar">
              <>
                <span className="sectionSummary">
                  <strong>{total === null ? `${galleryPageItems.length} shown` : `${galleryPageItems.length} of ${mediaCount} shown`}</strong>
                  <span>{loading ? "Loading" : query ? query : "All saved memes"}</span>
                </span>
                {view === "gallery" && isMobile ? (
                  <button
                    className="secondaryAction mobileSurpriseButton"
                    type="button"
                    onClick={chooseRandom}
                    disabled={!canChooseRandom}
                  >
                    <Shuffle size={16} />
                    Surprise
                  </button>
                ) : null}
              </>
            </div>
          )}
          {view === "gallery" ? (
            <>
              {showGalleryPanel ? <>
                  <Gallery
                    items={galleryPageItems}
                    selectedId={detailItem?.id || null}
                    onSelect={(nextId) => {
                      const item = itemById.get(nextId);
                      if (!item) return;
                      focusItemById(nextId);
                    }}
                    remixAssignments={remixAssignments}
                    onClearRemixSide={clearRemixSide}
                    removingId={deletingRemixId}
                    onRemix={startRemixFromDetail}
                    remixBusy={remixBusy}
                  />
                  <LoadMore
                    shown={galleryPageItems.length}
                    total={total}
                    hasMore={hasMore}
                    loading={loading}
                    onLoadMore={loadMore}
                  />
              </> : null}
              {showDetailPanel && isMobile ? (
                <section className="detailPanelWrapper mobileDetailWrapper">
                  <DetailView
                    item={detailItem}
                    apiBase={config.apiBase}
                    onShareChange={handleShareChange}
                    onUpdateItem={upsertItem}
                    remixBusy={remixBusy}
                    showRemixAction={!remixOpen}
                    showPublicShareAction={detailCanPublicShare}
                    publicShare={detailPublicShare}
                    publicShareLoading={detailPublicShareLoading}
                    publicShareBusy={detailPublicShareBusy}
                    publicShareNotice={publicShareNotice}
                    onStartRemix={startRemixFromDetail}
                    onCreatePublicShare={createPublicShare}
                    onPresentPublicShare={sharePublicLink}
                    onRecallPublicShare={recallPublicShare}
                    onRetryClassification={retryClassification}
                    onHide={setItemHidden}
                    onDeleteRemix={deleteRemix}
                    onSaveUpload={saveUploadedItemToArsenal}
                    uploadSaveStatus={detailItem?.id ? uploadSaveStatuses[detailItem.id] : null}
                    onSelectSource={focusItemById}
                  />
                </section>
              ) : null}
            </>
          ) : null}
          </> : null}
        </section>
        {!isMobile && !remixOpen && detailItem ? <button
          className="detailResizeHandle"
          type="button"
          role="separator"
          aria-label="Resize detail panel"
          aria-orientation="vertical"
          aria-valuemin={DETAIL_PANEL_MIN_WIDTH}
          aria-valuemax={maxDetailPanelWidth()}
          aria-valuenow={detailWidth}
          title="Drag to resize detail panel"
          onPointerDown={startDetailResize}
          onKeyDown={resizeDetailWithKeyboard}
          onDoubleClick={() => setDetailPanelWidth(DETAIL_PANEL_DEFAULT_WIDTH)}
        /> : null}
        {!isMobile && !remixOpen && detailItem ? (
          <section className="detailPanelWrapper">
            <DetailView
              item={detailItem}
              apiBase={config.apiBase}
              onShareChange={handleShareChange}
              onUpdateItem={upsertItem}
              remixBusy={remixBusy}
              showRemixAction={!remixOpen}
              showPublicShareAction={detailCanPublicShare}
              publicShare={detailPublicShare}
              publicShareLoading={detailPublicShareLoading}
              publicShareBusy={detailPublicShareBusy}
              publicShareNotice={publicShareNotice}
              onStartRemix={startRemixFromDetail}
              onCreatePublicShare={createPublicShare}
              onPresentPublicShare={sharePublicLink}
              onRecallPublicShare={recallPublicShare}
              onRetryClassification={retryClassification}
              onHide={setItemHidden}
              onDeleteRemix={deleteRemix}
              onSaveUpload={saveUploadedItemToArsenal}
              uploadSaveStatus={detailItem?.id ? uploadSaveStatuses[detailItem.id] : null}
              onSelectSource={focusItemById}
            />
          </section>
        ) : null}
      </div>
      <nav className="mobileActionBar destinationNav" aria-label="Primary destinations" ref={mobileActionBarRef}>
          <button
            type="button"
            className={!remixOpen ? "active" : ""}
            onClick={showGalleryView}
            aria-current={!remixOpen ? "page" : undefined}
            title="Gallery"
          >
            <LayoutGrid size={25} aria-hidden="true" />
            <span className="navText">Gallery</span>
          </button>
          <button type="button" title="Remix" aria-current={remixOpen ? "page" : undefined} className={`remixButton ${remixOpen ? "active remixActive" : "remixActive"}`} onClick={showRemixView}>
            <Sparkles size={25} aria-hidden="true" />
            <span className="navText">Remix</span>
          </button>
      </nav>
      {toast ? <div className={`saveToast ${toast.error ? "is-error" : ""}`} role={toast.error ? "alert" : "status"}>
        {toast.error ? <ShieldAlert size={18} /> : <Check size={18} />}<span>{toast.message}</span>
        {toast.retryJobId ? <button type="button" className="secondaryAction" onClick={() => {
          setRetrySaveRequest((current) => ({ jobId: toast.retryJobId!, key: (current?.key || 0) + 1 }));
          setToast(null);
        }}><RefreshCw size={16} />Retry save</button> : null}
        <button type="button" className="iconButton" aria-label="Dismiss notification" onClick={() => setToast(null)}><X size={16} /></button>
      </div> : null}
    </main>
  );
}


function RecoveryPickers({base, onJob, onDraft}: {base: string; onJob: (id: string) => void; onDraft: (item: CatalogItem) => void}) {
  const [pages, setPages] = useState<Record<string, {rows: Array<{id: string | number; status?: string}>; cursor: string | null}>>({});
  const [error, setError] = useState("");
  async function load(kind: string, more = false) {
    try {
      const cursor = more ? pages[kind]?.cursor : null;
      const response = await fetch(`${base}/${kind}?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`);
      if (!response.ok) throw new Error();
      const payload = await response.json();
      setPages(old => ({...old, [kind]: {rows: [...(more ? old[kind]?.rows || [] : []), ...(payload[kind === "uploads" ? "drafts" : "jobs"] || [])], cursor: payload.next_cursor}}));
      setError("");
    } catch { setError("Could not load retained work"); }
  }
  async function chooseDraft(id: string) {
    const response = await fetch(`${base}/uploads/${encodeURIComponent(id)}`);
    if (response.ok) onDraft(asCatalogItem(await response.json()));
    else setError("Draft unavailable");
  }
  return <div aria-label="Recover retained work">
    {["uploads", "jobs"].map(kind => <span key={kind}>
      <button type="button" onClick={() => void load(kind)}>{kind === "uploads" ? "Uploaded drafts" : "Retained jobs"}</button>
      {pages[kind] && <select aria-label={kind === "uploads" ? "Uploaded drafts" : "Retained jobs"} value="" onChange={event => { if (kind === "jobs") onJob(event.target.value); else void chooseDraft(event.target.value); }}>
        <option value="">Select retained work</option>
        {pages[kind].rows.map(row => <option key={row.id} value={row.id}>{row.id}{row.status ? ` · ${row.status}` : ""}</option>)}
      </select>}
      {pages[kind]?.cursor && <button type="button" onClick={() => void load(kind, true)}>Load older</button>}
    </span>)}
    {error && <span role="status">{error}</span>}
  </div>;
}
