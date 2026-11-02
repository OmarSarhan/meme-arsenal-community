"""Disposable OMA27 worker entry point; explicit fixture settings only."""
import json
from pathlib import Path
import socket
import sys
import time
from dataclasses import replace

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine


def install_stubs(patch, log, response, mode):
    from meme_arsenal import ai, online, online_worker, remix

    def record(value):
        with log.open("a") as stream:
            stream.write(value + "\n")
            stream.flush()

    def forbidden(*args, **kwargs):
        record("UNEXPECTED outbound")
        raise AssertionError("unexpected outbound operation")

    def generate(*args, **kwargs):
        record("generation " + mode)
        if mode == "blocked":
            log.with_suffix(".barrier").touch()
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(0.05)
            raise AssertionError("parent did not kill disposable worker")
        assert mode == "generate", "ready/saved restart invoked generation"
        return response

    classify = ai.classify_media_item

    def mock_classify(db, settings, item, **kwargs):
        return classify(db, replace(settings, ai_provider="mock"), item, **kwargs)

    for module in (ai, online, remix):
        patch(module, "classify_media_item", mock_classify)
    patch(remix, "post_google_model", generate)
    patch(online, "enqueue_remix_job", lambda *_: None)
    patch(online_worker, "_pop_redis_job", lambda *_: None)
    patch(online, "analyze_remix_tweaks", forbidden)
    patch(remix, "embed_media_item", forbidden)
    patch(socket.socket, "connect", forbidden)
    patch(socket.socket, "connect_ex", forbidden)


def main():
    from meme_arsenal.config import Settings
    payload = json.loads(Path(sys.argv[1]).read_text())
    def limits(connection, _record):
        with connection.cursor() as cursor:
            cursor.execute("SET statement_timeout = '10s'")
            cursor.execute("SET lock_timeout = '5s'")
        connection.commit()
    event.listen(Engine, "connect", limits)
    # No application/session initialization before proving isolated search_path.
    engine = create_engine(payload["settings"]["database_url"])
    with engine.connect() as connection:
        assert connection.scalar(text("select current_schema()")) == payload["schema"]
        assert connection.scalar(text("show search_path")) == payload["schema"] + ",public"
    engine.dispose()
    values = payload["settings"]
    values["reserve_path"] = Path(values["reserve_path"])
    assert values["reserve_path"].is_absolute()
    settings = Settings(**values)
    assert settings.owner_session_id == "fixture-owner"
    install_stubs(setattr, Path(payload["log"]), payload["response"], sys.argv[2])
    from meme_arsenal.online_worker import run_worker
    run_worker(settings, once=True)


if __name__ == "__main__":
    main()
