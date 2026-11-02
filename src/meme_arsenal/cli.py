from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
import os
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any

from sqlalchemy import text
import typer

from .ai_circuit import ai_circuit_status
from .ai import classify_pending_media
from .branding import APP_NAME, RESERVE_NAME
from .catalog import export_catalog
from .config import Settings, ensure_reserve_layout, load_settings
from .dashboard import build_dashboard, build_online_dashboard
from .db import ensure_schema, get_engine, session_scope
from .embeddings import embed_pending_media
from .google_api import probe_google_credit
from .logging import console, uvicorn_log_config, write_run_report
from .maintenance import cleanup_online_state
from .media import process_pending_media, repair_missing_media_references, warm_browser_playback
from .migration import migrate_sqlite_to_postgres
from .pipeline import run_media_pipeline
from .remix_server import serve_dashboard
from .search import export_search_results, search_media
from .telegram_importer import (
    backfill_telegram_post_urls,
    import_fixture_messages,
    import_saved_messages,
    telegram_login_sync,
)


app = typer.Typer(no_args_is_help=True, help=f"{APP_NAME} importer and live dashboard service tools.")
owner_app = typer.Typer(help="Explicit offline owner provisioning; never merges namespaces.")
app.add_typer(owner_app, name="owner")


@owner_app.command("inventory")
def owner_inventory() -> None:
    import json
    from .owner import inventory
    try:
        settings = load_settings(ignore_owner=True)
        try:
            settings = load_settings()
        except ValueError:
            pass
        result = inventory(settings)
    except Exception:
        raise typer.BadParameter("Cannot inventory schema; migrate first and check DB access") from None
    print(json.dumps(result, ensure_ascii=True))


@owner_app.command("initialize")
def owner_initialize(allow_empty_namespace: bool = False) -> None:
    from .owner import provision
    print(provision(load_settings(), initialize=True, allow_empty_namespace=allow_empty_namespace))


@owner_app.command("adopt")
def owner_adopt() -> None:
    from .owner import provision
    print(provision(load_settings(), initialize=False))

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", "-c", help="Optional config.yaml path."),
]
LimitOption = Annotated[int | None, typer.Option("--limit", "-l", help="Maximum records to process.")]


def _settings(config: Path | None = None) -> Settings:
    settings = load_settings(config)
    ensure_reserve_layout(settings)
    return settings


@app.command("import-folder")
def import_folder_command(
    root: Path,
    namespace: Annotated[str, typer.Option("--namespace")],
    mode: Annotated[str, typer.Option("--mode")] = "local",
    pipeline_limit: Annotated[int | None, typer.Option(
        "--pipeline-limit", help="Authorize configured processing, classification and embedding costs. "
        "Positive media-item bound; embeddings run regardless of automatic online sync setting."
    )] = None,
    config: ConfigOption = None,
) -> None:
    """Copy supported files into the reserve; default intake only, no providers."""
    from .folder_import import import_folder
    raise typer.Exit(import_folder(root, namespace, mode, pipeline_limit, config))


def _print_counts(title: str, counts: dict[str, Any]) -> None:
    rendered = ", ".join(
        f"{key}={value}" for key, value in counts.items() if isinstance(value, str | int | float)
    )
    console.print(f"[bold]{title}[/bold] {rendered}")


def _with_import_delay(settings: Settings, delay: float | None) -> Settings:
    if delay is None:
        return settings
    return replace(settings, import_delay_seconds=max(0.0, delay))


def _with_ai_overrides(
    settings: Settings,
    ai_delay: float | None = None,
    ai_max_retries: int | None = None,
) -> Settings:
    updates: dict[str, object] = {}
    if ai_delay is not None:
        updates["ai_request_delay_seconds"] = max(0.0, ai_delay)
    if ai_max_retries is not None:
        updates["ai_max_retries"] = max(0, ai_max_retries)
    return replace(settings, **updates) if updates else settings


def _pipeline_media_ids_from_import_report(import_report: object) -> list[int] | None:
    if not isinstance(import_report, dict):
        return None
    if "media_item_ids" not in import_report:
        return None
    media_item_ids = [int(value) for value in import_report.get("media_item_ids", [])]
    return media_item_ids


