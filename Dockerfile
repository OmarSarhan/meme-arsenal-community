# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS dashboard
WORKDIR /src/frontend/dashboard
COPY frontend/dashboard/package*.json ./
RUN npm ci
COPY frontend/dashboard/ ./
RUN npm run build

FROM python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2 AS app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      tesseract-ocr \
      webp; \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && pip install .

COPY --from=dashboard /src/frontend/dashboard/dist /app/static-dashboard
COPY docker/online-entrypoint.sh /usr/local/bin/online-entrypoint
COPY docker/community-entrypoint.py /usr/local/bin/community-entrypoint.py
COPY docker/community-sync.sh /usr/local/bin/community-sync.sh
RUN chmod +x /usr/local/bin/online-entrypoint

ENTRYPOINT ["online-entrypoint"]
CMD ["arsenal", "serve-online", "--host", "0.0.0.0", "--port", "8000"]
