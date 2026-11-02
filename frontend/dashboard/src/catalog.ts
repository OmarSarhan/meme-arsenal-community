import { startTransition, useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import type { CatalogIndex, CatalogItem, DashboardConfig, RemixSource, SearchResult, TagsCatalog } from "./types";
import { asCatalogItem, asNullableNumber } from "./api";
import { compareSavedAt, mergeCatalogItems } from "./catalogOrder";

type CatalogCursor = string | number;

function asCatalogCursor(value: unknown): CatalogCursor | null {
  return typeof value === "string" ? value : asNullableNumber(value);
}

export type Filters = {
  mediaType: string;
  topic: string;
  tone: string;
  reactionType: string;
  memeFormat: string;
  language: string;
  favorite: boolean;
  archived: boolean;
};

type StaticCatalogCache = {
  indexPath: string;
  index: CatalogIndex;
  items: CatalogItem[];
  tags: TagsCatalog;
};

type TagsLoadOptions = {
  background?: boolean;
  force?: boolean;
};

const GALLERY_BATCH_SIZE = 96;
const LIVE_GALLERY_INITIAL_BATCH_SIZE = 48;
const LIVE_GALLERY_BACKGROUND_BATCH_SIZE = 64;
const BACKGROUND_GALLERY_LOAD_DELAY_MS = 320;
const BACKGROUND_GALLERY_IDLE_TIMEOUT_MS = 1400;
const HIDDEN_DASHBOARD_MEDIA_TYPES = new Set(["document", "sticker"]);

export function normalizeDisplayText(value: string | null | undefined) {
  return (value || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim()
    .replace(/\s+/g, " ");
}

export function isDashboardVisibleItem(item: CatalogItem) {
  return !HIDDEN_DASHBOARD_MEDIA_TYPES.has(item.media_type);
}

function dashboardVisibleTags(tags: TagsCatalog) {
  return {
    ...tags,
    media_type: (tags.media_type || []).filter((tag) => !HIDDEN_DASHBOARD_MEDIA_TYPES.has(tag.value)),
  };
}

function isAbortError(error: unknown) {
  return error instanceof Error && error.name === "AbortError";
}

async function loadConfig(): Promise<DashboardConfig> {
  try {
    return (await fetch("./arsenal-config.json").then((response) => response.json())) as DashboardConfig;
  } catch {
    return { mode: "online", apiBase: "/api", remixApiBase: "/api/remix" };
  }
}

export function apiUrl(base: string, path: string, params?: Record<string, string | number | boolean | null | undefined>) {
  const trimmed = base.replace(/\/+$/, "");
  const url = new URL(`${trimmed}${path}`, window.location.origin);
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") url.searchParams.set(key, String(value));
  });
  return url.toString();
}

function staticSearchSlug(query: string) {
  return query.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "query";
}

async function loadStaticSearchResults(base: string, query: string): Promise<SearchResult[] | null> {
  const trimmedBase = base.replace(/\/+$/, "");
  try {
    const response = await fetch(`${trimmedBase}/${staticSearchSlug(query)}.json`);
    if (!response.ok) return null;
    const payload = (await response.json()) as { results?: unknown[] };
    if (!Array.isArray(payload.results)) return null;
    return payload.results
      .map((raw) => raw as Partial<SearchResult>)
      .filter((result): result is SearchResult => typeof result.media_item_id === "number" && typeof result.score === "number");
  } catch {
    return null;
  }
}

export function textForItem(item: CatalogItem) {
  return [
    item.caption,
    item.telegram_post_url || "",
    item.source_url || "",
    item.source_display_url || "",
    item.source_site || "",
    item.source_title || "",
    item.source_description || "",
    item.media_type,
    item.mime_type || "",
    item.summary,
    item.visible_text,
    item.narration,
    item.language || "",
    item.meme_format || "",
    ...(item.topics || []),
    ...(item.keywords || []),
    ...(item.tone || []),
    ...(item.reaction_type || []),
    ...(item.people_or_characters || []),
    ...(item.safety_flags || []),
    ...(item.manual_tags || []),
    ...(item.search_phrases || []),
    item.notes || "",
    item.favorite ? "favorite" : "",
    item.archived ? "archived" : "",
  ]
    .join(" ")
    .toLowerCase();
}