@contextmanager
def _sync_lock(settings: Settings):
    engine = get_engine(settings)
    connection = engine.connect()
    locked = False
    try:
        if settings.is_postgres:
            locked = bool(connection.execute(text("SELECT pg_try_advisory_lock(60340120260615)")).scalar())
            if not locked:
                console.print("[yellow]Another arsenal sync is already running; exiting.[/yellow]")
                raise typer.Exit(0)
        yield
    finally:
        if settings.is_postgres and locked:
            connection.execute(text("SELECT pg_advisory_unlock(60340120260615)"))
        connection.close()
        engine.dispose()


@app.command()
def init(config: ConfigOption = None) -> None:
    """Create Meme Arsenal Reserve directories and initialize the metadata schema."""

    settings = _settings(config)
    engine = get_engine(settings)
    ensure_schema(settings, engine)
    report = write_run_report(
        settings,
        "init",
        {
            "reserve_path": str(settings.reserve_path),
            "database_url": settings.database_url,
            "database": "postgresql" if settings.is_postgres else "sqlite",
        },
    )
    console.print(f"Initialized {RESERVE_NAME} at [bold]{settings.reserve_path}[/bold]")
    console.print(f"Run report: {report}")


@app.command("telegram-login")
def telegram_login(config: ConfigOption = None) -> None:
    """Log in to Telegram and store the session in the Meme Arsenal Reserve."""

    settings = _settings(config)
    telegram_login_sync(settings)
    console.print(f"Telegram session stored at [bold]{settings.telegram_session_path}[/bold]")


@app.command("import")
def import_command(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Telegram messages to scan.")] = 100,
    fixture_dir: Annotated[
        Path | None,
        typer.Option("--fixture-dir", help="Import local fixture media instead of Telegram."),
    ] = None,
    delay: Annotated[
        float | None,
        typer.Option(
            "--delay",
            help="Seconds to pause between Telegram import actions. Defaults to IMPORT_DELAY_SECONDS.",
        ),
    ] = None,
) -> None:
    """Import new Saved Messages media incrementally."""

    settings = _with_import_delay(_settings(config), delay)
    with session_scope(settings) as session:
        if fixture_dir:
            counts = import_fixture_messages(session, settings, fixture_dir, limit=limit)
        else:
            counts = import_saved_messages(session, settings, limit=limit)
        counts["telegram_post_url_backfill"] = backfill_telegram_post_urls(session)
        write_run_report(settings, "import", counts)
    _print_counts("Import", counts)


@app.command("backfill-telegram-post-urls")
def backfill_telegram_post_urls_command(config: ConfigOption = None) -> None:
    """Populate Telegram post URLs from stored raw Telegram metadata."""

    settings = _settings(config)
    with session_scope(settings) as session:
        counts = backfill_telegram_post_urls(session)
        write_run_report(settings, "backfill-telegram-post-urls", counts)
    _print_counts("Telegram post URL backfill", counts)


@app.command("process-media")
def process_media(config: ConfigOption = None, limit: LimitOption = None) -> None:
    """Generate previews, hover previews, thumbnails, hashes, keyframes, and duplicate groups."""

    settings = _settings(config)
    with session_scope(settings) as session:
        counts = process_pending_media(session, settings, limit=limit)
        write_run_report(settings, "process-media", counts)
    _print_counts("Process media", counts)


@app.command()
def classify(
    config: ConfigOption = None,
    limit: LimitOption = None,
    force: Annotated[bool, typer.Option("--force", help="Reclassify existing items.")] = False,
    ai_delay: Annotated[
        float | None,
        typer.Option(
            "--ai-delay",
            help="Seconds to pause between AI classification calls. Overrides AI_REQUEST_DELAY_SECONDS.",
        ),
    ] = None,
    ai_max_retries: Annotated[
        int | None,
        typer.Option("--ai-max-retries", help="Retries per AI request before recording a failure."),
    ] = None,
) -> None:
    """Classify processed memes with the configured AI provider."""

    settings = _with_ai_overrides(_settings(config), ai_delay=ai_delay, ai_max_retries=ai_max_retries)
    with session_scope(settings) as session:
        counts = classify_pending_media(session, settings, limit=limit, force=force)
        write_run_report(settings, "classify", counts)
    _print_counts("Classify", counts)


