from dataclasses import replace

import pytest

from meme_arsenal import cli, pipeline, online_worker as worker
from meme_arsenal.ai_circuit import open_ai_credit_circuit
from meme_arsenal.db import session_scope
from meme_arsenal.google_api import GeminiCreditCircuitOpenError, GeminiCreditProbeResult
from test_embedding_execution import establish


@pytest.mark.parametrize("compatible", [True, False])
@pytest.mark.parametrize("unavailable", [False, True])
def test_paused_classification_uses_authoritative_embedding_input(settings, monkeypatch, compatible, unavailable):
    google = replace(settings, classification_provider="google", embedding_provider="mock")
    # Establish actual Google identity through the synthetic provider boundary.
    from meme_arsenal import ai
    monkeypatch.setattr(ai, "classify_with_google", lambda *a, **k: (ai.ClassificationPayload(summary="known"), {}))
    with session_scope(google) as db:
        message, item, _ = establish(db, google, monkeypatch)
        from meme_arsenal.media import path_sha256, reserve_absolute
        original = google.reserve_path / "original.jpg"
        original.write_bytes(reserve_absolute(google, item.preview_path).read_bytes())
        item.original_path = original.relative_to(google.reserve_path).as_posix()
        item.sha256 = path_sha256(original)
        item_id = item.id
        if not compatible:
            message.caption = "changed source"
    if unavailable:
        from meme_arsenal import google_api
        monkeypatch.setattr(google_api, "ai_circuit_status", lambda _: (_ for _ in ()).throw(RuntimeError()))
        monkeypatch.setattr(google_api, "open_ai_credit_circuit", lambda _: pytest.fail("new depletion"))
    else:
        open_ai_credit_circuit(google)
    monkeypatch.setattr(ai, "classify_with_google", lambda *a, **k: pytest.fail("paused classification"))
    report = pipeline.run_media_pipeline(google, media_ids=[item_id], process_workers=1,
                                        classify_workers=1, embed_workers=1)
    assert report["classify_skipped"] == 1
    assert report["embedded"] == int(compatible)
    assert report["embed_skipped"] == int(not compatible)


def test_status_override_and_unavailable_make_no_probe(settings, monkeypatch):
    google = replace(settings, ai_provider="mock", remix_provider="google")
    monkeypatch.setattr(cli, "_settings", lambda _: google)
    monkeypatch.setattr(cli, "probe_google_credit", lambda *a, **k: pytest.fail("status probe"))
    monkeypatch.setattr(cli, "ai_circuit_status", lambda _: (_ for _ in ()).throw(RuntimeError()))
    assert cli._operation_context(google)["remix"] == "google"
    assert cli._circuit_payload(google)["state"] == "unavailable"
    cli.ai_status_command()


@pytest.mark.parametrize("recovered", [True, False])
def test_manual_probe_recovery_bounds(settings, monkeypatch, recovered):
    from contextlib import nullcontext
    settings = replace(settings, online_embeddings_enabled=False, ai_recovery_batch_size=2)
    monkeypatch.setattr(cli, "_settings", lambda _: settings)
    monkeypatch.setattr(cli, "_sync_lock", lambda _: nullcontext())
    calls = []
    def probe(s, **kwargs):
        assert kwargs == {"force": True, "operation": "embedding"}
        return GeminiCreditProbeResult("recovered" if recovered else "unavailable", True, recovered, None,
                                      operation="embedding", model="test")
    monkeypatch.setattr(cli, "probe_google_credit", probe)
    monkeypatch.setattr(cli, "run_media_pipeline", lambda s, **kw: calls.append(kw) or {})
    monkeypatch.setattr(cli, "write_run_report", lambda *a: None)
    cli.ai_resume_command(operation="embedding")
    assert len(calls) == int(recovered)
    if recovered:
        assert calls[0] == dict(limit=2, process_workers=1, classify_workers=1,
                                embed_workers=1, embed_enabled=False)


