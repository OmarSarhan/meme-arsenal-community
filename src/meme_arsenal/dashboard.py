from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .branding import APP_NAME
from .config import Settings


def frontend_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "frontend" / "dashboard"


def write_dashboard_config(settings: Settings) -> None:
    settings.dashboard_dist_dir.mkdir(parents=True, exist_ok=True)
    (settings.dashboard_dist_dir / "arsenal-config.json").write_text(
        '{\n  "catalogIndex": "../../catalog/index.json",\n  "remixApiBase": "../../api/remix"\n}\n',
        encoding="utf-8",
    )


def write_online_dashboard_config(settings: Settings) -> None:
    settings.dashboard_dist_dir.mkdir(parents=True, exist_ok=True)
    (settings.dashboard_dist_dir / "arsenal-config.json").write_text(
        '{\n  "mode": "online",\n  "apiBase": "/api",\n  "remixApiBase": "/api/remix"\n}\n',
        encoding="utf-8",
    )


def build_dashboard(settings: Settings) -> dict[str, str]:
    target = settings.dashboard_dist_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    source = frontend_dir()
    npm = shutil.which("npm")
    if npm and source.exists():
        if not (source / "node_modules").exists():
            subprocess.run([npm, "install"], cwd=source, check=True)
        subprocess.run(
            [npm, "run", "build", "--", "--outDir", str(target), "--emptyOutDir"],
            cwd=source,
            check=True,
        )
        write_dashboard_config(settings)
        return {"mode": "vite", "dist": str(target)}

    write_fallback_dashboard(target)
    write_dashboard_config(settings)
    return {"mode": "fallback", "dist": str(target)}


def build_online_dashboard(settings: Settings) -> dict[str, str]:
    target = settings.dashboard_dist_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    source = frontend_dir()
    npm = shutil.which("npm")
    if not npm or not source.exists():
        raise RuntimeError("Online dashboard builds require npm and frontend/dashboard.")
    if not (source / "node_modules").exists():
        subprocess.run([npm, "install"], cwd=source, check=True)
    subprocess.run(
        [npm, "run", "build", "--", "--outDir", str(target), "--emptyOutDir"],
        cwd=source,
        check=True,
    )
    write_online_dashboard_config(settings)
    return {"mode": "vite-online", "dist": str(target)}