@app.command()
def embed(
    config: ConfigOption = None,
    limit: LimitOption = None,
    force: Annotated[bool, typer.Option("--force", help="Regenerate existing embeddings.")] = False,
) -> None:
    """Generate and store embeddings for semantic search."""

    settings = _settings(config)
    with session_scope(settings) as session:
        counts = embed_pending_media(session, settings, limit=limit, force=force)
        write_run_report(settings, "embed", counts)
    _print_counts("Embed", counts)


@app.command()
def pipeline(
    config: ConfigOption = None,
    limit: LimitOption = None,
    process_workers: Annotated[
        int,
        typer.Option("--process-workers", help="Concurrent local media processing workers."),
    ] = 6,
    classify_workers: Annotated[
        int,
        typer.Option("--classify-workers", help="Concurrent classification workers. Keep this low for Gemini free tier."),
    ] = 3,
    embed_workers: Annotated[
        int,
        typer.Option("--embed-workers", help="Concurrent embedding workers."),
    ] = 3,
    ai_delay: Annotated[
        float | None,
        typer.Option(
            "--ai-delay",
            help="Seconds to pause between AI classification calls. Overrides AI_REQUEST_DELAY_SECONDS.",
        ),
    ] = None,
    ai_max_retries: Annotated[
        int | None,
        typer.Option("--ai-max-retries", help="Retries per AI request before recording a failure."),
    ] = None,
) -> None:
    """Overlap process-media -> classify -> embed for the newest pending messages."""

    settings = _with_ai_overrides(_settings(config), ai_delay=ai_delay, ai_max_retries=ai_max_retries)
    counts = run_media_pipeline(
        settings,
        limit=limit,
        process_workers=process_workers,
        classify_workers=classify_workers,
        embed_workers=embed_workers,
    )
    write_run_report(settings, "pipeline", counts)
    _print_counts("Pipeline", counts)


@app.command("export-catalog")
def export_catalog_command(config: ConfigOption = None) -> None:
    """Export chunked static catalog JSON from the database."""

    settings = _settings(config)
    with session_scope(settings) as session:
        index = export_catalog(session, settings)
        write_run_report(settings, "export-catalog", {"item_count": index["item_count"]})
    console.print(f"Exported {index['item_count']} items to [bold]{settings.catalog_dir}[/bold]")


@app.command("build-dashboard")
def build_dashboard_command(config: ConfigOption = None) -> None:
    """Build the mobile-compatible static dashboard into the Meme Arsenal Reserve."""

    settings = _settings(config)
    result = build_dashboard(settings)
    write_run_report(settings, "build-dashboard", result)
    console.print(f"Dashboard built in [bold]{result['dist']}[/bold] using {result['mode']}")


@app.command("build-online-dashboard")
def build_online_dashboard_command(config: ConfigOption = None) -> None:
    """Build the dashboard and point it at the hosted FastAPI APIs."""

    settings = _settings(config)
    result = build_online_dashboard(settings)
    write_run_report(settings, "build-online-dashboard", result)
    console.print(f"Online dashboard built in [bold]{result['dist']}[/bold] using {result['mode']}")