def test_worker_denial_does_not_open_closed_circuit(settings, monkeypatch):
    monkeypatch.setattr(worker, "open_ai_credit_circuit", lambda *a: pytest.fail("fabricated depletion"))
    result = worker._record_credit_pause(settings, "missing", None,
        GeminiCreditCircuitOpenError("state unavailable", status="CREDIT_CIRCUIT_UNAVAILABLE"))
    assert result == {"status": "queued", "paused": True}


@pytest.mark.parametrize("outcome", ["unavailable", "stale", "recovered", "normal"])
def test_sync_probe_scope_and_unavailable_isolation(settings, monkeypatch, outcome):
    from contextlib import nullcontext
    from meme_arsenal import online
    settings = replace(settings, classification_provider="mock", remix_provider="google",
                       online_embeddings_enabled=False, ai_recovery_batch_size=3)
    monkeypatch.setattr(cli, "_settings", lambda _: settings)
    monkeypatch.setattr(cli, "_sync_lock", lambda _: nullcontext())
    if outcome == "unavailable":
        monkeypatch.setattr(cli, "ai_circuit_status", lambda _: (_ for _ in ()).throw(RuntimeError()))
        monkeypatch.setattr(cli, "probe_google_credit", lambda *a, **k: pytest.fail("unavailable status"))
    else:
        status = open_ai_credit_circuit(settings)
        monkeypatch.setattr(cli, "ai_circuit_status", lambda _: status)
        monkeypatch.setattr(cli, "probe_google_credit", lambda *a, **k:
                            GeminiCreditProbeResult(outcome, False, outcome == "recovered", None))
    monkeypatch.setattr(cli, "import_fixture_messages", lambda *a, **k: {"media_item_ids": []})
    monkeypatch.setattr(cli, "backfill_telegram_post_urls", lambda *a: {})
    monkeypatch.setattr(cli, "repair_missing_media_references", lambda *a: {"media_item_ids": []})
    monkeypatch.setattr(cli, "warm_browser_playback", lambda *a, **k: {})
    monkeypatch.setattr(online, "refresh_public_indexes", lambda *a, **k: {})
    calls = []
    monkeypatch.setattr(cli, "run_media_pipeline", lambda *a, **k: calls.append(k) or {})
    monkeypatch.setattr(cli, "write_run_report", lambda *a: None)
    cli.sync_online(fixture_dir=settings.reserve_path)
    assert calls[0].get("ai_paused", False) is False
    assert calls[0]["embed_enabled"] is False
    assert calls[0]["media_ids"] == (None if outcome == "recovered" else [])
    assert calls[0]["limit"] == (3 if outcome == "recovered" else 100)
    assert calls[0]["classify_workers"] == (1 if outcome == "recovered" else 3)


