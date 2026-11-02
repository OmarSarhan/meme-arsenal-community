from __future__ import annotations

from meme_arsenal.cli import _pipeline_media_ids_from_import_report


def test_pipeline_media_ids_from_import_report_uses_new_media_scope() -> None:
    assert _pipeline_media_ids_from_import_report({"media_item_ids": [1, "2"]}) == [1, 2]


def test_pipeline_media_ids_from_import_report_empty_means_no_new_work() -> None:
    assert _pipeline_media_ids_from_import_report({"media_item_ids": []}) == []


def test_pipeline_media_ids_from_missing_import_report_allows_explicit_backfill() -> None:
    assert _pipeline_media_ids_from_import_report({}) is None
    assert _pipeline_media_ids_from_import_report(None) is None