function searchTextForItem(item: CatalogItem) {
  return normalizeDisplayText(textForItem(item));
}

function itemMatchesClientQuery(item: CatalogItem, query: string) {
  return !query || searchTextForItem(item).includes(query);
}

function itemMatchesCurrentView(item: CatalogItem, filters: Filters, query: string) {
  return isDashboardVisibleItem(item) && passesFilters(item, filters) && itemMatchesClientQuery(item, query);
}

function applyStaticSearchResults(candidates: CatalogItem[], exactMatches: CatalogItem[], results: SearchResult[] | null) {
  if (!results?.length) return exactMatches;
  const candidatesById = new Map(candidates.map((item) => [item.id, item]));
  const exactIds = new Set(exactMatches.map((item) => item.id));
  const semanticMatches = results
    .map((result) => candidatesById.get(result.media_item_id) || null)
    .filter((item): item is CatalogItem => item !== null && !exactIds.has(item.id));
  return [...exactMatches, ...semanticMatches];
}

async function loadStaticCatalog(indexPath: string): Promise<StaticCatalogCache> {
  const catalogIndex = (await fetch(indexPath).then((response) => response.json())) as CatalogIndex;
  const [chunks, loadedTags] = await Promise.all([
    Promise.all(catalogIndex.chunks.map((path) => fetch(path).then((response) => response.json()))),
    fetch(catalogIndex.tags_path).then((response) => response.json()),
  ]);
  const items = chunks
    .flatMap((chunk) => {
      const rawChunk = chunk as { items?: unknown[] };
      return Array.isArray(rawChunk.items) ? rawChunk.items : [];
    })
    .map(asCatalogItem)
    .filter(isDashboardVisibleItem);
  return {
    indexPath,
    index: catalogIndex,
    items,
    tags: dashboardVisibleTags(loadedTags),
  };
}

function applyStaticCatalogView(
  catalogItems: CatalogItem[],
  filters: Filters,
  searchQuery: string,
  searchResults: SearchResult[] | null,
) {
  const candidates = catalogItems.filter((item) => passesFilters(item, filters));
  const exactMatches = searchQuery ? candidates.filter((item) => itemMatchesClientQuery(item, searchQuery)) : candidates;
  return (searchQuery ? applyStaticSearchResults(candidates, exactMatches, searchResults) : exactMatches)
    .sort(compareSavedAt);
}

function scheduleBackgroundGalleryLoad(callback: () => void) {
  const idleWindow = window as Window & {
    requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
    cancelIdleCallback?: (handle: number) => void;
  };
  if (idleWindow.requestIdleCallback && idleWindow.cancelIdleCallback) {
    const handle = idleWindow.requestIdleCallback(callback, { timeout: BACKGROUND_GALLERY_IDLE_TIMEOUT_MS });
    return () => idleWindow.cancelIdleCallback?.(handle);
  }
  const timeoutId = window.setTimeout(callback, BACKGROUND_GALLERY_LOAD_DELAY_MS);
  return () => window.clearTimeout(timeoutId);
}

export function apiItemParams(filters: Filters, query: string, cursor: CatalogCursor | null = null, limit = LIVE_GALLERY_BACKGROUND_BATCH_SIZE) {
  return {
    limit,
    cursor,
    q: query,
    media_type: filters.mediaType !== "all" ? filters.mediaType : undefined,
    topic: filters.topic !== "all" ? filters.topic : undefined,
    tone: filters.tone !== "all" ? filters.tone : undefined,
    reaction_type: filters.reactionType !== "all" ? filters.reactionType : undefined,
    meme_format: filters.memeFormat !== "all" ? filters.memeFormat : undefined,
    language: filters.language !== "all" ? filters.language : undefined,
    favorite: filters.favorite || undefined,
    include_hidden: filters.archived || undefined,
  };
}

