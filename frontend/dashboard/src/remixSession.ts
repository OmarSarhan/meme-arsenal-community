import { useCallback, useEffect, useRef, useState } from "react";
import { asCatalogItem, asRemixJob } from "./api";
import type { CatalogItem, RemixJob, RemixTweakAnalysis } from "./types";

type PromptLevel = 0 | 1 | 2 | 3;
type Workspace = {
  left: CatalogItem | null;
  right: CatalogItem | null;
  jobId: string | null;
  dismissed: string[];
  tweakAnalysis: RemixTweakAnalysis | null;
  tweakValues: Record<string, number>;
  schizoLevel: PromptLevel;
  chronicallyOnlineLevel: PromptLevel;
  noText: boolean;
  comicStrip: boolean;
  antiSlop: boolean;
  saferMode: boolean;
};

function storageKey(base: string) {
  return `meme-arsenal.remix.workspace.v1:${base}`;
}

function readWorkspace(base: string): Partial<Workspace> {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(storageKey(base)) || "null");
    if (!value || typeof value !== "object") return {};
    return {
      ...value,
      left: value.left ? asCatalogItem(value.left) : null,
      right: value.right ? asCatalogItem(value.right) : null,
      jobId: typeof value.jobId === "string" ? value.jobId : null,
      dismissed: Array.isArray(value.dismissed) ? value.dismissed.filter((id: unknown) => typeof id === "string") : [],
      tweakAnalysis: Array.isArray(value.tweakAnalysis?.controls) ? value.tweakAnalysis : null,
      tweakValues: value.tweakValues && typeof value.tweakValues === "object" ? value.tweakValues : {},
    };
  } catch {
    return {};
  }
}

export function readRemixOpen() {
  try {
    return window.sessionStorage.getItem("meme-arsenal.remix.open") === "true";
  } catch {
    return false;
  }
}

export function persistRemixOpen(open: boolean) {
  try {
    window.sessionStorage.setItem("meme-arsenal.remix.open", String(open));
  } catch {
    // Storage may be unavailable in private browsing.
  }
}

