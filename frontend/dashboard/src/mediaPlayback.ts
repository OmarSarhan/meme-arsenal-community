type ActiveMediaPlayback = {
  id: string;
  video: HTMLVideoElement;
  stop?: () => void;
};

let activeMediaPlayback: ActiveMediaPlayback | null = null;

export function claimMediaPlayback(video: HTMLVideoElement, playbackId: string, stop?: () => void) {
  if (activeMediaPlayback?.id === playbackId && activeMediaPlayback.video === video) {
    activeMediaPlayback.stop = stop;
    return;
  }
  const previous = activeMediaPlayback;
  activeMediaPlayback = null;
  if (previous) {
    previous.video.pause();
    previous.stop?.();
  }
  activeMediaPlayback = { id: playbackId, video, stop };
}

export function releaseMediaPlayback(playbackId: string) {
  if (activeMediaPlayback?.id === playbackId) activeMediaPlayback = null;
}

export function formatDuration(seconds: number | string | null | undefined) {
  const duration = typeof seconds === "number" ? seconds : typeof seconds === "string" ? Number(seconds) : null;
  if (duration === null || Number.isNaN(duration)) return "";
  const rounded = Math.max(0, Math.round(duration));
  if (rounded === 0) return "";
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours > 0) {
    return `${hours}:${String(remainingMinutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
  }
  return `${minutes}:${String(remainingSeconds).padStart(2, "0")}`;
}
