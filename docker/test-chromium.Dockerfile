FROM node:22-bookworm@sha256:63538dfe497cc29dfafdda25ada09b46db68a3f4cc205abf1f5d6bf0913f3384 AS node-tools

# No project dependencies: fresh-demo installs them in a new mounted venv.
FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS fresh-demo-tools
COPY --from=node-tools /usr/local/bin/node /usr/local/bin/node
COPY --from=node-tools /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && \
    apt-get update && apt-get install -y --no-install-recommends ffmpeg chromium ca-certificates && \
    rm -rf /var/lib/apt/lists/*
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
WORKDIR /workspace

# Keep the default image and its entrypoint unchanged for workflow/recipe tests.
FROM node-tools AS chromium-tests

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      python3 \
      python3-pip \
      chromium \
      ca-certificates && \
    ln -sf /usr/bin/python3 /usr/bin/python && \
    ln -sf /usr/bin/pip3 /usr/bin/pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY pyproject.toml /workspace/pyproject.toml
COPY README.md /workspace/README.md
COPY LICENSE /workspace/LICENSE
COPY alembic.ini /workspace/alembic.ini
COPY migrations /workspace/migrations
COPY src /workspace/src

RUN pip install --no-input --disable-pip-version-check --break-system-packages --upgrade pip && \
    pip install --no-input --disable-pip-version-check --break-system-packages -e .[dev,browser]

COPY docker/test-chromium-entrypoint.sh /usr/local/bin/test-chromium
RUN chmod +x /usr/local/bin/test-chromium

ENTRYPOINT ["/usr/local/bin/test-chromium"]
CMD []

# Community UI proof uses the same tooling, with a fixture CA in Chromium's NSS DB.
# This target is test-only; the production Dockerfile has no browser dependency.
FROM chromium-tests AS community-browser
RUN apt-get update && apt-get install -y --no-install-recommends libnss3-tools && \
    rm -rf /var/lib/apt/lists/*

# Preserve the previous default test image for all other browser jobs.
FROM chromium-tests AS default-tests