export function useRemixSession(base: string) {
  const [selection, setSelection] = useState<{ id: string } | null>(null);
  const selectJob = useCallback((id: string) => setSelection({ id }), []);
  const [initial] = useState(() => readWorkspace(base));
  const [left, setLeft] = useState(initial.left || null);
  const [right, setRight] = useState(initial.right || null);
  const [job, setJob] = useState<RemixJob | null>(null);
  const [recovering, setRecovering] = useState(Boolean(initial.jobId) || base === "/api/remix");
  const [recoveryError, setRecoveryError] = useState<string | null>(null);
  const [tweakAnalysis, setTweakAnalysis] = useState<RemixTweakAnalysis | null>(initial.tweakAnalysis || null);
  const [tweakValues, setTweakValues] = useState(initial.tweakValues || {});
  const [schizoLevel, setSchizoLevel] = useState<PromptLevel>(initial.schizoLevel || 0);
  const [chronicallyOnlineLevel, setChronicallyOnlineLevel] = useState<PromptLevel>(initial.chronicallyOnlineLevel || 0);
  const [noText, setNoText] = useState(Boolean(initial.noText));
  const [comicStrip, setComicStrip] = useState(Boolean(initial.comicStrip));
  const [antiSlop, setAntiSlop] = useState(Boolean(initial.antiSlop));
  const [saferMode, setSaferMode] = useState(Boolean(initial.saferMode));
  const dismissedRef = useRef(initial.dismissed || []);

  useEffect(() => {
    const controller = new AbortController();
    setRecovering(true);
    let retry: number | undefined;
    async function recover() {
      try {
        let jobId = selection?.id || initial.jobId;
        if (!jobId && base === "/api/remix") {
          let cursor: string | null = null;
          do {
            const response = await fetch(`${base}/jobs?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`, { signal: controller.signal });
            if (!response.ok) throw new Error("Could not reconnect to your remixes");
            const payload = await response.json();
            const candidate = (payload.jobs || []).map(asRemixJob).find((candidate: RemixJob) =>
              ["queued", "preparing_media", "calling_gemini", "ready", "saving"].includes(candidate.status)
              && !dismissedRef.current.includes(candidate.id));
            jobId = candidate?.id;
            cursor = payload.next_cursor;
          } while (!jobId && cursor);
        }
        if (!jobId && base === "/api/remix") {
          setLeft(null);
          setRight(null);
        }
        if (jobId) {
          const response = await fetch(`${base}/jobs/${encodeURIComponent(jobId)}?include_sources=true`, {
            signal: controller.signal,
          });
          if (response.status === 404) {
            setJob(null);
            setLeft(null);
            setRight(null);
          } else {
            if (!response.ok) throw new Error("Could not reconnect to your remix");
            const payload = await response.json();
            const restored = asRemixJob(payload);
            // Only the local backend omits sources. Online responses remain
            // authoritative, including empty/denied slots.
            const sources: CatalogItem[] = base === "../../api/remix" && payload.source_items === undefined
              ? [initial.left, initial.right].filter((item): item is CatalogItem => Boolean(item))
              : (payload.source_items || []).map(asCatalogItem);
            const sourceFor = (id: number) => sources.find((item) => item.id === id) || null;
            setLeft(sourceFor(Number(restored.left_id)));
            setRight(sourceFor(Number(restored.right_id)));
            setTweakAnalysis(null);
            setTweakValues({});
            setSchizoLevel(0);
            setChronicallyOnlineLevel(0);
            setNoText(false);
            setComicStrip(false);
            setAntiSlop(false);
            setSaferMode(restored.safety_mode === "safer");
            if (restored.tweaks) {
              const tweaks = restored.tweaks;
              setTweakAnalysis({ prompt_version: tweaks.analysis_prompt_version, summary: tweaks.summary, controls: tweaks.controls });
              setTweakValues(tweaks.values);
              setSchizoLevel(Math.max(0, Math.min(3, Math.round(tweaks.schizo_level || 0))) as PromptLevel);
              setChronicallyOnlineLevel(Math.max(0, Math.min(3, Math.round(tweaks.chronically_online_level || 0))) as PromptLevel);
              setNoText(Boolean(tweaks.no_text));
              setComicStrip(Boolean(tweaks.comic_strip));
              setAntiSlop(Boolean(tweaks.anti_slop));
              setSaferMode(restored.safety_mode === "safer");
            }
            setJob(restored);
          }
        }
        if (!controller.signal.aborted) {
          setRecovering(false);
          setRecoveryError(null);
        }
      } catch {
        if (controller.signal.aborted) return;
        setRecoveryError("Reconnecting to your remix");
        retry = window.setTimeout(recover, 3000);
      }
    }
    void recover();
    return () => {
      controller.abort();
      window.clearTimeout(retry);
    };
  }, [base, initial, selection]);

  useEffect(() => {
    if (recovering) return;
    const workspace: Workspace = {
      left, right,
      jobId: job && !["pending", "failed"].includes(job.id) ? job.id : null,
      dismissed: dismissedRef.current,
      tweakAnalysis, tweakValues, schizoLevel, chronicallyOnlineLevel,
      noText, comicStrip, antiSlop, saferMode,
    };
    try {
      window.sessionStorage.setItem(storageKey(base), JSON.stringify(workspace));
    } catch {
      // Server job discovery still recovers unfinished work when storage is unavailable.
    }
  }, [base, recovering, left, right, job, tweakAnalysis, tweakValues, schizoLevel, chronicallyOnlineLevel,
    noText, comicStrip, antiSlop, saferMode]);

  const clearJob = useCallback(() => {
    if (job && !["pending", "failed"].includes(job.id)) {
      dismissedRef.current = [...dismissedRef.current.filter((id) => id !== job.id), job.id].slice(-40);
    }
    setJob(null);
  }, [job]);

  return {
    left, setLeft, right, setRight, job, setJob, clearJob, recovering, recoveryError, selectJob,
    tweakAnalysis, setTweakAnalysis, tweakValues, setTweakValues, schizoLevel, setSchizoLevel,
    chronicallyOnlineLevel, setChronicallyOnlineLevel, noText, setNoText, comicStrip, setComicStrip,
    antiSlop, setAntiSlop, saferMode, setSaferMode,
  };
}
