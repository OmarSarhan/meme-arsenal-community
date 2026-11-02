#!/usr/bin/env bash
# CI only: run in a disposable candidate checkout, never a configured archive.
set -euo pipefail
cd "$(dirname "$0")/.."
evidence="${RUNNER_TEMP:?Requires disposable CI runner}/fresh-mock-demo-evidence"
mkdir -p "$evidence"
exec > >(tee "$evidence/commands.log") 2>&1
trap 'status=$?; echo "Acceptance exit status: $status"' EXIT
set -x
git rev-parse HEAD
for path in env .env config.yaml config.yml meme-arsenal-reserve reserve .venv venv ENV node_modules dist frontend/dashboard/node_modules frontend/dashboard/dist; do
  if [[ -e "$path" || -L "$path" ]]; then
    echo "Fresh-source precondition failed: $path"
    exit 1
  fi
done
image="meme-arsenal-fresh-demo:${GITHUB_RUN_ID:-local}"
docker build --target fresh-demo-tools -f docker/test-chromium.Dockerfile -t "$image" .
docker image inspect --format '{{.Id}}' "$image"
# Only this disposable checkout is mounted. No ports, sockets, secrets or user homes.
docker run --rm --network bridge -v "$PWD:/workspace" --entrypoint /usr/bin/env "$image" \
  -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/tmp/install-home \
  bash -euxo pipefail -c '
    mkdir -p "$HOME"
    cd /workspace
    python -m venv .venv
    .venv/bin/python -m pip install -e ".[dev,browser]"
    npm --prefix frontend/dashboard ci --no-audit --no-fund
    .venv/bin/python -m pip check
    .venv/bin/python -m pip freeze
    npm --prefix frontend/dashboard ls --all
    .venv/bin/python --version
    .venv/bin/python -m pip --version
    .venv/bin/python -m playwright --version
    node --version
    npm --version
    ffmpeg -version
    chromium --version
  '
echo 'Install finished; new runtime container has no external network (loopback only).'
docker run --rm --network none -v "$PWD:/workspace" --entrypoint /usr/bin/env "$image" \
  -i PATH=/workspace/.venv/bin:/usr/local/bin:/usr/bin:/bin HOME=/tmp/runtime-home \
  PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium \
  /workspace/.venv/bin/python -m pytest -s -m chromium tests/test_fresh_mock_demo.py
# Containers are removed even on failure; checkout and installed paths belong to
# the disposable runner and are discarded with it. Evidence survives as artifact.
