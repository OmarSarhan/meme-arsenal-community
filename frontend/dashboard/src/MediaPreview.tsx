import { Image as ImageIcon, Play } from "lucide-react";
import { useEffect, useRef, useState, type SyntheticEvent as ReactSyntheticEvent } from "react";
import type { CatalogItem } from "./types";
import { claimMediaPlayback, releaseMediaPlayback, formatDuration } from "./mediaPlayback";
import { reportPlaybackDiagnostic, usePlaybackDiagnostics } from "./playbackDiagnostics";

const VIDEO_PREVIEW_START_TIMEOUT_MS = 5000;
const VIDEO_PREVIEW_STALL_TIMEOUT_MS = 3000;

export function MediaPreview({
  item,
  compact = false,
  priority = false,
  fetchPriority = false,
}: {
  item: CatalogItem;
  compact?: boolean;
  priority?: boolean;
  fetchPriority?: boolean;
}) {
  const [previewMode, setPreviewMode] = useState<"hover" | "manual" | "fallback" | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const hoverVideoRef = useRef<HTMLVideoElement | null>(null);
  const ignoreMousePreviewUntilRef = useRef(0);
  const manualPlaybackTimeoutRef = useRef<number | null>(null);
  const manualPlaybackActiveRef = useRef(false);
  const manualPlaybackStartedRef = useRef(false);
  const previewIdRef = useRef(`${item.id}-${Math.random().toString(36).slice(2)}`);
  const isVideo = item.media_type === "video";
  const posterSource = item.thumbnail_path || item.preview_path || "";
  const source = posterSource || item.original_path || "";
  const duration = formatDuration(item.duration_seconds);
  const hoverSource = isVideo ? item.hover_preview_path || item.original_path || "" : "";
  const manualSource = isVideo ? item.playback_path || item.original_path || hoverSource : "";
  const imageLoading = priority ? "eager" : "lazy";
  const previewing = previewMode !== null;
  const manualPreviewing = previewMode === "manual";
  usePlaybackDiagnostics(hoverVideoRef, item.id, "gallery");

  useEffect(
    () => () => {
      manualPlaybackActiveRef.current = false;
      manualPlaybackStartedRef.current = false;
      if (manualPlaybackTimeoutRef.current !== null) window.clearTimeout(manualPlaybackTimeoutRef.current);
      releaseMediaPlayback(previewIdRef.current);
    },
    [],
  );

  function clearManualPlaybackTimeout() {
    if (manualPlaybackTimeoutRef.current === null) return;
    window.clearTimeout(manualPlaybackTimeoutRef.current);
    manualPlaybackTimeoutRef.current = null;
  }

  function stopManualPlaybackAttempt() {
    manualPlaybackActiveRef.current = false;
    manualPlaybackStartedRef.current = false;
    clearManualPlaybackTimeout();
  }

  function failPlayback(error: unknown, visible: boolean) {
    console.warn(`Video preview ${item.id} could not play`, error);
    stopManualPlaybackAttempt();
    releaseMediaPlayback(previewIdRef.current);
    setPreviewMode(null);
    if (visible) setPlaybackError("This video could not play. Tap play to retry.");
  }

  function showAnimatedFallback(error: unknown) {
    if (!manualPlaybackActiveRef.current) return;
    if (hoverVideoRef.current) reportPlaybackDiagnostic(hoverVideoRef.current, item.id, "gallery", "fallback");
    console.warn(`Video preview ${item.id} stalled; showing animated preview`, error);
    stopManualPlaybackAttempt();
    releaseMediaPlayback(previewIdRef.current);
    if (!hoverSource || hoverSource === manualSource) {
      setPreviewMode(null);
      setPlaybackError("This video could not play. Tap play to retry.");
      return;
    }
    setPlaybackError(null);
    setPreviewMode("fallback");
  }

  function armManualPlaybackTimeout(delay: number, reason: string) {
    clearManualPlaybackTimeout();
    manualPlaybackTimeoutRef.current = window.setTimeout(() => {
      manualPlaybackTimeoutRef.current = null;
      showAnimatedFallback(new Error(reason));
    }, delay);
  }

  useEffect(() => {
    const video = hoverVideoRef.current;
    if (!video || !hoverSource) return;
    if (!previewMode) {
      stopManualPlaybackAttempt();
      releaseMediaPlayback(previewIdRef.current);
      video.pause();
      try {
        video.currentTime = 0;
      } catch {
        // Some browsers disallow seeking before metadata is available.
      }
      video.removeAttribute("src");
      video.autoplay = false;
      video.preload = "none";
      video.controls = false;
      video.load();
      return;
    }
    if (previewMode === "manual") return;

    let cancelled = false;
    const animatedFallback = previewMode === "fallback";
    const playPreview = () => {
      if (cancelled) return;
      try {
        video.currentTime = 0;
      } catch {
        // Playback can still start even if the reset seek is not available yet.
      }
      claimMediaPlayback(video, previewIdRef.current, () => setPreviewMode(null));
      void video.play().catch((error) => failPlayback(error, animatedFallback));
    };

    if (video.getAttribute("src") !== hoverSource) video.setAttribute("src", hoverSource);
    video.autoplay = animatedFallback;
    video.preload = animatedFallback ? "auto" : "metadata";
    video.muted = true;
    video.loop = true;
    video.playsInline = true;
    if (video.readyState >= 2) {
      playPreview();
    } else {
      video.load();
      video.addEventListener("loadeddata", playPreview, { once: true });
    }

    return () => {
      cancelled = true;
      video.removeEventListener("loadeddata", playPreview);
    };
  }, [hoverSource, previewMode]);

  function startHoverPreview() {
    if (hoverSource) setPreviewMode((current) => (current === "manual" ? current : "hover"));
  }

  function stopHoverPreview() {
    setPreviewMode((current) => (current === "hover" ? null : current));
  }

  function suppressTouchPreview() {
    ignoreMousePreviewUntilRef.current = Date.now() + 1000;
    stopHoverPreview();
  }

  function startMousePreview() {
    if (Date.now() < ignoreMousePreviewUntilRef.current) return;
    startHoverPreview();
  }

  function toggleManualPreview(event: ReactSyntheticEvent<HTMLElement>) {
    if (!manualSource) return;
    event.preventDefault();
    event.stopPropagation();
    if (previewMode === "manual") {
      stopManualPlaybackAttempt();
      releaseMediaPlayback(previewIdRef.current);
      setPreviewMode(null);
      return;
    }
    const video = hoverVideoRef.current;
    if (video) {
      setPlaybackError(null);
      setPreviewMode("manual");
      manualPlaybackActiveRef.current = true;
      manualPlaybackStartedRef.current = false;
      claimMediaPlayback(video, previewIdRef.current, () => setPreviewMode(null));
      video.pause();
      if (video.getAttribute("src") !== manualSource) video.setAttribute("src", manualSource);
      video.autoplay = true;
      video.preload = "auto";
      video.defaultMuted = false;
      video.muted = false;
      video.loop = false;
      video.controls = true;
      video.load();
      video.muted = false;
      armManualPlaybackTimeout(VIDEO_PREVIEW_START_TIMEOUT_MS, "Playback did not start in time");
      void video.play().catch(showAnimatedFallback);
    }
  }

  function keepManualPreviewTap(event: ReactSyntheticEvent<HTMLElement>) {
    if (!hoverSource) return;
    event.stopPropagation();
  }

  if (!source) {
    return (
      <span className={`mediaPreview missingPreview ${compact ? "compact" : ""}`}>
        <ImageIcon size={28} />
        <span className="previewBadges">
          <span className="typeBadge">{item.media_type}</span>
        </span>
      </span>
    );
  }
  return (
    <span
      className={`mediaPreview ${compact ? "compact" : ""} ${isVideo && hoverSource ? "videoPreview" : ""} ${previewing ? "is-previewing" : ""} ${manualPreviewing ? "is-manual-previewing" : ""}`}
      onPointerEnter={(event) => {
        if (event.pointerType === "touch") {
          suppressTouchPreview();
          return;
        }
        startMousePreview();
      }}
      onPointerLeave={stopHoverPreview}
      onPointerCancel={stopHoverPreview}
      onTouchStart={suppressTouchPreview}
      onMouseEnter={startMousePreview}
      onMouseLeave={stopHoverPreview}
    >
      <img
        className="previewBackdrop"
        src={source}
        loading={imageLoading}
        decoding="async"
        draggable={false}
        alt=""
        aria-hidden="true"
      />
      <img
        className="previewImage"
        src={source}
        loading={imageLoading}
        decoding="async"
        fetchPriority={fetchPriority ? "high" : undefined}
        draggable={false}
        alt=""
      />
      {hoverSource ? (
        <video
          ref={hoverVideoRef}
          className="hoverVideo"
          poster={previewing ? source : undefined}
          data-preview-src={hoverSource}
          data-manual-src={manualSource}
          muted={!manualPreviewing}
          loop={!manualPreviewing}
          controls={manualPreviewing}
          playsInline
          preload="none"
          disablePictureInPicture
          disableRemotePlayback
          controlsList="nodownload noremoteplayback"
          aria-hidden={!manualPreviewing}
          onClick={keepManualPreviewTap}
          onPointerDown={keepManualPreviewTap}
          onTouchStart={keepManualPreviewTap}
          onPlay={(event) => {
            setPlaybackError(null);
            if (manualPreviewing) {
              manualPlaybackActiveRef.current = true;
              armManualPlaybackTimeout(VIDEO_PREVIEW_START_TIMEOUT_MS, "Playback did not begin after play");
            }
            claimMediaPlayback(event.currentTarget, previewIdRef.current, () => setPreviewMode(null));
          }}
          onPlaying={() => {
            manualPlaybackStartedRef.current = true;
            clearManualPlaybackTimeout();
          }}
          onTimeUpdate={(event) => {
            if (manualPreviewing && event.currentTarget.currentTime > 0) {
              manualPlaybackStartedRef.current = true;
              clearManualPlaybackTimeout();
            }
          }}
          onWaiting={() => {
            if (manualPreviewing && manualPlaybackActiveRef.current) {
              armManualPlaybackTimeout(VIDEO_PREVIEW_STALL_TIMEOUT_MS, "Playback remained buffered");
            }
          }}
          onStalled={() => {
            if (manualPreviewing && manualPlaybackActiveRef.current) {
              armManualPlaybackTimeout(VIDEO_PREVIEW_STALL_TIMEOUT_MS, "Playback stalled while loading");
            }
          }}
          onPause={() => {
            if (manualPreviewing && manualPlaybackStartedRef.current) stopManualPlaybackAttempt();
          }}
          onError={(event) => {
            if (manualPreviewing) showAnimatedFallback(event.currentTarget.error);
          }}
          onEnded={() => {
            stopManualPlaybackAttempt();
            setPreviewMode(null);
          }}
        />
      ) : null}
      {playbackError ? (
        <span className="playbackError" role="status">
          {playbackError}
        </span>
      ) : null}
      <span className="mediaShade" />
      <span className="previewBadges">
        <span className="typeBadge">{item.media_type}</span>
        {isVideo && duration ? <span className="durationBadge">{duration}</span> : null}
      </span>
      {isVideo && !compact && !manualPreviewing ? (
        <button
          className="playBadge"
          type="button"
          aria-label="Play video with sound"
          onClick={toggleManualPreview}
          onPointerDown={keepManualPreviewTap}
          onTouchStart={keepManualPreviewTap}
        >
          <Play size={18} fill="currentColor" />
        </button>
      ) : null}
    </span>
  );
}
