#!/usr/bin/env python3
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
import tempfile

import sys
if "/workspace" not in sys.path:
    sys.path.insert(0, "/workspace")
from tests.test_dashboard_chromium import served_dashboard, chromium_page, _make_image, _make_hover_video, _catalog_item
from meme_arsenal.config import Settings, ensure_reserve_layout


def build_catalog(settings: Settings) -> None:
    _make_image(settings.previews_dir / "image-card.jpg", "STATIC IMAGE", (42, 96, 143))
    _make_image(settings.previews_dir / "video-poster.jpg", "HOVER VIDEO", (120, 42, 118))
    _make_hover_video(settings.video_previews_dir / "hover-card.mp4")

    items = [
        _catalog_item(
            item_id=1,
            media_type="image",
            summary="static dashboard image",
            preview_path="../../media/previews/image-card.jpg",
        ),
        _catalog_item(
            item_id=2,
            media_type="video",
            summary="hover video preview target",
            preview_path="../../media/previews/video-poster.jpg",
            hover_preview_path="../../media/video-previews/hover-card.mp4",
            original_path="../../media/video-previews/hover-card.mp4",
            duration_seconds=3.0,
        ),
    ]
    (settings.catalog_dir / "items-000.json").write_text(
        json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8"
    )
    (settings.catalog_dir / "tags.json").write_text(
        json.dumps(
            {
                "media_type": [{"value": "image", "count": 1}, {"value": "video", "count": 1}],
                "topics": [{"value": "dashboard", "count": 2}],
                "tone": [{"value": "playful", "count": 2}],
                "reaction_type": [{"value": "approval", "count": 2}],
                "meme_format": [{"value": "reaction meme", "count": 2}],
                "language": [{"value": "English", "count": 2}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (settings.catalog_dir / "index.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generated_at": "2026-06-14T12:00:00+00:00",
                "item_count": len(items),
                "chunk_size": 250,
                "chunks": ["../../catalog/items-000.json"],
                "tags_path": "../../catalog/tags.json",
                "search_index_path": "../../catalog/search-results",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def score_tap_targets(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const all = Array.from(document.querySelectorAll('button, a, input, [role="button"]'));
          const minTargets = all.filter((element) => {
            const box = element.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && (box.width < 38 || box.height < 38);
          });
          const tooNarrow = all.filter((element) => {
            const box = element.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && box.width < 44;
          });
          const tooShort = all.filter((element) => {
            const box = element.getBoundingClientRect();
            return box.width > 0 && box.height > 0 && box.height < 44;
          });
          const summary = minTargets.slice(0, 12).map((element) => ({
            selector: element.tagName.toLowerCase() + (element.className ? `.${element.className.split(/\s+/).join('.')}` : ''),
            w: Math.round(element.getBoundingClientRect().width),
            h: Math.round(element.getBoundingClientRect().height),
          }));
          return {
            totalInteractive: all.length,
            belowMinimum: summary,
            below44w: tooNarrow.length,
            below44h: tooShort.length,
          };
        }"""
    )


def audit_width(url: str, width: int) -> dict[str, Any]:
    height = 1100
    with chromium_page(url, width=width, height=height, mobile=width <= 760) as page:
        page.evaluate(
            """() => {
              const state = {
                errors: [],
                networkFailures: [],
              };
              window.__layoutAuditErrors = state;
              const capture = (type) => (event) => {
                state.errors.push({
                  type,
                  message: event.message || String(event.reason || event.error || event),
                });
              };
              window.addEventListener('error', capture('error'));
              window.addEventListener('unhandledrejection', capture('unhandledrejection'));
            }"""
        )
        page.wait_for_selector(".topbar")
        page.wait_for_selector(".galleryGrid")

        page.evaluate("""() => {
          window.__layoutPerf = {
            firstContentfulPaint: 0,
            largestContentfulPaint: 0,
            cumulativeLayoutShift: 0,
          };
        }""")

        baseline = page.evaluate(
            """() => {
              const overflow = [];
              const viewportW = window.innerWidth;
              const viewportH = window.innerHeight;
              document.querySelectorAll('*').forEach((node) => {
                if (!(node instanceof Element)) return;
                const rect = node.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) return;
                const style = getComputedStyle(node);
                if (style.visibility === 'hidden' || style.display === 'none') return;
                if (rect.left < -1 || rect.right > viewportW + 1 || rect.bottom > (window.scrollY + viewportH) + 1) {
                  overflow.push({
                    selector: node.id ? `#${node.id}` : '.' + Array.from(node.classList).slice(0, 2).join('.'),
                    left: Math.round(rect.left),
                    right: Math.round(rect.right),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                    tag: node.tagName.toLowerCase(),
                    text: (node.textContent || '').trim().slice(0, 60),
                  });
                }
              });

              const clippedTextNodes = Array.from(document.querySelectorAll('*')).filter((node) => {
                if (!(node instanceof HTMLElement)) return false;
                const style = getComputedStyle(node);
                return style.overflow === 'hidden' && style.textOverflow === 'ellipsis';
              }).slice(0, 14).map((node) => {
                return {
                  selector: node.tagName.toLowerCase() + (node.className ? `.${node.className.split(/\s+/).slice(0, 2).join('.')}` : ''),
                  scrollWidth: Math.round(node.scrollWidth),
                  clientWidth: Math.round(node.clientWidth),
                  text: (node.textContent || '').trim().slice(0, 80),
                };
              });

              const topbarRect = document.querySelector('.topbar')?.getBoundingClientRect();
              const workspaceRect = document.querySelector('.workspace')?.getBoundingClientRect();
              const sectionButtons = Array.from(document.querySelectorAll('.topbarActions button')).map((button) => ({
                label: (button.textContent || '').trim().slice(0, 80),
                width: Math.round(button.getBoundingClientRect().width),
                height: Math.round(button.getBoundingClientRect().height),
              }));

              return {
                viewportW: viewportW,
                viewportH: viewportH,
                docW: Math.ceil(document.documentElement.scrollWidth),
                docH: Math.ceil(document.documentElement.scrollHeight),
                bodyW: Math.ceil(document.body.scrollWidth),
                topbarTop: topbarRect ? Math.round(topbarRect.top) : null,
                workspaceTop: workspaceRect ? Math.round(workspaceRect.top) : null,
                topbarW: topbarRect ? Math.round(topbarRect.width) : null,
                topbarActions: sectionButtons,
                overflowCount: overflow.length,
                overflowSample: overflow.slice(0, 10),
                clippedTextCount: clippedTextNodes.length,
                clippedTextSample: clippedTextNodes,
              };
            }"""
        )

        page.evaluate("window.scrollTo(0, 420)")
        sticky = page.evaluate(
            """() => {
              const topbar = document.querySelector('.topbar');
              const r = topbar ? topbar.getBoundingClientRect() : null;
              return {
                top: r ? Math.round(r.top) : null,
                sticky: getComputedStyle(topbar || document.body).position,
                stuck: r ? r.top <= 1 : null,
              };
            }"""
        )

        page.evaluate("document.querySelector('.filterButton')?.click()")
        page.wait_for_selector('.filters.is-open')
        filter_open = page.evaluate("Boolean(document.querySelector('.filters.is-open'))")
        filter_sheet = page.evaluate(
            """() => {
              const sheet = document.querySelector('.filters');
              if (!sheet) return {exists: false};
              const rect = sheet.getBoundingClientRect();
              return {
                exists: true,
                width: Math.round(rect.width),
                left: Math.round(rect.left),
                right: Math.round(rect.right),
                top: Math.round(rect.top),
              };
            }"""
        )

        page.evaluate("document.querySelector('.filterButton')?.click()")
        page.evaluate("new Promise((resolve) => setTimeout(resolve, 200))", await_promise=True)
        filter_closed = page.evaluate("Boolean(document.querySelector('.filters.is-open'))")

        perf = page.evaluate(
            """() => {
              const entries = performance.getEntriesByType('resource').filter((entry) => entry.transferSize > 0);
              const navTimes = entries.map((entry) => ({
                name: entry.name,
                duration: Math.round(entry.duration),
              })).filter((entry) => entry.name.includes('/dashboard/dist/') || entry.name.includes('/api/') || entry.name.includes('/media/'));
              const paint = performance.getEntriesByType('paint').map((entry) => ({
                name: entry.name,
                startTime: Math.round(entry.startTime),
              }));
              return {
                resourceCount: entries.length,
                slowResources: navTimes.filter((entry) => entry.duration > 1200),
                paint,
              };
            }"""
        )

        diag = page.evaluate("window.__layoutAuditErrors || {}")
        tap = score_tap_targets(page)

        return {
            "width": width,
            "layout": baseline,
            "sticky": sticky,
            "filter": {
              "opened": filter_open,
              "closed": filter_closed,
              "sheet": filter_sheet,
            },
            "performance": perf,
            "diagnostics": diag,
            "tapTargets": tap,
        }



def main() -> None:
    width_set = [375, 390, 768, 1024, 1280, 1440]
    results: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="arsenal-mobile-audit-") as tmpdir:
        settings = Settings(
            reserve_path=Path(tmpdir),
            database_url=f"sqlite:///{Path(tmpdir) / 'data' / 'arsenal.sqlite'}",
            ai_provider="mock",
        )
        ensure_reserve_layout(settings)
        build_catalog(settings)

        with served_dashboard(settings) as url:
            for width in width_set:
                results.append(audit_width(url, width))

    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    if os.environ.get("MOBILE"):
        print('ok')
    main()