def test_guidance_stale_selected_by_configured_recovery(settings, monkeypatch):
    from contextlib import nullcontext
    from meme_arsenal import ai, online
    configured = replace(settings, classification_style_guidance="new nonsecret style",
                         remix_provider="google", online_embeddings_enabled=False, ai_recovery_batch_size=1)
    with session_scope(settings) as db:
        _, item, row = establish(db, settings, monkeypatch)
        item.original_path = item.preview_path
        from meme_arsenal.media import path_sha256, reserve_absolute
        item.sha256 = path_sha256(reserve_absolute(settings, item.original_path))
        db.flush()
        row = ai.classify_media_item(db, settings, item, force=True)
        item_id = item.id
        old_key = row.compatibility_key
        assert ai.classification_decision(db, configured, item)[0] == "execute"
        assert row.compatibility_key == old_key
    monkeypatch.setattr(cli, "_settings", lambda _: configured)
    monkeypatch.setattr(cli, "_sync_lock", lambda _: nullcontext())
    monkeypatch.setattr(cli, "ai_circuit_status", lambda _: open_ai_credit_circuit(configured))
    monkeypatch.setattr(cli, "probe_google_credit", lambda *a, **k:
                        GeminiCreditProbeResult("recovered", False, True, None))
    monkeypatch.setattr(cli, "import_fixture_messages", lambda *a, **k: {"media_item_ids": []})
    monkeypatch.setattr(cli, "backfill_telegram_post_urls", lambda *a: {})
    monkeypatch.setattr(cli, "repair_missing_media_references", lambda *a: {"media_item_ids": []})
    monkeypatch.setattr(cli, "warm_browser_playback", lambda *a, **k: {})
    monkeypatch.setattr(online, "refresh_public_indexes", lambda *a, **k: {})
    monkeypatch.setattr(cli, "write_run_report", lambda *a: None)
    calls = []
    def selected(config, **kwargs):
        calls.append(kwargs)
        with session_scope(config) as db:
            assert pipeline.select_pipeline_media_ids(db, config, limit=kwargs["limit"],
                media_ids=kwargs["media_ids"], include_embeddings=kwargs["embed_enabled"]) == [item_id]
        return {}
    monkeypatch.setattr(cli, "run_media_pipeline", selected)
    assert calls == []
    cli.sync_online(fixture_dir=settings.reserve_path)
    assert len(calls) == 1
    assert calls[0]["media_ids"] is None
    assert calls[0]["limit"] == 1
    assert calls[0]["embed_enabled"] is False


@pytest.mark.parametrize("scenario", ["unavailable", "disabled", "midrun", "completion_unknown"])
def test_sync_reports_observed_circuit_state(settings, monkeypatch, scenario):
    from contextlib import nullcontext
    from meme_arsenal import online
    from meme_arsenal.ai_circuit import ai_circuit_status

    settings = replace(settings, classification_provider="google", remix_provider="mock",
                       embedding_provider="mock", online_embeddings_enabled=False)
    if scenario == "disabled":
        settings = replace(settings, classification_provider="mock", embedding_provider="google")
    if scenario in {"disabled", "completion_unknown"}:
        open_ai_credit_circuit(settings)
    reads = []
    def status(config):
        reads.append(True)
        if scenario == "unavailable":
            raise RuntimeError("synthetic status read failure")
        return ai_circuit_status(config)
    monkeypatch.setattr(cli, "ai_circuit_status", status)
    if scenario == "completion_unknown":
        monkeypatch.setattr(cli, "probe_google_credit", lambda *a, **k:
                            GeminiCreditProbeResult("unavailable", True, False, None))
    monkeypatch.setattr(cli, "_settings", lambda _: settings)
    monkeypatch.setattr(cli, "_sync_lock", lambda _: nullcontext())
    monkeypatch.setattr(cli, "import_fixture_messages", lambda *a, **k: {"media_item_ids": []})
    monkeypatch.setattr(cli, "backfill_telegram_post_urls", lambda *a: {})
    monkeypatch.setattr(cli, "repair_missing_media_references", lambda *a: {"media_item_ids": []})
    monkeypatch.setattr(cli, "warm_browser_playback", lambda *a, **k: {})
    monkeypatch.setattr(online, "refresh_public_indexes", lambda *a, **k: {})
    def pipeline(config, **kwargs):
        assert kwargs["media_ids"] == []
        if scenario == "midrun":
            open_ai_credit_circuit(config)
        return {}
    monkeypatch.setattr(cli, "run_media_pipeline", pipeline)
    reports = []
    monkeypatch.setattr(cli, "write_run_report", lambda s, name, report: reports.append(report))
    cli.sync_online(fixture_dir=settings.reserve_path)
    report = reports[0]
    assert report["ai_credit_probe"]["outcome"] == {
        "unavailable": "unavailable", "disabled": "disabled",
        "midrun": "not_needed", "completion_unknown": "unavailable",
    }[scenario]
    assert report["ai_circuit"]["state"] == (
        "unavailable" if scenario in {"unavailable", "completion_unknown"} else "open")
    assert not report["ai_credit_probe"]["recovered"]
    if scenario in {"unavailable", "completion_unknown"}:
        assert len(reads) == 1


