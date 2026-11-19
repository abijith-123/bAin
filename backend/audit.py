"""Durable append-only audit logging for successful and rejected workflow actions."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from backend.models import AuditEventItem, AuditOutcome, Role


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_audit_event(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    actor_role: Role,
    patient_id: str | None,
    action: str,
    outcome: AuditOutcome,
    reason: str | None,
    version: int,
) -> str:
    """Insert an audit row using the caller's active SQLite connection/transaction."""
    event_id = f"evt-{uuid.uuid4().hex[:12]}"
    occurred_at = utc_now_iso()
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, occurred_at, actor_id, actor_role,
            patient_id, action, outcome, reason, version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            occurred_at,
            actor_id,
            actor_role.value,
            patient_id,
            action,
            outcome.value,
            reason,
            version,
        ),
    )
    return event_id


def list_audit_events(
    conn: sqlite3.Connection,
    patient_id: str,
) -> list[AuditEventItem]:
    """Return chronological audit events for a patient."""
    rows = conn.execute(
        """
        SELECT event_id, occurred_at, actor_id, actor_role,
               patient_id, action, outcome, reason, version
        FROM audit_events
        WHERE patient_id = ?
        ORDER BY rowid ASC
        """,
        (patient_id,),
    ).fetchall()
    return [
        AuditEventItem(
            event_id=row["event_id"],
            occurred_at=datetime.fromisoformat(row["occurred_at"].replace("Z", "+00:00")),
            actor_id=row["actor_id"],
            actor_role=Role(row["actor_role"]),
            patient_id=row["patient_id"],
            action=row["action"],
            outcome=AuditOutcome(row["outcome"]),
            reason=row["reason"],
            version=int(row["version"]),
        )
        for row in rows
    ]
