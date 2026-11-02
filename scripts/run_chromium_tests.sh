#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
HAS_LOCAL_TOOLS=0

cd "$ROOT_DIR"

if command -v npm >/dev/null 2>&1 && python -c 'import playwright.sync_api' 2>/dev/null; then
  # Reuse system Chromium, or the revision installed by Playwright.
  BROWSER_PATH="${PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH:-$(command -v chromium || true)}"
  if [[ -z "$BROWSER_PATH" ]]; then
    BROWSER_PATH="$(python -c 'from playwright.sync_api import sync_playwright; p = sync_playwright().start(); print(p.chromium.executable_path); p.stop()')"
  fi
  if [[ -x "$BROWSER_PATH" ]]; then
    export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH="$BROWSER_PATH"
    HAS_LOCAL_TOOLS=1
  fi
fi

if [[ "${HAS_LOCAL_TOOLS}" == "1" && "${MEME_ARSENAL_FORCE_DOCKER:-0}" != "1" ]]; then
  npm --prefix frontend/dashboard ci --no-audit --no-fund
  npm --prefix frontend/dashboard run build
  exec python -m pytest -m chromium tests/test_dashboard_workflow.py tests/test_recipe_preview_chromium.py "$@"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Playwright, Chromium, and npm are not all available, and docker is not available for fallback."
  echo "Install .[dev,browser], Chromium, and npm, or install docker and rerun."
  exit 1
fi

if [ "${HAS_LOCAL_TOOLS}" == "1" ]; then
  echo "Local tooling present, but docker mode was forced (MEME_ARSENAL_FORCE_DOCKER=1)."
fi

docker compose -f deploy/test/compose.yml run --build --rm chromium-tests "$@"
