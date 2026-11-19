"""SQLite persistence with BEGIN IMMEDIATE transactions, optimistic locking, and idempotency."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, TypeVar

from backend.audit import list_audit_events, record_audit_event, utc_now_iso
from backend.auth import Actor, can_resolve_exception, can_update_milestone
from backend.dag import (
    DAG_BY_ID,
    DAG_SPECS,
    DAGValidationError,
    compute_overall_workflow_status,
    compute_unlocked_promotions,
    is_milestone_locked,
    validate_milestone_transition,
)
from backend.models import (
    AuditEventItem,
    AuditOutcome,
    Department,
    ErrorCode,
    ExceptionItem,
    ExceptionStatus,
    MilestoneItem,
    MilestoneUpdateResponse,
    PatientDetail,
    PatientSummary,
    WorkflowResponse,
    WorkflowStatus,
)

T = TypeVar("T")

DEFAULT_DB_PATH = str(Path(__file__).resolve().parent / ".clearturn_local.db")


class WorkflowError(Exception):
    """Domain error mapped directly to contract-compatible HTTP responses."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


class StaleVersionError(WorkflowError):
    """Raised when expected_version does not match the current persisted case version."""

    def __init__(self, expected_version: int, current_version: int) -> None:
        super().__init__(
            status_code=409,
            code=ErrorCode.STALE_VERSION,
            message="Workflow version is stale; refresh state and retry.",
            details={
                "expected_version": expected_version,
                "current_version": current_version,
            },
        )
        self.expected_version = expected_version
        self.current_version = current_version