def write_fallback_dashboard(target: Path) -> None:
    assets = target / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (target / "index.html").write_text(
        f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{APP_NAME}</title>
    <link rel="stylesheet" href="./assets/fallback.css" />
  </head>
  <body>
    <main id="app"></main>
    <script src="./assets/fallback.js"></script>
  </body>
</html>
""",
        encoding="utf-8",
    )
    (assets / "fallback.css").write_text(
        """
:root {
  color-scheme: light;
  --page-start: #f0e8d5;
  --page-mid: #fdf9e8;
  --page-end: #ffffff;
  --ink: #0f172a;
  --ink-muted: rgba(15, 23, 42, .58);
  --panel: rgba(15, 23, 42, .045);
  --panel-strong: rgba(255, 255, 255, .78);
  --panel-deep: #e9ddc4;
  --border: rgba(15, 23, 42, .13);
  --accent: #ef467e;
  --accent-mid: #7b33e9;
  --accent-end: #eb4759;
  --accent-gradient: linear-gradient(90deg, var(--accent), var(--accent-mid), var(--accent-end));
  --warning: #b42318;
  --warning-soft: #fff1ed;
  --warning-border: #f4b7a9;
  font-family: Inter, ui-sans-serif, system-ui, sans-serif;
}
body {
  margin: 0;
  color: var(--ink);
  background:
    radial-gradient(circle at 8% -10%, rgba(123, 51, 233, .12), transparent 32rem),
    radial-gradient(circle at 92% 8%, rgba(239, 70, 126, .13), transparent 26rem),
    linear-gradient(135deg, var(--page-start), var(--page-mid) 54%, var(--page-end));
}
.shell { max-width: 1120px; margin: 0 auto; padding: 18px; }
.top { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
.topActions { display: flex; gap: 8px; align-items: center; }
h1 { font-size: 1.45rem; margin: 0; letter-spacing: 0; }
button, input, select { font: inherit; }
input, select { min-height: 42px; border: 1px solid var(--border); border-radius: 6px; padding: 0 12px; color: var(--ink); background: var(--panel-strong); }
.random { min-height: 40px; border: 0; border-radius: 8px; padding: 0 12px; color: white; background: var(--accent-gradient); cursor: pointer; }
.remix { min-height: 40px; border: 0; border-radius: 8px; padding: 0 12px; color: white; background: linear-gradient(90deg, #ec4899, #8b5cf6, #2563eb); cursor: pointer; }
.controls { display: flex; gap: 8px; flex-wrap: wrap; width: 100%; }
.controls input { flex: 0 1 clamp(220px, 22vw, 360px); }
.remixPanel { width: 100%; display: grid; gap: 4px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel-strong); }
.remixPanel span { color: var(--ink-muted); font-size: .86rem; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(154px, 1fr)); gap: 12px; margin-top: 16px; }
.loadMore { display: flex; justify-content: center; margin: 16px 0 4px; }
.loadMore button { min-height: 40px; border: 1px solid var(--border); border-radius: 8px; padding: 0 12px; background: var(--panel-strong); cursor: pointer; }
.card { background: var(--panel-strong); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; cursor: pointer; box-shadow: 0 10px 20px rgba(15, 23, 42, .05); }
.card.blocked { border-color: var(--warning); box-shadow: 0 0 0 2px rgba(180, 35, 24, .18), 0 14px 28px rgba(15, 23, 42, .07); }
.mediaFrame { position: relative; width: 100%; aspect-ratio: 4 / 3; overflow: hidden; display: grid; place-items: center; background: linear-gradient(135deg, rgba(255,255,255,.44), rgba(15,23,42,.1)), var(--panel-deep); }
.mediaFrame img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain; z-index: 1; }
.mediaFrame img.backdrop { object-fit: cover; opacity: .25; filter: blur(12px) saturate(.9); transform: scale(1.1); z-index: 0; }
.mediaFrame .shade { position: absolute; inset: auto 0 0; height: 48%; background: linear-gradient(180deg, transparent, rgba(15,15,15,.48)); z-index: 2; }
.mediaFrame .badgeRow { position: absolute; inset: auto 6px 6px; z-index: 3; display: flex; justify-content: space-between; gap: 6px; }
.mediaFrame .badge { min-height: 20px; display: inline-flex; align-items: center; border-radius: 5px; padding: 4px 6px; background: rgba(15,15,15,.74); color: white; font-size: .7rem; line-height: 1; text-transform: uppercase; }
.mediaFrame .play { position: absolute; inset: 50% auto auto 50%; z-index: 3; width: 38px; height: 38px; display: grid; place-items: center; border-radius: 999px; color: white; background: rgba(15,15,15,.72); transform: translate(-50%, -50%); }
.missingFrame { color: var(--ink-muted); }
.meta { padding: 10px; display: grid; gap: 6px; }
.summary { font-size: .9rem; line-height: 1.25; }
.tags { display: flex; gap: 5px; flex-wrap: wrap; }
.tag { background: rgba(239, 70, 126, .1); color: #92104b; font-size: .72rem; padding: 3px 6px; border-radius: 999px; }
.tag:nth-child(2n) { background: rgba(123, 51, 233, .1); color: #4c1d95; }
.blockedBadge { display: inline-flex; width: fit-content; color: var(--warning); background: var(--warning-soft); border: 1px solid var(--warning-border); border-radius: 999px; padding: 3px 7px; font-size: .72rem; }
.detail { margin-top: 16px; background: var(--panel-strong); border: 1px solid var(--border); border-radius: 8px; padding: 12px; box-shadow: 0 16px 34px rgba(15, 23, 42, .08); }
.detail img, .detail video { max-width: 100%; max-height: 58vh; height: auto; object-fit: contain; border-radius: 6px; background: #111; }
.panel { margin-top: 10px; padding: 10px; border: 1px solid var(--border); border-radius: 8px; background: var(--panel); }
.panel.blocked { border-color: var(--warning-border); background: var(--warning-soft); color: var(--warning); }
.panel span { display: block; color: var(--ink-muted); font-size: .78rem; margin-bottom: 4px; }
@media (max-width: 430px) { .shell { padding: 12px; } .grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
""",
        encoding="utf-8",
    )
    (assets / "fallback.js").write_text(
        """
	const APP_NAME = "__APP_NAME__";
	const BATCH_SIZE = 96;
	const HIDDEN_MEDIA_TYPES = new Set(["document", "sticker"]);
	const state = { items: [], selected: null, query: "", mediaType: "all", remixOpen: false, remixStatus: "", limit: BATCH_SIZE };
const app = document.getElementById("app");
const genericChipValues = new Set(["blocked", "caption", "classification", "content", "gemini blocked", "gif", "image", "media", "meme", "photo", "prohibited content", "saved", "screenshot", "sticker", "text", "unknown", "video", "visible text"]);
const tokenStopWords = new Set(["and", "for", "from", "has", "into", "just", "meme", "not", "that", "the", "this", "with"]);
function cleanText(value) {
  return String(value || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim().replace(/\\s+/g, " ");
}
function textTokens(value) {
  return cleanText(value).split(" ").filter(token => token.length > 2 && !tokenStopWords.has(token));
}
function sameText(left, right) {
  const leftText = cleanText(left);
  const rightText = cleanText(right);
  return Boolean(leftText && rightText && leftText === rightText);
}
function itemContextText(item) {
  return [item.caption, item.source_title || "", item.source_description || "", item.summary, item.visible_text, item.narration, item.meme_format || "", ...(item.reaction_type || [])].join(" ");
}
function chipScore(item, value, manualTags) {
  const normalized = cleanText(value);
  if (!normalized || genericChipValues.has(normalized) || normalized === cleanText(item.media_type)) return -1;
  if (manualTags.has(normalized)) return 100;
  const tokens = textTokens(normalized);
  if (!tokens.length) return -1;
  const context = cleanText(itemContextText(item));
  const contextTokens = new Set(textTokens(context));
  let score = context.includes(normalized) ? 20 : 0;
  tokens.forEach(token => { if (contextTokens.has(token)) score += 10; });
  return score;
}
function itemChips(item, limit) {
  const manualTags = new Set((item.manual_tags || []).map(cleanText));
  const seen = new Set();
  return [...(item.manual_tags || []), ...(item.topics || []), ...(item.keywords || []), ...(item.reaction_type || [])]
    .map((value, index) => ({ value, index, normalized: cleanText(value), score: chipScore(item, value, manualTags) }))
    .filter(chip => {
      if (!chip.normalized || seen.has(chip.normalized) || chip.score <= 0) return false;
      seen.add(chip.normalized);
      return true;
    })
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .slice(0, limit)
    .map(chip => chip.value);
}
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || Number.isNaN(Number(seconds))) return "";
  const rounded = Math.max(0, Math.round(Number(seconds)));
  if (rounded === 0) return "";
  const minutes = Math.floor(rounded / 60);
  const remainingSeconds = rounded % 60;
  const hours = Math.floor(minutes / 60);
  const remainingMinutes = minutes % 60;
  if (hours > 0) return `${hours}:${String(remainingMinutes).padStart(2, "0")}:${String(remainingSeconds).padStart(2, "0")}`;
  return `${minutes}:${String(remainingSeconds).padStart(2, "0")}`;
}
function mediaFrame(item) {
  const source = item.thumbnail_path || item.preview_path || item.original_path || "";
  const duration = item.media_type === "video" ? formatDuration(item.duration_seconds) : "";
  if (!source) return `<div class="mediaFrame missingFrame"><span>${item.media_type || "media"}</span></div>`;
  const play = item.media_type === "video" ? `<span class="play">&#9654;</span>` : "";
  const durationBadge = duration ? `<span class="badge">${duration}</span>` : "";
  return `<div class="mediaFrame"><img class="backdrop" src="${source}" alt="" loading="lazy" decoding="async"><img src="${source}" alt="" loading="lazy" decoding="async"><span class="shade"></span><span class="badgeRow"><span class="badge">${item.media_type}</span>${durationBadge}</span>${play}</div>`;
}
	async function loadCatalog() {
	  const config = await fetch("./arsenal-config.json").then(r => r.json()).catch(() => ({ catalogIndex: "../../catalog/index.json" }));
	  const index = await fetch(config.catalogIndex).then(r => r.json());
	  const chunks = await Promise.all(index.chunks.map(path => fetch(path).then(r => r.json())));
	  state.items = chunks.flatMap(chunk => chunk.items || []).filter(item => !HIDDEN_MEDIA_TYPES.has(item.media_type));
	  render();
	}
function matches(item) {
  const text = [item.caption, item.summary, item.visible_text, item.narration, ...(item.keywords || []), ...(item.topics || [])].join(" ").toLowerCase();
  return (!state.query || text.includes(state.query.toLowerCase())) && (state.mediaType === "all" || item.media_type === state.mediaType);
}
function card(item) {
  const tags = itemChips(item, 4).map(tag => `<span class="tag">${tag}</span>`).join("");
  const blocked = item.classification_status === "blocked";
  const badge = blocked ? `<span class="blockedBadge">Blocked</span>` : "";
  return `<article class="card ${blocked ? "blocked" : ""}" data-id="${item.id}">${mediaFrame(item)}<div class="meta"><strong>${item.media_type}</strong>${badge}<div class="summary">${item.summary || item.caption || "Saved meme"}</div><div class="tags">${tags}</div></div></article>`;
}
function detail(item) {
  if (!item) return "";
  const mediaSource = item.preview_path || item.original_path || "";
  const media = item.media_type === "video"
    ? item.original_path
      ? `<video src="${item.original_path}" controls playsinline poster="${item.preview_path || ""}"></video>`
      : mediaSource
        ? `<img src="${mediaSource}" alt="">`
        : `<div class="missingFrame"><span>${item.media_type || "media"}</span></div>`
    : mediaSource
      ? `<img src="${mediaSource}" alt="">`
      : `<div class="missingFrame"><span>${item.media_type || "media"}</span></div>`;
  const blocked = item.classification_status === "blocked" ? `<div class="panel blocked"><span>Classification</span>Gemini blocked classification${item.classification_block_reason ? `: ${item.classification_block_reason}` : ""}</div>` : "";
  const narration = item.narration && !sameText(item.narration, item.visible_text) ? `<div class="panel"><span>Narration</span><p>${item.narration}</p></div>` : "";
  const visibleText = item.visible_text ? `<div class="panel"><span>Visible text</span><p>${item.visible_text}</p></div>` : "";
  const chips = itemChips(item, 10).map(tag => `<span class="tag">${tag}</span>`).join("");
  const telegram = item.telegram_post_url ? `<a href="${item.telegram_post_url}">Open Telegram post</a>` : "";
  return `<section class="detail">${media}<h2>${item.summary || "Saved meme"}</h2>${blocked}<p>${item.caption || ""}</p>${narration}${visibleText}<div class="tags">${chips}</div><a href="${item.original_path}">Open original</a>${telegram}</section>`;
}
function render() {
  const items = state.items.filter(matches);
  const renderedItems = items.slice(0, state.limit);
  const types = ["all", ...Array.from(new Set(state.items.map(item => item.media_type))).sort()];
  const remixPanel = state.remixOpen ? `<section class="remixPanel"><strong>Remix</strong><span>${state.remixStatus || "Start arsenal serve-dashboard to use Remix"}</span></section>` : "";
  const loadMore = renderedItems.length < items.length ? `<div class="loadMore"><button type="button">Load more (${renderedItems.length} of ${items.length})</button></div>` : "";
  app.innerHTML = `<div class="shell"><div class="top"><div class="topActions"><button class="random" type="button">Random</button><button class="remix" type="button">Remix</button></div><h1>${APP_NAME}</h1><div>${renderedItems.length} of ${items.length} items</div>${remixPanel}</div><div class="controls"><input placeholder="Search captions, OCR, keywords" value="${state.query}"><select>${types.map(type => `<option ${type === state.mediaType ? "selected" : ""}>${type}</option>`).join("")}</select></div><div class="grid">${renderedItems.map(card).join("")}</div>${loadMore}${detail(state.selected)}</div>`;
  app.querySelector(".random").addEventListener("click", () => { if (!items.length) return; state.selected = items[Math.floor(Math.random() * items.length)]; render(); });
  app.querySelector(".remix").addEventListener("click", async () => {
    state.remixOpen = !state.remixOpen;
    state.remixStatus = state.remixOpen ? "Checking Remix server" : "";
    render();
    if (!state.remixOpen) return;
    try {
      const health = await fetch("../../api/remix/health").then(response => {
        if (!response.ok) throw new Error(String(response.status));
        return response.json();
      });
      state.remixStatus = health.enabled ? `Ready with ${health.model}` : health.message;
    } catch {
      state.remixStatus = "Start arsenal serve-dashboard to use Remix";
    }
    render();
  });
  const loadMoreButton = app.querySelector(".loadMore button");
  if (loadMoreButton) loadMoreButton.addEventListener("click", () => { state.limit += BATCH_SIZE; render(); });
  app.querySelector("input").addEventListener("input", event => { state.query = event.target.value; state.limit = BATCH_SIZE; render(); });
  app.querySelector("select").addEventListener("change", event => { state.mediaType = event.target.value; state.limit = BATCH_SIZE; render(); });
  app.querySelectorAll(".card").forEach(node => node.addEventListener("click", () => { state.selected = state.items.find(item => String(item.id) === node.dataset.id); render(); }));
}
loadCatalog();
""".replace("__APP_NAME__", APP_NAME),
        encoding="utf-8",
    )
