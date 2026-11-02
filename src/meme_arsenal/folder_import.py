"""Sequential folder intake with an explicitly authorized, bounded follow-up."""
from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import sys
import tempfile

from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .config import RESERVE_DIRS, Settings, ensure_reserve_layout, load_settings
from .db import ensure_schema, get_engine
from .ingestion import CommitUncertain, SourceRecord, ingest_local_record
from .media import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
from .owner import require_owner
from .pipeline import run_media_pipeline, select_pipeline_media_ids


def preflight(settings: Settings, root: Path, namespace: str, mode: str,
              pipeline_limit: int | None) -> list[str]:
    if (not namespace or "\0" in namespace or len(namespace.encode("utf-8")) > 128
            or mode not in ("local", "hosted")
            or (pipeline_limit is not None and pipeline_limit <= 0)):
        raise ValueError
    if root.is_symlink() or not root.is_dir():
        raise ValueError
    source = root.resolve(strict=True)
    destinations = [settings.reserve_path / relative for relative in RESERVE_DIRS]
    destinations += [settings.reserve_path, settings.run_logs_dir,
                     settings.reserve_path / "originals" / "folder"]
    url = make_url(settings.database_url)
    if url.get_backend_name() == "sqlite":
        # SQLite URI filenames have different path/escaping semantics; this
        # command supports ordinary SQLAlchemy filesystem URLs only.
        if "uri" in url.query:
            raise ValueError
        memory = not url.database or url.database == ":memory:"
        if memory and pipeline_limit is not None:
            raise ValueError
        if not memory:
            destinations.append(Path(url.database))
    for destination in destinations:
        resolved = destination.resolve()
        if resolved.is_relative_to(source) or source.is_relative_to(resolved):
            raise ValueError
    if pipeline_limit is not None:
        visited: set[Path] = set()
        def check_derivatives(directory: Path) -> None:
            resolved = directory.resolve()
            if resolved.is_relative_to(source) or source.is_relative_to(resolved):
                raise ValueError
            if resolved in visited or not directory.is_dir():
                return
            visited.add(resolved)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.is_symlink() or entry.is_dir(follow_symlinks=False):
                        check_derivatives(Path(entry.path))
        for directory in (settings.thumbnails_dir, settings.previews_dir,
                          settings.video_previews_dir, settings.video_playback_dir,
                          settings.keyframes_dir):
            check_derivatives(directory)
    keys = []
    # scandir errors propagate; a partial enumeration must never become success.
    def visit(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise ValueError
                info = entry.stat(follow_symlinks=False)
                path = Path(entry.path)
                if stat.S_ISDIR(info.st_mode):
                    visit(path)
                elif not stat.S_ISREG(info.st_mode):
                    raise ValueError
                elif path.suffix.lower() in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
                    key = path.relative_to(root).as_posix()
                    if len(key.encode("utf-8")) > 1024:
                        raise ValueError
                    keys.append(key)
    visit(root)
    return sorted(keys)


def write_report(settings: Settings, report: dict) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="folder-import-", suffix=".json",
                                        dir=settings.run_logs_dir)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=True)
        handle.write("\n")
    return Path(name)


def import_folder(root: Path, namespace: str, mode: str, pipeline_limit: int | None,
                  config: Path | None) -> int:
    report: dict = {"items": [], "media_item_ids": [], "validated_media_item_ids": [],
                    "reasons": [], "pipeline": "not-requested", "refresh": "not-requested"}
    engine = None
    settings = None
    code = 0
    phase = "configuration"
    initialized = False
    in_flight = None
    try:
        settings = load_settings(config)
        phase = "preflight"
        keys = preflight(settings, root, namespace, mode, pipeline_limit)
        ensure_reserve_layout(settings)
        phase = "schema"
        engine = get_engine(settings)
        ensure_schema(settings, engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as session:
            owner = require_owner(settings, session) if mode == "hosted" else None
        initialized = True
        phase = "intake"
        for key in keys:
            in_flight = key
            try:
                result = ingest_local_record(factory, settings, root, SourceRecord(namespace, key),
                                             mode=mode, owner_id=owner)
                status = result.status
                if status == "created":
                    report["media_item_ids"].append(result.media_item_id)
                if status in ("created", "unchanged"):
                    report["validated_media_item_ids"].append(result.media_item_id)
                else:
                    code = 1
            except CommitUncertain:
                report["items"].append({"key": key, "status": "uncertain"})
                report["reasons"].append("uncertain")
                code = 1
                break
            except Exception:
                status = "failed"
                code = 1
                report["reasons"].append("intake")
            report["items"].append({"key": key, "status": status})
            in_flight = None
        else:
            if pipeline_limit is not None:
                phase = "pipeline"
                scope = list(dict.fromkeys(report["validated_media_item_ids"]))
                try:
                    with factory() as session:
                        pending = set(select_pipeline_media_ids(session, settings,
                                                               include_embeddings=True))
                    eligible = [item for item in scope if item in pending]
                    selected = eligible[:pipeline_limit]
                    report.update(eligible=len(eligible), selected=len(selected),
                                  deferred=len(eligible) - len(selected))
                    result = run_media_pipeline(settings, limit=None, media_ids=selected,
                                                process_workers=1, classify_workers=1,
                                                embed_workers=1, embed_enabled=True)
                    counters = ("processed", "process_skipped", "process_failed", "classified",
                                "classify_blocked", "classify_skipped", "classify_failed",
                                "embedded", "embed_skipped", "embed_failed")
                    flags = ("ai_paused", "classification_provider_paused",
                             "embedding_provider_paused", "stopped_early")
                    report["stages"] = {key: result[key] for key in counters + flags
                                        if type(result.get(key)) in (int, bool)}
                    failed = any(result.get(key) for key in
                                 ("process_failed", "classify_failed", "embed_failed", "stopped_early"))
                    if selected and any(result.get(key) for key in flags[:-1]):
                        # A pause on an irrelevant provider is harmless if all
                        # requested items have reached their terminal state.
                        with factory() as session:
                            remaining = set(select_pipeline_media_ids(
                                session, settings, include_embeddings=True))
                        failed |= bool(remaining.intersection(selected))
                    report["pipeline"] = "failed" if failed else "completed"
                    if failed:
                        code = code or 3
                        report["reasons"].append("pipeline")
                except Exception:
                    report["pipeline"] = "unknown"
                    report["refresh"] = "deferred"
                    report["reasons"].append("pipeline")
                    code = code or 3
                else:
                    phase = "refresh"
                    try:
                        from .online import refresh_public_indexes
                        with factory.begin() as session:
                            refresh_public_indexes(session, settings=settings, media_item_ids=scope)
                        report["refresh"] = "completed"
                    except Exception:
                        report["refresh"] = "failed"
                        report["reasons"].append("refresh")
                        code = code or 3
    except KeyboardInterrupt:
        code = 130
        report["reasons"].append("interrupted")
        if in_flight is not None:
            report["items"].append({"key": in_flight, "status": "unknown"})
    except Exception:
        code = 2 if not initialized else (code or 3)
        report["reasons"].append(phase)
    finally:
        if engine is not None:
            engine.dispose()
    if initialized and settings is not None:
        try:
            report["exit_code"] = code
            write_report(settings, report)
        except Exception:
            code = code or 3
            print("folder-import: report-write failed", file=sys.stderr)
    print(f"folder-import: exit={code}")
    return code