def _hash_payload(actor_id: str, endpoint: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"actor_id": actor_id, "endpoint": endpoint, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ClearTurnStore:
    """Deterministic SQLite-backed store for discharge workflows."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.getenv("BAIN_DB_PATH") or DEFAULT_DB_PATH

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=10.0,
            isolation_level=None,  # explicit BEGIN IMMEDIATE / COMMIT / ROLLBACK
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def init_and_seed(self) -> None:
        """Create tables if missing and seed synthetic Patient #402."""
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS patients (
                    patient_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    clinical_summary TEXT NOT NULL,
                    clinical_clearance_at TEXT,
                    transport_required INTEGER NOT NULL DEFAULT 0,
                    transport_status TEXT NOT NULL DEFAULT 'ready',
                    version INTEGER NOT NULL DEFAULT 1
                );

                CREATE TABLE IF NOT EXISTS milestones (
                    patient_id TEXT NOT NULL,
                    milestone_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (patient_id, milestone_id),
                    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS exceptions (
                    exception_id TEXT PRIMARY KEY,
                    patient_id TEXT NOT NULL,
                    milestone_id TEXT NOT NULL,
                    department TEXT NOT NULL,
                    code TEXT NOT NULL,
                    status TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    resolution TEXT,
                    resolved_by TEXT,
                    resolved_at TEXT,
                    FOREIGN KEY (patient_id) REFERENCES patients(patient_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    occurred_at TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    patient_id TEXT,
                    action TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    reason TEXT,
                    version INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    status_code INTEGER NOT NULL,
                    response_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            conn.execute("BEGIN IMMEDIATE")
            self._seed_patient_402_in_tx(conn)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def _seed_patient_402_in_tx(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT 1 FROM patients WHERE patient_id = ?", ("402",)
        ).fetchone()
        if existing is not None:
            return

        now = utc_now_iso()
        conn.execute(
            """
            INSERT INTO patients (
                patient_id, display_name, clinical_summary,
                clinical_clearance_at, transport_required, transport_status, version
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "402",
                "Synthetic Patient 402",
                "Synthetic post-op case cleared clinically; awaiting discharge medication and insurer auth.",
                "2026-11-19T09:00:00Z",
                1,
                "ready",
                1,
            ),
        )

        initial_milestones = [
            ("clinical_clearance", WorkflowStatus.completed.value),
            ("pharmacy", WorkflowStatus.blocked.value),
            ("insurance", WorkflowStatus.blocked.value),
            ("patient_departed", WorkflowStatus.pending.value),
            ("housekeeping_dispatched", WorkflowStatus.pending.value),
            ("bed_ready", WorkflowStatus.pending.value),
        ]
        for milestone_id, status in initial_milestones:
            conn.execute(
                """
                INSERT INTO milestones (patient_id, milestone_id, status, updated_at)
                VALUES (?, ?, ?, ?)
                """,
                ("402", milestone_id, status, now),
            )

        initial_exceptions = [
            (
                "ex-ph-402",
                "402",
                "pharmacy",
                Department.pharmacy.value,
                "MEDICATION_STOCKOUT",
                ExceptionStatus.open.value,
                "Pharmacy stockout: medication unavailable; pharmacist review required",
            ),
            (
                "ex-ins-402",
                "402",
                "insurance",
                Department.insurance.value,
                "ERR_AUTH_409",
                ExceptionStatus.open.value,
                "Insurance authorization ERR_AUTH_409: billing specialist review required",
            ),
        ]
        for row in initial_exceptions:
            conn.execute(
                """
                INSERT INTO exceptions (
                    exception_id, patient_id, milestone_id, department, code, status, summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )

    def _load_milestone_statuses(
        self, conn: sqlite3.Connection, patient_id: str
    ) -> dict[str, WorkflowStatus]:
        rows = conn.execute(
            "SELECT milestone_id, status FROM milestones WHERE patient_id = ?",
            (patient_id,),
        ).fetchall()
        return {r["milestone_id"]: WorkflowStatus(r["status"]) for r in rows}

    def _load_open_blocker_labels(
        self, conn: sqlite3.Connection, patient_id: str
    ) -> list[str]:
        rows = conn.execute(
            """
            SELECT department, code, summary
            FROM exceptions
            WHERE patient_id = ? AND status = 'open'
            ORDER BY exception_id ASC
            """,
            (patient_id,),
        ).fetchall()
        blockers: list[str] = []
        for row in rows:
            if row["code"] == "MEDICATION_STOCKOUT":
                blockers.append("Pharmacy stockout")
            elif row["code"] == "ERR_AUTH_409":
                blockers.append("Insurance authorization ERR_AUTH_409")
            else:
                blockers.append(f"{row['department']}: {row['code']}")
        return blockers

    def _load_patient_row(self, conn: sqlite3.Connection, patient_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM patients WHERE patient_id = ?", (patient_id,)
        ).fetchone()
        if row is None:
            raise WorkflowError(
                status_code=404,
                code=ErrorCode.NOT_FOUND,
                message=f"Patient '{patient_id}' not found.",
            )
        return row

    def list_patients(
        self, status_filter: WorkflowStatus | None = None
    ) -> list[tuple[PatientSummary, dict[str, WorkflowStatus]]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM patients ORDER BY patient_id ASC"
            ).fetchall()
            results: list[tuple[PatientSummary, dict[str, WorkflowStatus]]] = []
            for row in rows:
                pid = row["patient_id"]
                statuses = self._load_milestone_statuses(conn, pid)
                overall = compute_overall_workflow_status(statuses)
                if status_filter is not None and overall != status_filter:
                    continue
                summary = PatientSummary(
                    patient_id=pid,
                    display_name=row["display_name"],
                    workflow_status=overall,
                    version=int(row["version"]),
                    blockers=self._load_open_blocker_labels(conn, pid),
                    clinical_clearance_at=_parse_iso_dt(row["clinical_clearance_at"]),
                )
                results.append((summary, statuses))
            return results
        finally:
            conn.close()

    def get_patient_detail(
        self, patient_id: str
    ) -> tuple[PatientDetail, dict[str, WorkflowStatus]]:
        conn = self._connect()
        try:
            row = self._load_patient_row(conn, patient_id)
            statuses = self._load_milestone_statuses(conn, patient_id)
            overall = compute_overall_workflow_status(statuses)
            departments = {
                "pharmacy": statuses["pharmacy"],
                "insurance": statuses["insurance"],
                "transport": WorkflowStatus(row["transport_status"]),
                "housekeeping": (
                    WorkflowStatus.completed
                    if statuses["bed_ready"] == WorkflowStatus.completed
                    else statuses["housekeeping_dispatched"]
                ),
            }
            detail = PatientDetail(
                patient_id=patient_id,
                display_name=row["display_name"],
                workflow_status=overall,
                version=int(row["version"]),
                blockers=self._load_open_blocker_labels(conn, patient_id),
                clinical_clearance_at=_parse_iso_dt(row["clinical_clearance_at"]),
                departments=departments,
                clinical_summary=row["clinical_summary"],
                transport_required=bool(row["transport_required"]),
            )
            return detail, statuses
        finally:
            conn.close()

    def get_workflow(
        self, patient_id: str
    ) -> tuple[WorkflowResponse, dict[str, WorkflowStatus]]:
        conn = self._connect()
        try:
            row = self._load_patient_row(conn, patient_id)
            statuses = self._load_milestone_statuses(conn, patient_id)
            milestones = [
                MilestoneItem(
                    milestone_id=spec.milestone_id,
                    label=spec.label,
                    status=statuses[spec.milestone_id],
                    dependencies=list(spec.dependencies),
                    locked=is_milestone_locked(spec.milestone_id, statuses),
                )
                for spec in DAG_SPECS
            ]
            wf = WorkflowResponse(
                patient_id=patient_id,
                version=int(row["version"]),
                milestones=milestones,
                transport_required=bool(row["transport_required"]),
                blockers=self._load_open_blocker_labels(conn, patient_id),
            )
            return wf, statuses
        finally:
            conn.close()

    def list_exceptions(
        self, status_filter: ExceptionStatus | None = None
    ) -> list[ExceptionItem]:
        conn = self._connect()
        try:
            if status_filter is not None:
                rows = conn.execute(
                    """
                    SELECT e.*, p.version AS patient_version
                    FROM exceptions e
                    JOIN patients p ON p.patient_id = e.patient_id
                    WHERE e.status = ?
                    ORDER BY e.exception_id ASC
                    """,
                    (status_filter.value,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT e.*, p.version AS patient_version
                    FROM exceptions e
                    JOIN patients p ON p.patient_id = e.patient_id
                    ORDER BY e.exception_id ASC
                    """
                ).fetchall()
            return [
                ExceptionItem(
                    exception_id=row["exception_id"],
                    patient_id=row["patient_id"],
                    department=Department(row["department"]),
                    code=row["code"],
                    status=ExceptionStatus(row["status"]),
                    summary=row["summary"],
                    version=int(row["patient_version"]),
                )
                for row in rows
            ]
        finally:
            conn.close()

    def get_audit_events(self, patient_id: str) -> list[AuditEventItem]:
        conn = self._connect()
        try:
            self._load_patient_row(conn, patient_id)
            return list_audit_events(conn, patient_id)
        finally:
            conn.close()

    def _check_idempotency_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        actor: Actor,
        endpoint: str,
        request_hash: str,
        patient_id: str | None,
        current_version: int,
        action: str,
    ) -> dict[str, Any] | None:
        existing = conn.execute(
            "SELECT * FROM idempotency_keys WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing is None:
            return None

        if existing["request_hash"] == request_hash and existing["endpoint"] == endpoint:
            return json.loads(existing["response_json"])

        # Mismatched payload or endpoint for an existing idempotency key:
        # commit ONLY the rejected audit record and leave workflow state untouched.
        record_audit_event(
            conn,
            actor_id=actor.actor_id,
            actor_role=actor.role,
            patient_id=patient_id,
            action=action,
            outcome=AuditOutcome.rejected,
            reason=f"{ErrorCode.IDEMPOTENCY_CONFLICT.value}: Idempotency-Key reused with different request payload",
            version=current_version,
        )
        conn.execute("COMMIT")
        raise WorkflowError(
            status_code=409,
            code=ErrorCode.IDEMPOTENCY_CONFLICT,
            message="Idempotency-Key was already used with a different request payload.",
            details={"idempotency_key": idempotency_key},
        )

    def _reject_with_durable_audit(
        self,
        conn: sqlite3.Connection,
        *,
        actor: Actor,
        patient_id: str,
        action: str,
        current_version: int,
        error: WorkflowError,
    ) -> None:
        """Persist a rejected audit row without mutating any workflow/milestone/exception tables."""
        record_audit_event(
            conn,
            actor_id=actor.actor_id,
            actor_role=actor.role,
            patient_id=patient_id,
            action=action,
            outcome=AuditOutcome.rejected,
            reason=f"{error.code.value}: {error.message}",
            version=current_version,
        )
        conn.execute("COMMIT")
        raise error

    def update_milestone(
        self,
        *,
        patient_id: str,
        milestone_id: str,
        target_status: WorkflowStatus,
        expected_version: int,
        reason: str,
        actor: Actor,
        idempotency_key: str,
    ) -> MilestoneUpdateResponse:
        """Atomically validate and apply a milestone transition with optimistic locking."""
        endpoint = f"POST /api/patients/{patient_id}/milestones"
        action = f"milestone:{milestone_id}->{target_status.value}"
        request_hash = _hash_payload(
            actor.actor_id,
            endpoint,
            {
                "patient_id": patient_id,
                "milestone_id": milestone_id,
                "target_status": target_status.value,
                "expected_version": expected_version,
                "reason": reason,
            },
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            patient = self._load_patient_row(conn, patient_id)
            current_version = int(patient["version"])

            replayed = self._check_idempotency_in_tx(
                conn,
                idempotency_key=idempotency_key,
                actor=actor,
                endpoint=endpoint,
                request_hash=request_hash,
                patient_id=patient_id,
                current_version=current_version,
                action=action,
            )
            if replayed is not None:
                conn.execute("COMMIT")
                return MilestoneUpdateResponse(**replayed)

            if milestone_id not in DAG_BY_ID:
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=WorkflowError(
                        status_code=409,
                        code=ErrorCode.INVALID_TRANSITION,
                        message=f"Unknown milestone '{milestone_id}'.",
                        details={"milestone_id": milestone_id},
                    ),
                )

            # 1. Server-side RBAC check
            if not can_update_milestone(actor.role, milestone_id):
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=WorkflowError(
                        status_code=403,
                        code=ErrorCode.FORBIDDEN,
                        message=f"Role '{actor.role.value}' is not authorized to update milestone '{milestone_id}'.",
                        details={"milestone_id": milestone_id, "actor_role": actor.role.value},
                    ),
                )

            # 2. Optimistic version check
            if expected_version != current_version:
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=StaleVersionError(
                        expected_version=expected_version,
                        current_version=current_version,
                    ),
                )

            # 3. Deterministic DAG & exception guard
            statuses = self._load_milestone_statuses(conn, patient_id)
            open_exc_rows = conn.execute(
                """
                SELECT code FROM exceptions
                WHERE patient_id = ? AND milestone_id = ? AND status = 'open'
                """,
                (patient_id, milestone_id),
            ).fetchall()
            open_exc_codes = [r["code"] for r in open_exc_rows]

            try:
                validate_milestone_transition(
                    milestone_id=milestone_id,
                    target_status=target_status,
                    milestone_statuses=statuses,
                    open_exception_codes=open_exc_codes,
                    transport_required=bool(patient["transport_required"]),
                    transport_ready=(patient["transport_status"] == WorkflowStatus.ready.value),
                )
            except DAGValidationError as exc:
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=WorkflowError(
                        status_code=409,
                        code=ErrorCode.INVALID_TRANSITION,
                        message=exc.message,
                        details=exc.details,
                    ),
                )

            # 4. Atomic state mutation, downstream ready promotion, version increment, audit & idempotency
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE milestones
                SET status = ?, updated_at = ?
                WHERE patient_id = ? AND milestone_id = ?
                """,
                (target_status.value, now, patient_id, milestone_id),
            )
            statuses[milestone_id] = target_status

            for promoted_id, promoted_status in compute_unlocked_promotions(statuses).items():
                conn.execute(
                    """
                    UPDATE milestones
                    SET status = ?, updated_at = ?
                    WHERE patient_id = ? AND milestone_id = ?
                    """,
                    (promoted_status.value, now, patient_id, promoted_id),
                )

            new_version = current_version + 1
            cur = conn.execute(
                """
                UPDATE patients
                SET version = ?
                WHERE patient_id = ? AND version = ?
                """,
                (new_version, patient_id, expected_version),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise StaleVersionError(
                    expected_version=expected_version,
                    current_version=current_version,
                )

            audit_event_id = record_audit_event(
                conn,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                patient_id=patient_id,
                action=action,
                outcome=AuditOutcome.success,
                reason=reason,
                version=new_version,
            )

            response = MilestoneUpdateResponse(
                patient_id=patient_id,
                milestone_id=milestone_id,
                status=target_status,
                version=new_version,
                audit_event_id=audit_event_id,
            )
            conn.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, actor_id, endpoint, request_hash,
                    status_code, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    actor.actor_id,
                    endpoint,
                    request_hash,
                    200,
                    response.model_dump_json(),
                    now,
                ),
            )
            conn.execute("COMMIT")
            return response
        except WorkflowError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def resolve_exception(
        self,
        *,
        exception_id: str,
        resolution: str,
        expected_version: int,
        actor: Actor,
        idempotency_key: str,
    ) -> ExceptionItem:
        """Atomically resolve a specialist exception and move its milestone from blocked -> ready."""
        endpoint = f"POST /api/exceptions/{exception_id}/resolve"
        action = f"exception:{exception_id}:resolve"
        request_hash = _hash_payload(
            actor.actor_id,
            endpoint,
            {
                "exception_id": exception_id,
                "resolution": resolution,
                "expected_version": expected_version,
            },
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            exc_row = conn.execute(
                "SELECT * FROM exceptions WHERE exception_id = ?",
                (exception_id,),
            ).fetchone()
            if exc_row is None:
                conn.execute("ROLLBACK")
                raise WorkflowError(
                    status_code=404,
                    code=ErrorCode.NOT_FOUND,
                    message=f"Exception '{exception_id}' not found.",
                )

            patient_id = exc_row["patient_id"]
            milestone_id = exc_row["milestone_id"]
            department = Department(exc_row["department"])
            patient = self._load_patient_row(conn, patient_id)
            current_version = int(patient["version"])

            replayed = self._check_idempotency_in_tx(
                conn,
                idempotency_key=idempotency_key,
                actor=actor,
                endpoint=endpoint,
                request_hash=request_hash,
                patient_id=patient_id,
                current_version=current_version,
                action=action,
            )
            if replayed is not None:
                conn.execute("COMMIT")
                return ExceptionItem(**replayed)

            # 1. Server-side RBAC check
            if not can_resolve_exception(actor.role, department):
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=WorkflowError(
                        status_code=403,
                        code=ErrorCode.FORBIDDEN,
                        message=(
                            f"Role '{actor.role.value}' is not authorized to resolve "
                            f"'{department.value}' exceptions."
                        ),
                        details={
                            "exception_id": exception_id,
                            "department": department.value,
                            "actor_role": actor.role.value,
                        },
                    ),
                )

            # 2. Optimistic version check against patient workflow version
            if expected_version != current_version:
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=StaleVersionError(
                        expected_version=expected_version,
                        current_version=current_version,
                    ),
                )

            # 3. Verify exception is currently open
            if exc_row["status"] != ExceptionStatus.open.value:
                self._reject_with_durable_audit(
                    conn,
                    actor=actor,
                    patient_id=patient_id,
                    action=action,
                    current_version=current_version,
                    error=WorkflowError(
                        status_code=409,
                        code=ErrorCode.INVALID_TRANSITION,
                        message=f"Exception '{exception_id}' is already resolved.",
                        details={"exception_id": exception_id, "status": exc_row["status"]},
                    ),
                )

            # 4. Atomic update: resolve exception, unblock milestone to 'ready' (NEVER 'completed')
            now = utc_now_iso()
            conn.execute(
                """
                UPDATE exceptions
                SET status = ?, resolution = ?, resolved_by = ?, resolved_at = ?
                WHERE exception_id = ?
                """,
                (
                    ExceptionStatus.resolved.value,
                    resolution,
                    actor.actor_id,
                    now,
                    exception_id,
                ),
            )

            remaining_open = conn.execute(
                """
                SELECT COUNT(*) AS cnt
                FROM exceptions
                WHERE patient_id = ? AND milestone_id = ? AND status = 'open'
                """,
                (patient_id, milestone_id),
            ).fetchone()["cnt"]

            if remaining_open == 0:
                ms_row = conn.execute(
                    "SELECT status FROM milestones WHERE patient_id = ? AND milestone_id = ?",
                    (patient_id, milestone_id),
                ).fetchone()
                if ms_row and ms_row["status"] == WorkflowStatus.blocked.value:
                    conn.execute(
                        """
                        UPDATE milestones
                        SET status = ?, updated_at = ?
                        WHERE patient_id = ? AND milestone_id = ?
                        """,
                        (WorkflowStatus.ready.value, now, patient_id, milestone_id),
                    )

            new_version = current_version + 1
            cur = conn.execute(
                """
                UPDATE patients
                SET version = ?
                WHERE patient_id = ? AND version = ?
                """,
                (new_version, patient_id, expected_version),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK")
                raise StaleVersionError(
                    expected_version=expected_version,
                    current_version=current_version,
                )

            record_audit_event(
                conn,
                actor_id=actor.actor_id,
                actor_role=actor.role,
                patient_id=patient_id,
                action=action,
                outcome=AuditOutcome.success,
                reason=resolution,
                version=new_version,
            )

            item = ExceptionItem(
                exception_id=exc_row["exception_id"],
                patient_id=patient_id,
                department=department,
                code=exc_row["code"],
                status=ExceptionStatus.resolved,
                summary=exc_row["summary"],
                version=new_version,
            )
            conn.execute(
                """
                INSERT INTO idempotency_keys (
                    idempotency_key, actor_id, endpoint, request_hash,
                    status_code, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    actor.actor_id,
                    endpoint,
                    request_hash,
                    200,
                    item.model_dump_json(),
                    now,
                ),
            )
            conn.execute("COMMIT")
            return item
        except WorkflowError:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def execute_with_optimistic_retry(
        self,
        patient_id: str,
        operation: Callable[[int], T],
        *,
        max_retries: int = 3,
    ) -> T:
        """Safe refresh-and-retry helper for conflicting independent department operations.

        On StaleVersionError, reloads the current version and re-invokes the full validated
        operation. Validation (RBAC, DAG, exception state) is never bypassed.
        """
        wf, _ = self.get_workflow(patient_id)
        current_version = wf.version
        for attempt in range(max_retries + 1):
            try:
                return operation(current_version)
            except StaleVersionError as exc:
                if attempt >= max_retries:
                    raise
                current_version = exc.current_version
        raise RuntimeError("Unreachable retry loop exit")