export function useCatalog(filters: Filters, searchQuery: string) {
  const [items, setItems] = useState<CatalogItem[]>([]);
  const [matchedItems, setMatchedItems] = useState<CatalogItem[]>([]);
  const [tags, setTags] = useState<TagsCatalog>({});
  const [index, setIndex] = useState<CatalogIndex | null>(null);
  const [config, setConfig] = useState<DashboardConfig>({ mode: "online", apiBase: "/api", remixApiBase: "/api/remix" });
  const [configReady, setConfigReady] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nextCursor, setNextCursor] = useState<CatalogCursor | null>(null);
  const [total, setTotal] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const requestIdRef = useRef(0);
  const liveStateSignatureRef = useRef<string | null>(null);
  const loadMoreCursorRef = useRef<CatalogCursor | null>(null);
  const backgroundPageLoadedRef = useRef(false);
  const tagsLoadedRef = useRef(false);
  const tagsLoadingRef = useRef(false);
  const tagsRequestIdRef = useRef(0);
  const itemRequestControllersRef = useRef<Set<AbortController>>(new Set());
  const staticCatalogCacheRef = useRef<StaticCatalogCache | null>(null);
  const staticSearchResultsRef = useRef<Map<string, SearchResult[] | null>>(new Map());
  const itemsRef = useRef(items);
  const galleryAnchorRef = useRef<{ id: string; top: number } | null>(null);
  const viewGenerationRef = useRef(0);

  useLayoutEffect(() => {
    itemsRef.current = items;
    const anchor = galleryAnchorRef.current;
    galleryAnchorRef.current = null;
    if (!anchor) return;
    const card = document.querySelector<HTMLElement>(`.itemCard[data-item-id="${anchor.id}"]`);
    if (card) window.scrollBy({ top: card.getBoundingClientRect().top - anchor.top, behavior: "auto" });
  }, [items]);

  useEffect(() => {
    let cancelled = false;
    async function run() {
      try {
        const config = await loadConfig();
        if (cancelled) return;
        setConfig(config);
        setConfigReady(true);
        if (!config.apiBase && !config.catalogIndex) {
          throw new Error("Dashboard config must provide apiBase for live mode or catalogIndex for static mode.");
        }
      } catch (catalogError) {
        if (!cancelled) setError(catalogError instanceof Error ? catalogError.message : String(catalogError));
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    tagsLoadedRef.current = false;
    tagsLoadingRef.current = false;
    tagsRequestIdRef.current += 1;
    if (config.apiBase) setTags({});
  }, [config.apiBase]);

  const loadApiTags = useCallback(async (options: TagsLoadOptions = {}) => {
    const apiBase = config.apiBase;
    if (!apiBase) return;
    if (!options.force && (tagsLoadedRef.current || tagsLoadingRef.current)) return;
    const requestId = ++tagsRequestIdRef.current;
    tagsLoadingRef.current = true;
    try {
      const response = await fetch(apiUrl(apiBase, "/tags"));
      if (!response.ok) throw new Error("Could not load tags");
      const loadedTags = await response.json() as TagsCatalog;
      if (requestId !== tagsRequestIdRef.current) return;
      tagsLoadedRef.current = true;
      const applyTags = () => setTags(dashboardVisibleTags(loadedTags));
      if (options.background) {
        startTransition(applyTags);
      } else {
        applyTags();
      }
    } catch {
      // The gallery should remain usable if tag metadata is slow or unavailable.
    } finally {
      if (requestId === tagsRequestIdRef.current) {
        tagsLoadingRef.current = false;
      }
    }
  }, [config.apiBase]);

  useEffect(() => {
    return () => {
      itemRequestControllersRef.current.forEach((controller) => controller.abort());
      itemRequestControllersRef.current.clear();
    };
  }, []);

  const loadApiPage = useCallback(async (
    cursor: CatalogCursor | null,
    append: boolean,
    options: { background?: boolean; limit?: number } = {},
  ) => {
    const apiBase = config.apiBase;
    if (!apiBase) return;
    if (append && loadMoreCursorRef.current === cursor) return;
    const background = Boolean(options.background);
    const pageLimit = options.limit ?? (append ? LIVE_GALLERY_BACKGROUND_BATCH_SIZE : LIVE_GALLERY_INITIAL_BATCH_SIZE);
    loadMoreCursorRef.current = append ? cursor : null;
    const requestId = ++requestIdRef.current;
    if (!append) {
      viewGenerationRef.current += 1;
      itemRequestControllersRef.current.forEach((controller) => controller.abort());
      itemRequestControllersRef.current.clear();
      backgroundPageLoadedRef.current = false;
    }
    const controller = new AbortController();
    itemRequestControllersRef.current.add(controller);
    if (!background) setLoading(true);
    if (!append) {
      setNextCursor(null);
    }
    try {
      const response = await fetch(apiUrl(apiBase, "/items", apiItemParams(filters, searchQuery, cursor, pageLimit)), {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error(`Failed to load items (${response.status})`);
      const page = await response.json();
      if (requestId !== requestIdRef.current) return;
      const pagePayload = page as { items?: unknown[]; next_cursor?: unknown; total?: unknown };
      const rawItems = Array.isArray(pagePayload.items) ? pagePayload.items : [];
      const pageItems = (rawItems as unknown[]).map(asCatalogItem).filter(isDashboardVisibleItem).sort(compareSavedAt);
      const applyPage = () => {
        if (append) {
          setItems((current) => mergeCatalogItems(current, pageItems));
          setMatchedItems((current) => mergeCatalogItems(current, pageItems));
        } else {
          setItems(pageItems);
          setMatchedItems(pageItems);
        }
        setNextCursor(asCatalogCursor(pagePayload.next_cursor));
        setTotal(asNullableNumber(pagePayload.total));
        setIndex({
          version: 2,
          generated_at: new Date().toISOString(),
          item_count: asNullableNumber(pagePayload.total) ?? pageItems.length,
          chunk_size: pageLimit,
          chunks: [],
          tags_path: apiUrl(apiBase, "/tags"),
          search_index_path: "",
          search_results_base: "",
        });
        setError(null);
      };
      if (background) {
        startTransition(applyPage);
      } else {
        applyPage();
      }
    } catch (catalogError) {
      if (isAbortError(catalogError)) return;
      if (!background && requestId === requestIdRef.current) {
        setError(catalogError instanceof Error ? catalogError.message : String(catalogError));
      }
    } finally {
      const aborted = controller.signal.aborted;
      itemRequestControllersRef.current.delete(controller);
      if (append && loadMoreCursorRef.current === cursor) loadMoreCursorRef.current = null;
      if (!aborted && !background && requestId === requestIdRef.current) setLoading(false);
    }
  }, [config.apiBase, filters, searchQuery]);

  const refreshApiPage = useCallback(async () => {
    if (!config.apiBase || loadMoreCursorRef.current !== null) return false;
    const generation = viewGenerationRef.current;
    const newestLoadedItem = itemsRef.current[0];
    const controller = new AbortController();
    itemRequestControllersRef.current.add(controller);
    const incoming: CatalogItem[] = [];
    let cursor: CatalogCursor | null = null;
    let reachedLoadedItems = false;
    let refreshedTotal: number | null = null;
    try {
      // Follow new arrivals as far as the loaded head, without resetting older pages.
      do {
        const response = await fetch(apiUrl(config.apiBase, "/items", apiItemParams(filters, searchQuery, cursor)), {
          signal: controller.signal,
          cache: "no-store",
        });
        if (!response.ok) throw new Error("Could not refresh gallery");
        const page = await response.json();
        const pageItems: CatalogItem[] = (page.items || []).map(asCatalogItem);
        incoming.push(...pageItems.filter(isDashboardVisibleItem));
        reachedLoadedItems = !newestLoadedItem || pageItems.some((item) => compareSavedAt(item, newestLoadedItem) >= 0);
        cursor = asCatalogCursor(page.next_cursor);
        refreshedTotal = asNullableNumber(page.total);
      } while (cursor !== null && !reachedLoadedItems);
      if (controller.signal.aborted || generation !== viewGenerationRef.current) return false;
      const mergeRefresh = (current: CatalogItem[]) => mergeCatalogItems(current, incoming);
      if (window.scrollY > 0) {
        const anchor = Array.from(document.querySelectorAll<HTMLElement>(".itemCard[data-item-id]"))
          .find((card) => card.getBoundingClientRect().bottom > 0);
        if (anchor) galleryAnchorRef.current = { id: anchor.dataset.itemId!, top: anchor.getBoundingClientRect().top };
      }
      setItems(mergeRefresh);
      setMatchedItems(mergeRefresh);
      setTotal(refreshedTotal);
      if (!newestLoadedItem) setNextCursor(cursor);
      return true;
    } finally {
      itemRequestControllersRef.current.delete(controller);
    }
  }, [config.apiBase, filters, searchQuery]);

  useEffect(() => {
    if (!configReady || !config.apiBase) return;
    void loadApiPage(null, false, { limit: LIVE_GALLERY_INITIAL_BATCH_SIZE });
  }, [configReady, config.apiBase, loadApiPage]);

  useEffect(() => {
    if (!configReady || !config.catalogIndex || config.apiBase) return;
    let cancelled = false;
    async function run() {
      const indexPath = config.catalogIndex || "";
      const cachedCatalog =
        staticCatalogCacheRef.current?.indexPath === indexPath ? staticCatalogCacheRef.current : null;
      setLoading(!cachedCatalog);
      try {
        const catalog = cachedCatalog || (await loadStaticCatalog(indexPath));
        if (!cachedCatalog) {
          staticCatalogCacheRef.current = catalog;
          staticSearchResultsRef.current.clear();
        }
        const searchKey =
          searchQuery && catalog.index.search_results_base
            ? `${catalog.index.search_results_base.replace(/\/+$/, "")}/${staticSearchSlug(searchQuery)}`
            : "";
        let searchResults: SearchResult[] | null = null;
        if (searchKey) {
          if (staticSearchResultsRef.current.has(searchKey)) {
            searchResults = staticSearchResultsRef.current.get(searchKey) || null;
          } else {
            searchResults = await loadStaticSearchResults(catalog.index.search_results_base, searchQuery);
            staticSearchResultsRef.current.set(searchKey, searchResults);
          }
        }
        if (cancelled) return;
        const visibleItems = applyStaticCatalogView(catalog.items, filters, searchQuery, searchResults);
        const pageItems = visibleItems.slice(0, GALLERY_BATCH_SIZE);
        setIndex(catalog.index);
        setMatchedItems(visibleItems);
        setItems(pageItems);
        setTotal(visibleItems.length);
        setNextCursor(visibleItems.length > pageItems.length ? pageItems.length : null);
        backgroundPageLoadedRef.current = false;
        tagsLoadedRef.current = true;
        setTags(catalog.tags);
        setError(null);
      } catch (catalogError) {
        if (!cancelled) setError(catalogError instanceof Error ? catalogError.message : String(catalogError));
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    run();
    return () => {
      cancelled = true;
    };
  }, [configReady, config.apiBase, config.catalogIndex, filters, searchQuery]);

  useEffect(() => {
    if (!configReady || !config.apiBase) return;
    let cancelled = false;
    let refreshing = false;
    async function refreshIfChanged() {
      if (refreshing || document.hidden) return;
      refreshing = true;
      try {
        const response = await fetch(apiUrl(config.apiBase || "/api", "/catalog-state"));
        if (!response.ok) return;
        const state = await response.json();
        if (cancelled) return;
        const signature = `${state.item_count || 0}:${state.latest_item_id || ""}:${state.updated_at || ""}`;
        if (!liveStateSignatureRef.current) {
          liveStateSignatureRef.current = signature;
          return;
        }
        if (signature !== liveStateSignatureRef.current) {
          if (!(await refreshApiPage()) || cancelled) return;
          liveStateSignatureRef.current = signature;
          const tagsWereLoaded = tagsLoadedRef.current;
          tagsLoadedRef.current = false;
          if (tagsWereLoaded) void loadApiTags({ background: true, force: true });
        }
      } catch {
        // The next poll or user action will retry.
      } finally {
        refreshing = false;
      }
    }
    void refreshIfChanged();
    const intervalId = window.setInterval(refreshIfChanged, 20000);
    return () => {
      cancelled = true;
      window.clearInterval(intervalId);
    };
  }, [configReady, config.apiBase, refreshApiPage, loadApiTags]);

  const appendStaticPage = useCallback((cursor: number, background = false) => {
    const nextItems = matchedItems.slice(cursor, cursor + GALLERY_BATCH_SIZE);
    const nextOffset = cursor + nextItems.length;
    if (!nextItems.length) {
      setNextCursor(null);
      return;
    }
    const applyPage = () => {
      setItems((current) => mergeCatalogItems(current, nextItems));
      setNextCursor(nextOffset < matchedItems.length ? nextOffset : null);
    };
    if (background) {
      startTransition(applyPage);
    } else {
      applyPage();
    }
  }, [matchedItems]);

  const loadMore = useCallback(() => {
    if (loading || nextCursor === null) return;
    if (config.apiBase) {
      void loadApiPage(nextCursor, true);
      return;
    }
    if (!config.catalogIndex || typeof nextCursor !== "number") return;
    appendStaticPage(nextCursor);
  }, [appendStaticPage, config.apiBase, config.catalogIndex, loadApiPage, loading, nextCursor]);

  useEffect(() => {
    if (!configReady || loading || nextCursor === null || !items.length || backgroundPageLoadedRef.current) return;
    backgroundPageLoadedRef.current = true;
    return scheduleBackgroundGalleryLoad(() => {
      if (config.apiBase) {
        void loadApiPage(nextCursor, true, { background: true, limit: LIVE_GALLERY_BACKGROUND_BATCH_SIZE });
        return;
      }
      if (config.catalogIndex && typeof nextCursor === "number") appendStaticPage(nextCursor, true);
    });
  }, [
    appendStaticPage,
    config.apiBase,
    config.catalogIndex,
    configReady,
    items.length,
    loadApiPage,
    loading,
    nextCursor,
  ]);

  function upsertItem(item: CatalogItem) {
    if (!itemMatchesCurrentView(item, filters, searchQuery)) {
      if (items.some((candidate) => candidate.id === item.id)) removeItem(item.id);
      return;
    }
    const sourceIds = new Set((item.remix_sources || []).map((source) => source.id));
    const usedByTrace = tracePayloadFromItem(item);
    setItems((current) =>
      mergeCatalogItems(current, [item]).map((candidate) => {
        if (!sourceIds.has(candidate.id)) return candidate;
        const existing = candidate.remix_used_by || [];
        return {
          ...candidate,
          remix_used_by: [usedByTrace, ...existing.filter((usedBy) => usedBy.id !== item.id)],
        };
      }),
    );
    setMatchedItems((current) =>
      mergeCatalogItems(current, [item]).map((candidate) => {
        if (!sourceIds.has(candidate.id)) return candidate;
        const existing = candidate.remix_used_by || [];
        return {
          ...candidate,
          remix_used_by: [usedByTrace, ...existing.filter((usedBy) => usedBy.id !== item.id)],
        };
      }),
    );
    setTotal((current) => (current === null ? current : current + (items.some((candidate) => candidate.id === item.id) ? 0 : 1)));
  }

  function removeItem(itemId: number) {
    setItems((current) => current.filter((item) => item.id !== itemId));
    setMatchedItems((current) => current.filter((item) => item.id !== itemId));
    setTotal((current) => current === null ? current : Math.max(0, current - 1));
  }

  return {
    items,
    matchedItems,
    tags,
    index,
    config,
    error,
    total,
    hasMore: nextCursor !== null,
    loading,
    loadMore,
    ensureTagsLoaded: loadApiTags,
    upsertItem,
    removeItem,
  };
}

function passesFilters(item: CatalogItem, filters: Filters) {
  return (
    (filters.mediaType === "all" || item.media_type === filters.mediaType) &&
    (filters.topic === "all" || item.topics.includes(filters.topic)) &&
    (filters.tone === "all" || item.tone.includes(filters.tone)) &&
    (filters.reactionType === "all" || item.reaction_type.includes(filters.reactionType)) &&
    (filters.memeFormat === "all" || item.meme_format === filters.memeFormat) &&
    (filters.language === "all" || item.language === filters.language) &&
    (!filters.favorite || item.favorite) &&
    (filters.archived || !item.archived)
  );
}

export function tracePayloadFromItem(item: CatalogItem): RemixSource {
  return {
    id: item.id,
    title: item.summary || item.caption || item.source_title || `Media ${item.id}`,
    media_type: item.media_type,
    thumbnail_path: item.thumbnail_path,
    preview_path: item.preview_path,
    original_path: item.original_path,
  };
}
