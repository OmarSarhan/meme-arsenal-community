import { useEffect, type RefObject } from "react";

type Surface = "gallery" | "detail";
type EventName = "play" | "playing" | "stalled" | "error" | "fallback";
type Attempt = { id: string; started: number; events: Set<EventName> };
const attempts = new WeakMap<HTMLVideoElement, Attempt>();

export function reportPlaybackDiagnostic(video: HTMLVideoElement, itemId: number, surface: Surface, event: EventName) {
  try {
    sendPlaybackDiagnostic(video, itemId, surface, event);
  } catch {
    // Diagnostics must never interrupt playback on older browsers.
  }
}

function sendPlaybackDiagnostic(video: HTMLVideoElement, itemId: number, surface: Surface, event: EventName) {
  const url = video.currentSrc || video.getAttribute("src") || "";
  // The standalone/offline server has no diagnostic endpoint.
  if (!video.dataset.manualSrc?.startsWith("/api/") && !url.includes("/api/items/")) return;
  let attempt = attempts.get(video);
  if (!attempt || (event === "play" && attempt.events.has("playing"))) {
    attempt = { id: crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(36).slice(2)}`, started: performance.now(), events: new Set() };
    attempts.set(video, attempt);
  }
  if (attempt.events.has(event)) return;
  attempt.events.add(event);
  const payload = {
    attempt: attempt.id, event, surface,
    source: url.includes("/video-previews/") ? "preview" : url.includes("/playback") ? "playback" : "original",
    elapsed_ms: Math.min(86_400_000, Math.round(performance.now() - attempt.started)),
    current_time: Number.isFinite(video.currentTime) ? video.currentTime : 0,
    ready_state: video.readyState, network_state: video.networkState,
    buffered_end: video.buffered.length ? video.buffered.end(video.buffered.length - 1) : 0,
    paused: video.paused, muted: video.muted, error_code: video.error?.code || null,
    h264_aac: video.canPlayType('video/mp4; codecs="avc1.42E01E, mp4a.40.2"'),
  };
  void fetch(`/api/items/${itemId}/playback-events`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload), keepalive: true,
  }).catch(() => {});
}

export function usePlaybackDiagnostics(ref: RefObject<HTMLVideoElement | null>, itemId: number | undefined, surface: Surface) {
  useEffect(() => {
    const video = ref.current;
    if (!video || itemId === undefined) return;
    let timeout: number | undefined;
    const clearStall = () => window.clearTimeout(timeout);
    const play = () => {
      reportPlaybackDiagnostic(video, itemId, surface, "play");
      clearStall();
      timeout = window.setTimeout(() => reportPlaybackDiagnostic(video, itemId, surface, "stalled"), 4000);
    };
    const playing = () => {
      clearStall();
      reportPlaybackDiagnostic(video, itemId, surface, "playing");
    };
    const stalled = () => {
      clearStall();
      if (!video.paused) timeout = window.setTimeout(() => reportPlaybackDiagnostic(video, itemId, surface, "stalled"), 2500);
    };
    const error = () => {
      clearStall();
      reportPlaybackDiagnostic(video, itemId, surface, "error");
    };
    const listeners = { play, playing, waiting: stalled, stalled, error, pause: clearStall, ended: clearStall };
    Object.entries(listeners).forEach(([name, listener]) => video.addEventListener(name, listener));
    return () => {
      clearStall();
      Object.entries(listeners).forEach(([name, listener]) => video.removeEventListener(name, listener));
    };
  }, [ref, itemId, surface]);
}
