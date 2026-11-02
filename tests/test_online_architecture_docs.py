from __future__ import annotations

from pathlib import Path


def test_online_architecture_doc_covers_required_operations():
    text = Path("docs/online-architecture.md").read_text(encoding="utf-8").lower()

    for required in [
        "system overview",
        "database architecture",
        "public and private boundaries",
        "api runtime",
        "remix worker flow",
        "cron sync",
        "docker compose layout",
        "environment variables",
        "first deploy",
        "update deploy",
        "health checks",
        "backup",
        "restore",
        "rollback",
    ]:
        assert required in text


def test_online_architecture_doc_lists_live_api_routes():
    text = Path("docs/online-architecture.md").read_text(encoding="utf-8")

    for route in [
        "GET /api/health",
        "GET /api/catalog-state",
        "GET /api/items",
        "GET /api/items/random",
        "GET /api/items/{id}",
        "GET /api/tags",
        "PATCH /api/session/items/{id}",
        "GET /api/remix/health",
        "POST /api/remix/analysis",
        "POST /api/remix/jobs",
        "GET /api/remix/jobs/{id}",
        "GET /api/remix/jobs/{id}/image",
        "POST /api/remix/jobs/{id}/save",
        "POST /api/remix/uploads",
        "POST /api/remix/uploads/{id}/save",
    ]:
        assert route in text