@app.command("serve-dashboard")
def serve_dashboard_command(
    config: ConfigOption = None,
    host: Annotated[
        str,
        typer.Option("--host", help="Host interface to bind. Use 0.0.0.0 inside Docker/devcontainers."),
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", help="Port to serve the dashboard and remix API.")] = 8123,
) -> None:
    """Serve the dashboard plus the live server-side Remix API."""

    settings = _settings(config)
    serve_dashboard(settings, host=host, port=port)


@app.command("serve-online")
def serve_online_command(
    config: ConfigOption = None,
    host: Annotated[str, typer.Option("--host", help="Host interface to bind.")] = "0.0.0.0",
    port: Annotated[int, typer.Option("--port", help="Port for the FastAPI online app.")] = 8000,
) -> None:
    """Serve the production-oriented FastAPI public website API."""

    settings = _settings(config)
    import uvicorn

    from .online import create_app

    from .drain import DrainServer

    if os.environ.get('ARSENAL_COMMUNITY_DRAIN') == '1':
        DrainServer(uvicorn.Config(create_app(settings), host=host, port=port,
                                  timeout_graceful_shutdown=None,
                                  log_config=uvicorn_log_config())).run()
    else:
        uvicorn.run(create_app(settings), host=host, port=port, log_config=uvicorn_log_config())


@app.command("online-worker")
def online_worker_command(
    config: ConfigOption = None,
    poll_seconds: Annotated[float, typer.Option("--poll-seconds", help="DB fallback poll interval.")] = 3.0,
    once: Annotated[bool, typer.Option("--once", help="Process at most one queued job, then exit.")] = False,
) -> None:
    """Run the persistent public remix worker."""

    import signal
    from threading import Event
    from .online_worker import run_worker, _REJECTION_EXPLANATION_EXECUTOR

    stopped = Event()
    previous = {sig: signal.signal(sig, lambda *_: stopped.set())
                for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        run_worker(_settings(config), poll_seconds=poll_seconds, once=once, stop=stopped)
        # Join at process lifetime, not inside reusable run_worker(once=True).
        _REJECTION_EXPLANATION_EXECUTOR.shutdown(wait=True)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@app.command("ai-status")
def ai_status_command(config: ConfigOption = None) -> None:
    """Show the persisted Google credit circuit state without calling a model."""

    settings = _settings(config)
    _print_counts("Configured operations", _operation_context(settings))
    _print_counts("AI credit circuit", _circuit_payload(settings))


def _operation_context(settings: Settings) -> dict[str, object]:
    return {
        "classification": settings.effective_classification_provider,
        "embedding": settings.effective_embedding_provider,
        "remix": settings.effective_remix_provider,
        "online_embeddings_enabled": settings.online_embeddings_enabled,
        "classification_model": settings.ai_model,
        "embedding_model": settings.embedding_model,
        "remix_model": settings.remix_model,
    }


def _circuit_payload(settings: Settings) -> dict[str, object]:
    try:
        return ai_circuit_status(settings).payload()
    except Exception:
        return {"state": "unavailable", "google_paused": True}


@app.command("ai-resume")
def ai_resume_command(
    config: ConfigOption = None,
    operation: Annotated[str | None, typer.Option(
        "--operation", help="Configured Google operation: classification, embedding, or remix."
    )] = None,
) -> None:
    """Probe one configured Google operation and run bounded message recovery."""

    settings = _settings(config)
    with _sync_lock(settings):
        probe = probe_google_credit(settings, force=True, operation=operation)
        report: dict[str, object] = {"probe": probe.payload()}
        if probe.recovered:
            report["recovery_pipeline"] = run_media_pipeline(
                settings,
                limit=max(1, settings.ai_recovery_batch_size),
                process_workers=1,
                classify_workers=1,
                embed_workers=1,
                embed_enabled=settings.online_embeddings_enabled,
            )
        write_run_report(settings, "ai-resume", report)
    console.print(f"AI credit probe: {probe.outcome}")
    if probe.operation:
        console.print(f"Selected operation: {probe.operation}; model: {probe.model}")
    if probe.recovered and not settings.online_embeddings_enabled:
        console.print("Online embeddings are disabled; rerun local embedding work explicitly.")
    if isinstance(report.get("recovery_pipeline"), dict):
        _print_counts("Recovery pipeline", report["recovery_pipeline"])


@app.command("refresh-public-indexes")
def refresh_public_indexes_command(config: ConfigOption = None) -> None:
    """Refresh normalized public tags and search documents from current archive data."""

    from .online import refresh_public_indexes

    settings = _settings(config)
    with session_scope(settings) as session:
        counts = refresh_public_indexes(session, settings=settings)
        write_run_report(settings, "refresh-public-indexes", counts)
    _print_counts("Public index refresh", counts)


@app.command("warm-browser-playback")
def warm_browser_playback_command(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Maximum videos to transcode.")] = 12,
) -> None:
    """Prepare recent public videos for mobile browser playback."""

    settings = _settings(config)
    counts = warm_browser_playback(settings, limit=limit)
    _print_counts("Browser playback", counts)


@app.command("cleanup-online-state")
def cleanup_online_state_command(
    config: ConfigOption = None,
    job_retention_days: Annotated[
        int,
        typer.Option("--job-retention-days", min=1, help="Age for failed remix job removal."),
    ] = 90,
    draft_retention_days: Annotated[
        int,
        typer.Option("--draft-retention-days", min=1, help="Age for orphan remix draft removal."),
    ] = 14,
    apply: Annotated[bool, typer.Option("--apply", help="Delete the reported records and files.")] = False,
) -> None:
    """Report or remove stale failed remix jobs and orphan draft files."""

    settings = _settings(config)
    counts = cleanup_online_state(
        settings,
        job_retention_days=job_retention_days,
        draft_retention_days=draft_retention_days,
        apply=apply,
    )
    write_run_report(settings, "cleanup-online-state", counts)
    _print_counts("Online cleanup", counts)


@app.command("repair-media-references")
def repair_media_references(config: ConfigOption = None, limit: LimitOption = None) -> None:
    """Hide missing originals and queue missing derivatives for regeneration."""

    from .online import refresh_public_indexes

    settings = _settings(config)
    with session_scope(settings) as session:
        counts = repair_missing_media_references(session, settings, limit=limit)
        index_counts = refresh_public_indexes(session, settings=settings)
        counts.update({f"public_indexes_{key}": value for key, value in index_counts.items()})
        write_run_report(settings, "repair-media-references", counts)
    _print_counts("Media reference repair", counts)


@app.command("migrate-sqlite-to-postgres")
def migrate_sqlite_to_postgres_command(
    config: ConfigOption = None,
    source_sqlite: Annotated[
        Path | None,
        typer.Option("--source-sqlite", help="Source SQLite database. Defaults to RESERVE_PATH/data/arsenal.sqlite."),
    ] = None,
    promote_static_visible_drafts: Annotated[
        bool,
        typer.Option(
            "--promote-static-visible-drafts",
            help="Publish existing processed dashboard-upload drafts that were visible in the old static dashboard.",
        ),
    ] = False,
    force: Annotated[bool, typer.Option("--force", help="Delete existing target model rows before migration.")] = False,
    allow_empty_source: Annotated[
        bool,
        typer.Option(
            "--allow-empty-source",
            help="Allow migration from an empty sqlite source when using --force.",
        ),
    ] = False,
) -> None:
    """Copy the archived SQLite reserve database into an empty PostgreSQL database."""

    settings = _settings(config)
    source = source_sqlite or (settings.reserve_path / "data" / "arsenal.sqlite")
    counts = migrate_sqlite_to_postgres(
        settings,
        source_sqlite=source,
        promote_static_visible_drafts=promote_static_visible_drafts,
        force=force,
        allow_empty_source=allow_empty_source,
    )
    from .online import refresh_public_indexes

    with session_scope(settings) as session:
        index_counts = refresh_public_indexes(session, settings=settings)
    counts.update({f"public_indexes_{key}": value for key, value in index_counts.items()})
    write_run_report(settings, "migrate-sqlite-to-postgres", counts)
    _print_counts("SQLite to PostgreSQL migration", counts)


@app.command()
def search(
    query: Annotated[str, typer.Argument(help="Natural-language search query.")],
    config: ConfigOption = None,
    top_k: Annotated[int | None, typer.Option("--top-k", help="Number of hits to return.")] = None,
    export: Annotated[bool, typer.Option("--export/--no-export", help="Write static result bundle.")] = True,
) -> None:
    """Run hybrid lexical and embedding cosine-similarity search."""

    settings = _settings(config)
    with session_scope(settings) as session:
        hits = search_media(session, settings, query, top_k=top_k)
        result_path = export_search_results(session, settings, query, top_k=top_k) if export else None
        write_run_report(
            settings,
            "search",
            {
                "query": query,
                "hits": [hit.__dict__ for hit in hits],
                "result_path": str(result_path) if result_path else None,
            },
        )
    for hit in hits:
        console.print(
            f"#{hit.media_item_id} score={hit.score:.3f} "
            f"semantic={hit.semantic_score:.3f} lexical={hit.lexical_score:.3f} {hit.explanation}"
        )
    if result_path:
        console.print(f"Search result bundle: {result_path}")


@app.command("sync-online")
def sync_online(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Recent Telegram messages to scan.")] = 100,
    fixture_dir: Annotated[
        Path | None,
        typer.Option("--fixture-dir", help="Import fixture media instead of Telegram."),
    ] = None,
    skip_import: Annotated[bool, typer.Option("--skip-import", help="Process existing DB rows only.")] = False,
    delay: Annotated[
        float | None,
        typer.Option(
            "--delay",
            help="Seconds to pause between Telegram import actions. Defaults to IMPORT_DELAY_SECONDS.",
        ),
    ] = None,
    ai_delay: Annotated[
        float | None,
        typer.Option(
            "--ai-delay",
            help="Seconds to pause between AI classification calls. Overrides AI_REQUEST_DELAY_SECONDS.",
        ),
    ] = None,
    ai_max_retries: Annotated[
        int | None,
        typer.Option("--ai-max-retries", help="Retries per AI request before recording a failure."),
    ] = None,
    process_workers: Annotated[
        int,
        typer.Option("--process-workers", help="Concurrent local media processing workers."),
    ] = 6,
    classify_workers: Annotated[
        int,
        typer.Option("--classify-workers", help="Concurrent classification workers. Keep this low for Gemini free tier."),
    ] = 3,
    embed_workers: Annotated[
        int,
        typer.Option("--embed-workers", help="Concurrent embedding workers."),
    ] = 3,
) -> None:
    """Run the live-service sync without writing static catalog/dashboard data."""

    settings = _with_ai_overrides(
        _with_import_delay(_settings(config), delay),
        ai_delay=ai_delay,
        ai_max_retries=ai_max_retries,
    )
    from .online import refresh_public_indexes

    with _sync_lock(settings):
        report: dict[str, object] = {}
        imported_media_ids: list[int] | None = None
        with session_scope(settings) as session:
            if not skip_import:
                if fixture_dir:
                    report["import"] = import_fixture_messages(session, settings, fixture_dir, limit=limit)
                else:
                    report["import"] = import_saved_messages(
                        session,
                        settings,
                        limit=limit,
                        backfill_older=False,
                    )
                if isinstance(report["import"], dict):
                    report["import"]["telegram_post_url_backfill"] = backfill_telegram_post_urls(session)
                    imported_media_ids = _pipeline_media_ids_from_import_report(report["import"])

        with session_scope(settings) as session:
            media_repair = repair_missing_media_references(session, settings)
        report["media_repair"] = media_repair
        repaired_media_ids = _pipeline_media_ids_from_import_report(media_repair) or []

        circuit = None
        probe = None
        if "google" in (settings.effective_classification_provider,
                         settings.effective_embedding_provider, settings.effective_remix_provider):
            try:
                circuit = ai_circuit_status(settings)
            except Exception:
                circuit = None
            if circuit and circuit.is_open:
                probe = probe_google_credit(settings)
                if probe.outcome != "disabled":
                    circuit = probe.circuit
            report["ai_credit_probe"] = probe.payload() if probe else {
                "outcome": "not_needed" if circuit is not None else "unavailable",
                "request_made": False,
                "recovered": False,
            }
            report["ai_circuit"] = circuit.payload() if circuit else {"state": "unavailable", "google_paused": True}
            report["configured_operations"] = _operation_context(settings)

        recovering = bool(probe and probe.recovered)
        pipeline_limit = max(1, settings.ai_recovery_batch_size) if recovering else limit
        changed_media_ids = sorted(set(imported_media_ids or []) | set(repaired_media_ids))
        pipeline_media_ids = None if recovering or skip_import else changed_media_ids
        report["pipeline"] = run_media_pipeline(
            settings,
            limit=pipeline_limit,
            media_ids=pipeline_media_ids,
            process_workers=1 if recovering else process_workers,
            classify_workers=1 if recovering else classify_workers,
            embed_workers=1 if recovering else embed_workers,
            embed_enabled=settings.online_embeddings_enabled,
        )
        if circuit is not None:
            report["ai_circuit"] = _circuit_payload(settings)
        report["browser_playback"] = warm_browser_playback(
            settings,
            limit=settings.browser_playback_backfill_batch_size,
        )
        pipeline_report = report["pipeline"]
        if isinstance(pipeline_report, dict):
            changed_media_ids = sorted(
                set(changed_media_ids)
                | set(_pipeline_media_ids_from_import_report(pipeline_report) or [])
            )
        with session_scope(settings) as session:
            report["public_indexes"] = refresh_public_indexes(
                session,
                changed_media_ids,
                settings=settings,
            )
        write_run_report(settings, "sync-online", report)
    console.print("Online sync complete.")
    for key, value in report.items():
        if isinstance(value, dict):
            _print_counts(key.replace("_", " ").title(), value)


@app.command()
def sync(
    config: ConfigOption = None,
    limit: Annotated[int, typer.Option("--limit", "-l", help="Telegram messages to scan.")] = 100,
    fixture_dir: Annotated[
        Path | None,
        typer.Option("--fixture-dir", help="Import fixture media instead of Telegram."),
    ] = None,
    skip_import: Annotated[bool, typer.Option("--skip-import", help="Process existing DB rows only.")] = False,
    delay: Annotated[
        float | None,
        typer.Option(
            "--delay",
            help="Seconds to pause between Telegram import actions. Defaults to IMPORT_DELAY_SECONDS.",
        ),
    ] = None,
    ai_delay: Annotated[
        float | None,
        typer.Option(
            "--ai-delay",
            help="Seconds to pause between AI classification calls. Overrides AI_REQUEST_DELAY_SECONDS.",
        ),
    ] = None,
    ai_max_retries: Annotated[
        int | None,
        typer.Option("--ai-max-retries", help="Retries per AI request before recording a failure."),
    ] = None,
    process_workers: Annotated[
        int,
        typer.Option("--process-workers", help="Concurrent local media processing workers."),
    ] = 6,
    classify_workers: Annotated[
        int,
        typer.Option("--classify-workers", help="Concurrent classification workers. Keep this low for Gemini free tier."),
    ] = 3,
    embed_workers: Annotated[
        int,
        typer.Option("--embed-workers", help="Concurrent embedding workers."),
    ] = 3,
) -> None:
    """Run import -> URL backfill -> process-media -> classify -> embed."""

    settings = _with_ai_overrides(
        _with_import_delay(_settings(config), delay),
        ai_delay=ai_delay,
        ai_max_retries=ai_max_retries,
    )
    from .online import refresh_public_indexes

    with _sync_lock(settings):
        report: dict[str, object] = {}
        imported_media_ids: list[int] | None = None
        with session_scope(settings) as session:
            if not skip_import:
                if fixture_dir:
                    report["import"] = import_fixture_messages(session, settings, fixture_dir, limit=limit)
                else:
                    report["import"] = import_saved_messages(session, settings, limit=limit)
                if isinstance(report["import"], dict):
                    report["import"]["telegram_post_url_backfill"] = backfill_telegram_post_urls(session)
                import_report = report["import"]
                if isinstance(import_report, dict):
                    imported_media_ids = _pipeline_media_ids_from_import_report(import_report)

        report["pipeline"] = run_media_pipeline(
            settings,
            limit=limit,
            media_ids=imported_media_ids,
            process_workers=process_workers,
            classify_workers=classify_workers,
            embed_workers=embed_workers,
        )
        with session_scope(settings) as session:
            report["public_indexes"] = refresh_public_indexes(session, settings=settings)
            report["catalog"] = export_catalog(session, settings)
        report["dashboard"] = build_dashboard(settings)
        write_run_report(settings, "sync", report)
    console.print("Sync complete.")
    for key, value in report.items():
        if isinstance(value, dict):
            _print_counts(key.replace("_", " ").title(), value)


def _run(command: list[str]) -> None:
    console.print(f"[bold]$ {' '.join(command)}[/bold]")
    raise typer.Exit(subprocess.run(command, check=False).returncode)


@app.command("test-backend")
def test_backend() -> None:
    """Run backend tests."""

    _run([sys.executable, "-m", "pytest", "tests"])


@app.command("test-frontend")
def test_frontend() -> None:
    """Run dashboard build checks."""

    if not Path("frontend/dashboard/package.json").exists():
        console.print("Frontend package is missing.")
        raise typer.Exit(1)
    if not shutil_which("npm"):
        console.print("npm is not available in this shell; run inside the devcontainer.")
        raise typer.Exit(0)
    _run(["npm", "--prefix", "frontend/dashboard", "run", "build"])


@app.command("test-chromium")
def test_chromium() -> None:
    """Run browser-backed dashboard tests in Chromium."""

    script = Path("scripts/run_chromium_tests.sh")
    if not script.exists():
        fallback = Path(__file__).resolve().parents[2] / "scripts" / "run_chromium_tests.sh"
        if not fallback.exists():
            console.print("Chromium test helper script is missing.")
            raise typer.Exit(1)
        script = fallback
    _run(["bash", str(script)])


@app.command("test-e2e")
def test_e2e() -> None:
    """Run the fixture-based end-to-end smoke test."""

    _run([sys.executable, "-m", "pytest", "tests/test_e2e.py"])


@app.command("test")
def test_all() -> None:
    """Run the full available test suite."""

    _run([sys.executable, "-m", "pytest", "tests"])


def shutil_which(name: str) -> str | None:
    from shutil import which

    return which(name)


if __name__ == "__main__":
    app()
