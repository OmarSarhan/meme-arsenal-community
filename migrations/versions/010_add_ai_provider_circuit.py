"""Add persisted AI provider credit circuit state.

Revision ID: 010_ai_provider_circuit
Revises: 009_widen_language
Create Date: 2026-07-14
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "010_ai_provider_circuit"
down_revision = "009_widen_language"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if not _table_exists("ai_provider_circuits"):
        op.create_table(
            "ai_provider_circuits",
            sa.Column("provider", sa.String(length=32), primary_key=True),
            sa.Column("state", sa.String(length=32), nullable=False, server_default="closed"),
            sa.Column("reason", sa.String(length=128), nullable=True),
            sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("next_probe_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("probe_lease_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("probe_token", sa.String(length=64), nullable=True),
            sa.Column("last_probe_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("recovered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_index("ix_ai_provider_circuits_state", "ai_provider_circuits", ["state"])
        op.create_index(
            "ix_ai_provider_circuits_next_probe_at",
            "ai_provider_circuits",
            ["next_probe_at"],
        )

    bind = op.get_bind()
    existing = bind.execute(
        sa.text("SELECT provider FROM ai_provider_circuits WHERE provider = 'google'")
    ).first()
    if existing is not None:
        return

    now = datetime.now(timezone.utc)
    state = "closed"
    reason = None
    opened_at = None
    next_probe_at = None
    if _table_exists("gemini_call_errors"):
        depleted = bind.execute(
            sa.text(
                "SELECT created_at FROM gemini_call_errors "
                "WHERE lower(error_message) LIKE '%prepayment credit%' "
                "AND lower(error_message) LIKE '%deplet%' "
                "ORDER BY created_at DESC LIMIT 1"
            )
        ).first()
        if depleted is not None:
            state = "open"
            reason = "prepayment_credits_depleted"
            opened_at = depleted[0]
            next_probe_at = now

    bind.execute(
        sa.text(
            "INSERT INTO ai_provider_circuits "
            "(provider, state, reason, failure_count, opened_at, next_probe_at, updated_at) "
            "VALUES (:provider, :state, :reason, :failure_count, :opened_at, :next_probe_at, :updated_at)"
        ),
        {
            "provider": "google",
            "state": state,
            "reason": reason,
            "failure_count": 1 if state == "open" else 0,
            "opened_at": opened_at,
            "next_probe_at": next_probe_at,
            "updated_at": now,
        },
    )


def downgrade() -> None:
    if _table_exists("ai_provider_circuits"):
        op.drop_table("ai_provider_circuits")
