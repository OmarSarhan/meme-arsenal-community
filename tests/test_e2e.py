from __future__ import annotations

from meme_arsenal.ai import classify_pending_media
from meme_arsenal.catalog import export_catalog
from meme_arsenal.dashboard import build_dashboard
from meme_arsenal.db import session_scope
from meme_arsenal.embeddings import embed_pending_media
from meme_arsenal.media import process_pending_media
from meme_arsenal.search import export_search_results
from meme_arsenal.telegram_importer import import_fixture_messages


def test_fixture_e2e_builds_static_archive(settings, fixture_dir, monkeypatch):
    with session_scope(settings) as session:
        assert import_fixture_messages(session, settings, fixture_dir)["media"] == 2
        assert process_pending_media(session, settings)["processed"] == 2
        assert classify_pending_media(session, settings)["classified"] == 2
        assert embed_pending_media(session, settings)["embedded"] == 2
        index = export_catalog(session, settings)
        result_path = export_search_results(session, settings, "code review win")

    monkeypatch.setattr("meme_arsenal.dashboard.shutil.which", lambda _name: None)
    dashboard = build_dashboard(settings)

    assert index["item_count"] == 2
    assert result_path.exists()
    assert dashboard["mode"] == "fallback"
    assert (settings.dashboard_dist_dir / "arsenal-config.json").exists()

