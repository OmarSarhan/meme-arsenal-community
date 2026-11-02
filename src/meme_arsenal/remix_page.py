"""Readable remix recipes for private previews and revocable public links."""

import html
import uuid
from typing import Any

from fastapi.responses import HTMLResponse


def render_remix_page(
    *,
    image_url: str,
    sources: list[tuple[str, str | None]],
    summary: str,
    tweaks: dict[str, Any] | None,
    public_url: str | None = None,
    source_videos: dict[str, str] | None = None,
) -> HTMLResponse:
    def escape(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    nonce = uuid.uuid4().hex
    source_markup = []
    for index, side in enumerate(("A", "B")):
        description, image = sources[index] if index < len(sources) else ("Source unavailable", None)
        media = f'<img src="{escape(image)}" alt="Source {side}" loading="lazy">' if image else '<div class="unavailable">Source unavailable</div>'
        video = (source_videos or {}).get(side)
        if video:
            poster = f' poster="{escape(image)}"' if image else ""
            media = (
                f'<video src="{escape(video)}"{poster} controls playsinline preload="none" '
                'disablepictureinpicture disableremoteplayback controlslist="noremoteplayback" '
                f'aria-label="Play source {side}"></video>'
            )
        source_markup.append(f'<figure class="source"><h3 class="side side{side}">{side}</h3>{media}<figcaption>{escape(description)}</figcaption></figure>')

    settings_markup = "<p>No saved settings for this remix.</p>"
    intent = ""
    if tweaks:
        intent = str(tweaks.get("summary") or "")
        values = tweaks.get("values") or {}
        rows = [
            ("Schizophrenic", tweaks.get("schizo_level", 0), "Off (0)", "High (3)", ""),
            ("Chronically online", tweaks.get("chronically_online_level", 0), "Off (0)", "High (3)", ""),
        ]
        for control in tweaks.get("controls") or []:
            rows.append((
                control.get("label", "Tweak"), values.get(control.get("id"), control.get("default_value", 50)),
                f'{control.get("low_label", "Low")} (0)', f'{control.get("high_label", "High")} (100)',
                control.get("description", ""),
            ))
        settings_markup = '<dl class="settings">' + "".join(
            f'<div class="setting"><dt>{escape(label)}</dt><dd class="value">{escape(value)}</dd>'
            f'<dd class="range"><span>{escape(low)}</span><span>{escape(high)}</span></dd>'
            + (f'<dd class="settingDescription">{escape(description)}</dd>' if description else "") + '</div>'
            for label, value, low, high, description in rows
        ) + '</dl><dl class="flags">' + "".join(
            f'<div><dt>{label}</dt><dd class="{"on" if tweaks.get(key) else "off"}">{"On" if tweaks.get(key) else "Off"}</dd></div>'
            for key, label in (("no_text", "Text free"), ("anti_slop", "Anti-slop"), ("comic_strip", "Comic strip"))
        ) + '</dl>'

    # Inline Lucide Share2 for the standalone page; no external scripts or fonts.
    share_icon = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.59 13.51 6.83 3.98M15.41 6.51l-6.82 3.98"/></svg>'
    toolbar = f'<header class="toolbar"><h1>Forward Deployed Meme</h1><button id="share" aria-label="Share remix page" title="Share remix page">{share_icon}</button></header>' if public_url else ""
    canonical = f'<meta property="og:url" content="{escape(public_url)}">' if public_url else ""
    page = f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Remix | Forward Deployed Meme</title><meta name="robots" content="noindex, nofollow, noarchive">
<meta property="og:title" content="Remix | Forward Deployed Meme"><meta property="og:type" content="website">
<meta property="og:description" content="{escape(summary)}"><meta property="og:image" content="{escape(image_url)}">{canonical}
<style nonce="{nonce}">
:root {{ color-scheme: light; --ink: #16120b; --pink: #ff7ad9; --mint: #18f4d6; }}
* {{ box-sizing: border-box; letter-spacing: 0; }}
body {{ margin: 0; color: var(--ink); font: 16px/1.6 Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif; background: #fff5fc; overflow-wrap: anywhere; }}
body.preview {{ background: transparent; }}
h1, h2, h3, p, figure, dl, dd {{ margin: 0; }}
h1, h2 {{ font-size: 20px; line-height: 1.3; }}
h3 {{ font-size: 16px; }}
.toolbar {{ display: flex; align-items: center; justify-content: space-between; gap: 16px; padding: 12px max(16px, env(safe-area-inset-right)) 12px max(16px, env(safe-area-inset-left)); border-bottom: 1px solid var(--ink); background: linear-gradient(100deg, var(--pink), #faff9c 50%, var(--mint)); }}
.toolbar h1 {{ min-width: 0; }}
button {{ display: inline-flex; align-items: center; justify-content: center; flex: 0 0 44px; width: 44px; height: 44px; padding: 0; border: 1px solid var(--ink); border-radius: 0; color: #fff; background: var(--ink); cursor: pointer; }}
button:focus-visible {{ outline: 3px solid #1238ff; outline-offset: 3px; }}
.recipePage {{ max-width: 1120px; margin: 0 auto; padding: 28px max(16px, env(safe-area-inset-right)) max(36px, env(safe-area-inset-bottom)) max(16px, env(safe-area-inset-left)); }}
.preview .recipePage {{ padding: 0 0 12px; }}
section + section {{ margin-top: 32px; }}
section > h2 {{ margin-bottom: 16px; }}
.sources {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 20px; }}
.source {{ min-width: 0; }}
.side {{ display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; margin-bottom: 10px; color: #fff; }}
.sideA {{ background: #d7194c; }} .sideB {{ background: #007a4b; }}
.source img, .source video, .unavailable {{ display: block; width: 100%; height: 240px; object-fit: contain; background: #f0eee9; }}
.unavailable {{ display: grid; place-items: center; padding: 16px; }}
figcaption, .summary, .intent, .settingDescription {{ white-space: pre-wrap; }}
figcaption {{ padding-top: 12px; }}
.result {{ border-top: 1px solid var(--ink); padding-top: 24px; }}
.result img {{ display: block; width: auto; height: auto; max-width: 100%; max-height: 80vh; object-fit: contain; margin: 0 auto 20px; border: 1px solid var(--ink); }}
.preview .result img {{ max-height: 720px; }}
.summary {{ width: 100%; }}
.intent {{ max-width: 80ch; }}
@media (min-width: 601px) {{ .summary {{ text-align: justify; }} }}
.settings {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0 24px; }}
.setting {{ min-width: 0; display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px 16px; align-content: start; padding: 16px 0; border-top: 1px solid #fff; }}
.setting dt {{ font-weight: 750; }} .value {{ font-weight: 750; font-size: 20px; }}
.range {{ grid-column: 1 / -1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
.range span:last-child {{ text-align: right; }} .settingDescription {{ grid-column: 1 / -1; }}
.tweaks {{ padding: 24px; background: linear-gradient(120deg, #ffd5f1, #c8fff1); }}
.flags {{ display: flex; flex-wrap: wrap; justify-content: center; gap: 12px 24px; margin-top: 20px; border-top: 1px solid #fff; padding-top: 16px; }}
.flags > div {{ display: flex; align-items: center; gap: 12px; }}
.flags dd {{ padding: 2px 10px; }} .on {{ color: #fff; background: #007a4b; }} .off {{ color: #626262; background: #fff; }}
#status {{ position: fixed; bottom: 16px; left: 16px; right: 16px; width: fit-content; max-width: calc(100% - 32px); margin: auto; padding: 8px 16px; background: var(--mint); border: 1px solid var(--ink); }}
@media (max-width: 600px) {{ .toolbar h1 {{ font-size: 16px; }} .sources {{ gap: 12px; }} .source img, .source video, .unavailable {{ height: 160px; }} .settings {{ grid-template-columns: minmax(0, 1fr); }} .tweaks {{ padding: 16px; }} .preview .result img {{ max-height: 480px; }} }}
</style></head><body class="{'public' if public_url else 'preview'}">{toolbar}
<main class="recipePage">
<section aria-labelledby="sourcesTitle"><h2 id="sourcesTitle">Ingredients</h2><div class="sources">{''.join(source_markup)}</div></section>
<section class="result" aria-labelledby="resultTitle"><h2 id="resultTitle">The remix</h2><img src="{escape(image_url)}" alt="Generated remix"><p class="summary">{escape(summary)}</p></section>
{f'<section aria-labelledby="intentTitle"><h2 id="intentTitle">The idea</h2><p class="intent">{escape(intent)}</p></section>' if intent else ''}
<section class="tweaks" aria-labelledby="tweaksTitle"><h2 id="tweaksTitle">Settings</h2>{settings_markup}</section>
</main><p id="status" role="status" hidden></p>
<script nonce="{nonce}">
const videos = document.querySelectorAll('video');
videos.forEach(video => video.addEventListener('play', () => {{
  videos.forEach(other => {{ if (other !== video) other.pause(); }});
}}));
const button = document.getElementById('share');
const status = document.getElementById('status');
let timer;
const notify = (text) => {{ clearTimeout(timer); status.textContent = text; status.hidden = false; timer = setTimeout(() => status.hidden = true, 2400); }};
button?.addEventListener('click', async () => {{
  button.disabled = true;
  try {{
    if (navigator.share) {{ await navigator.share({{ title: document.title, url: location.href }}); }}
    else {{ await navigator.clipboard.writeText(location.href); notify('Link copied'); }}
  }} catch (error) {{
    if (error?.name !== 'AbortError') {{
      try {{ await navigator.clipboard.writeText(location.href); notify('Link copied'); }}
      catch {{ notify('Could not share. Copy the link from your address bar.'); }}
    }}
  }} finally {{ button.disabled = false; }}
}});
</script></body></html>'''
    return HTMLResponse(page, headers={
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": (
            "default-src 'none'; img-src 'self'; media-src 'self'; "
            f"style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; "
            "base-uri 'none'; form-action 'none'; "
            + ("frame-ancestors 'none'" if public_url else "frame-ancestors 'self'")
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex, nofollow, noarchive",
    })
