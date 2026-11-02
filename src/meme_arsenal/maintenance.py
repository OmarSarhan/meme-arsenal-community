from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from .config import Settings
from .db import session_scope
from .models import RemixJobRecord


REMOVABLE_REMIX_JOB_STATUSES = ("failed", "blocked", "rate_limited")


def cleanup_online_state(
    settings: Settings,
    *,
    job_retention_days: int = 90,
    draft_retention_days: int = 14,
    apply: bool = False,
) -> dict[str, int | bool]:
    now = datetime.now(timezone.utc)
    job_cutoff = now - timedelta(days=max(1, job_retention_days))
    draft_cutoff_timestamp = (now - timedelta(days=max(1, draft_retention_days))).timestamp()

    with session_scope(settings) as session:
        removable_jobs = session.scalars(
            select(RemixJobRecord).where(
                RemixJobRecord.status.in_(REMOVABLE_REMIX_JOB_STATUSES),
                RemixJobRecord.updated_at < job_cutoff,
            )
        ).all()
        removable_job_ids = {job.id for job in removable_jobs}
        retained_job_ids = set(
            session.scalars(
                select(RemixJobRecord.id).where(RemixJobRecord.id.not_in(removable_job_ids))
            ).all()
        )

        stale_drafts = [
            path
            for path in settings.remix_drafts_dir.iterdir()
            if _is_stale_unreferenced_draft(path, retained_job_ids, draft_cutoff_timestamp)
        ]

        if apply:
            for path in stale_drafts:
                path.unlink(missing_ok=True)
            for job in removable_jobs:
                session.delete(job)

    return {
        "apply": apply,
        "terminal_jobs": len(removable_jobs),
        "orphan_drafts": len(stale_drafts),
        "jobs_removed": len(removable_jobs) if apply else 0,
        "drafts_removed": len(stale_drafts) if apply else 0,
    }


def _is_stale_unreferenced_draft(
    path: Path,
    retained_job_ids: set[str],
    cutoff_timestamp: float,
) -> bool:
    if not path.is_file() or path.stat().st_mtime >= cutoff_timestamp:
        return False
    return path.stem not in retained_job_ids
