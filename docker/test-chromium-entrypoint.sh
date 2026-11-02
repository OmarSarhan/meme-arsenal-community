#!/usr/bin/env bash
set -euo pipefail

cd /workspace

# The image supplies Python/browser dependencies; use the same build and test
# selection as contributors, without recursively invoking Docker.
export MEME_ARSENAL_FORCE_DOCKER=0
exec scripts/run_chromium_tests.sh "$@"