@pytest.mark.parametrize("google_stage", ["classification", "embedding"])
def test_midrun_credit_pause_isolates_provider(settings, fixture_dir, monkeypatch, google_stage):
    from threading import Event
    from meme_arsenal import ai, embeddings, google_api
    from meme_arsenal.media import process_pending_media
    from meme_arsenal.telegram_importer import import_fixture_messages
    from meme_arsenal.google_api import GeminiRateLimitError
    # Build real compatible input before triggering the forced refresh.
    monkeypatch.setattr(ai, "extract_ocr_text", lambda *a: "")
    monkeypatch.setattr(ai, "classify_with_google", lambda *a: (ai.ClassificationPayload(summary="known"), {}))
    configured = replace(settings, ai_stop_on_rate_limit=False,
                         classification_provider="google" if google_stage == "classification" else "mock",
                         embedding_provider="google" if google_stage == "embedding" else "mock")
    with session_scope(configured) as db:
        imported = import_fixture_messages(db, configured, fixture_dir)
        process_pending_media(db, configured)
        ai.classify_pending_media(db, configured)
    ready = Event()
    real_pool = pipeline.ThreadPoolExecutor
    submissions = []
    held = []
    class ControlledPool(real_pool):
        def submit(self, fn, *args, **kwargs):
            target = pipeline._classify_one if google_stage == "classification" else pipeline._embed_one
            if fn is target:
                submissions.append(args[1])
                if len(submissions) == 2:
                    # Hold one pending future so cancellation cannot race the executor.
                    from concurrent.futures import Future
                    future = Future()
                    held.append(future)
                    ready.set()
                    return future
            return super().submit(fn, *args, **kwargs)
    monkeypatch.setattr(pipeline, "ThreadPoolExecutor", ControlledPool)
    calls = []
    def depleted(s, *args, **kwargs):
        assert ready.wait(5)
        google_api._assert_credit_circuit_closed(s)
        calls.append(google_stage)
        open_ai_credit_circuit(s)
        raise GeminiRateLimitError("prepayment credits are depleted", status_code=429)
    if google_stage == "classification":
        monkeypatch.setattr(ai, "classify_with_google", depleted)
    else:
        monkeypatch.setattr(embeddings, "embed_text", depleted)
    report = pipeline.run_media_pipeline(configured, media_ids=imported["media_item_ids"], force=True,
                                        process_workers=1, classify_workers=1, embed_workers=1)
    assert calls == [google_stage]
    assert len(held) == 1 and held[0].cancelled()
    assert report["stopped_early"]
    if google_stage == "classification":
        assert report["embedded"] == 2
        assert report["classify_failed"] + report["classify_skipped"] == 2
    else:
        assert report["classified"] == 2
        assert report["embed_failed"] + report["embed_skipped"] == 2


@pytest.mark.parametrize("stop", [True, False])
def test_ordinary_quota_keeps_existing_option_and_no_embedding(settings, fixture_dir, monkeypatch, stop):
    from meme_arsenal import ai
    from meme_arsenal.google_api import GeminiRateLimitError
    from meme_arsenal.telegram_importer import import_fixture_messages
    configured = replace(settings, ai_stop_on_rate_limit=stop)
    with session_scope(configured) as db:
        imported = import_fixture_messages(db, configured, fixture_dir)
    def quota(*a, **kw):
        raise GeminiRateLimitError("ordinary request quota", status_code=429)
    monkeypatch.setattr(ai, "classify_with_mock", quota)
    report = pipeline.run_media_pipeline(configured, media_ids=imported["media_item_ids"],
                                        process_workers=1, classify_workers=1, embed_workers=1)
    assert report["embedded"] == 0
    assert report["stopped_early"] is stop
    assert report["classify_failed"] + report["classify_skipped"] == 2
    assert not report["classification_provider_paused"]
