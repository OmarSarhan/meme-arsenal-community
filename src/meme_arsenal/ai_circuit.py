from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import session_scope
from .models import AiProviderCircuit


GOOGLE_PROVIDER = "google"
CIRCUIT_CLOSED = "closed"
CIRCUIT_OPEN = "open"
CREDIT_DEPLETION_REASON = "prepayment_credits_depleted"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class AiCircuitStatus:
    provider: str
    state: str
    reason: str | None
    failure_count: int
    opened_at: datetime | None
    next_probe_at: datetime | None
    probe_lease_until: datetime | None
    last_probe_at: datetime | None
    recovered_at: datetime | None

    @property
    def is_open(self) -> bool:
        return self.state == CIRCUIT_OPEN

    def retry_after_seconds(self, now: datetime | None = None) -> float | None:
        if not self.is_open or self.next_probe_at is None:
            return None
        now = _as_utc(now) or _utc_now()
        return max(0.0, (_as_utc(self.next_probe_at) - now).total_seconds())

    def payload(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "state": self.state,
            "reason": self.reason,
            "failure_count": self.failure_count,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "next_probe_at": self.next_probe_at.isoformat() if self.next_probe_at else None,
            "probe_lease_until": self.probe_lease_until.isoformat() if self.probe_lease_until else None,
            "last_probe_at": self.last_probe_at.isoformat() if self.last_probe_at else None,
            "recovered_at": self.recovered_at.isoformat() if self.recovered_at else None,
        }


@dataclass(frozen=True)
class AiProbeLease:
    acquired: bool
    outcome: str
    token: str | None
    status: AiCircuitStatus


@dataclass(frozen=True)
class AiProbeCompletion:
    applied: bool
    status: AiCircuitStatus


def _status(row: AiProviderCircuit) -> AiCircuitStatus:
    return AiCircuitStatus(
        provider=row.provider,
        state=row.state,
        reason=row.reason,
        failure_count=int(row.failure_count or 0),
        opened_at=_as_utc(row.opened_at),
        next_probe_at=_as_utc(row.next_probe_at),
        probe_lease_until=_as_utc(row.probe_lease_until),
        last_probe_at=_as_utc(row.last_probe_at),
        recovered_at=_as_utc(row.recovered_at),
    )


def _closed_status(provider: str) -> AiCircuitStatus:
    return AiCircuitStatus(
        provider=provider,
        state=CIRCUIT_CLOSED,
        reason=None,
        failure_count=0,
        opened_at=None,
        next_probe_at=None,
        probe_lease_until=None,
        last_probe_at=None,
        recovered_at=None,
    )


def _locked_row(session: Session, provider: str) -> AiProviderCircuit:
    row = session.scalar(
        select(AiProviderCircuit)
        .where(AiProviderCircuit.provider == provider)
        .with_for_update()
    )
    if row is None:
        row = AiProviderCircuit(provider=provider, state=CIRCUIT_CLOSED)
        session.add(row)
        session.flush()
    return row


def ai_circuit_status(settings: Settings, provider: str = GOOGLE_PROVIDER) -> AiCircuitStatus:
    with session_scope(settings) as session:
        row = session.get(AiProviderCircuit, provider)
        return _status(row) if row is not None else _closed_status(provider)


def open_ai_credit_circuit(
    settings: Settings,
    *,
    provider: str = GOOGLE_PROVIDER,
    reason: str = CREDIT_DEPLETION_REASON,
    now: datetime | None = None,
) -> AiCircuitStatus:
    now = _as_utc(now) or _utc_now()
    with session_scope(settings) as session:
        row = _locked_row(session, provider)
        if row.state != CIRCUIT_OPEN or row.opened_at is None:
            row.opened_at = now
        row.state = CIRCUIT_OPEN
        row.reason = reason
        row.failure_count = int(row.failure_count or 0) + 1
        row.next_probe_at = now + timedelta(seconds=max(1.0, settings.ai_credit_probe_interval_seconds))
        row.probe_lease_until = None
        row.probe_token = None
        row.updated_at = now
        session.flush()
        return _status(row)


def acquire_ai_credit_probe(
    settings: Settings,
    *,
    provider: str = GOOGLE_PROVIDER,
    force: bool = False,
    now: datetime | None = None,
) -> AiProbeLease:
    now = _as_utc(now) or _utc_now()
    with session_scope(settings) as session:
        row = _locked_row(session, provider)
        status = _status(row)
        if not status.is_open:
            return AiProbeLease(False, "closed", None, status)
        lease_until = _as_utc(row.probe_lease_until)
        if lease_until is not None and lease_until > now:
            return AiProbeLease(False, "probe_in_progress", None, status)
        next_probe_at = _as_utc(row.next_probe_at)
        if not force and next_probe_at is not None and next_probe_at > now:
            return AiProbeLease(False, "cooldown", None, status)

        token = uuid.uuid4().hex
        row.probe_token = token
        row.probe_lease_until = now + timedelta(seconds=max(1.0, settings.ai_credit_probe_lease_seconds))
        row.last_probe_at = now
        row.updated_at = now
        session.flush()
        return AiProbeLease(True, "acquired", token, _status(row))


def complete_ai_credit_probe(
    settings: Settings,
    token: str,
    *,
    recovered: bool,
    provider: str = GOOGLE_PROVIDER,
    now: datetime | None = None,
) -> AiProbeCompletion:
    """Complete only the matching lease; report ownership with the locked status."""
    now = _as_utc(now) or _utc_now()
    with session_scope(settings) as session:
        row = _locked_row(session, provider)
        if row.probe_token != token:
            return AiProbeCompletion(False, _status(row))
        row.probe_token = None
        row.probe_lease_until = None
        row.updated_at = now
        if recovered:
            row.state = CIRCUIT_CLOSED
            row.reason = None
            row.opened_at = None
            row.next_probe_at = None
            row.recovered_at = now
        else:
            row.state = CIRCUIT_OPEN
            row.next_probe_at = now + timedelta(
                seconds=max(1.0, settings.ai_credit_probe_interval_seconds)
            )
        session.flush()
        return AiProbeCompletion(True, _status(row))
