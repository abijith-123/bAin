"""Local bAin simulation API.

The API uses synthetic cases only. Workflow decisions, authorization, version checks,
idempotency, and audit writes happen here; the browser is never trusted to enforce them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from backend.llm_advisory import AdvisoryProvider, AdvisoryService


Role = Literal[
    "admin", "doctor", "nurse", "pharmacist", "billing_specialist",
    "transport_staff", "housekeeping",
]
Status = Literal["pending", "ready", "in_progress", "blocked", "completed"]
Department = Literal["pharmacy", "insurance", "transport", "housekeeping"]
ExceptionOutcome = Literal["cleared", "denial_upheld"]

DB_PATH = Path(os.environ.get("BAIN_DB_PATH", Path(tempfile.gettempdir()) / "bain-demo.sqlite3"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

TOKEN_ENV_BY_ROLE: dict[str, Role] = {
    "BAIN_TOKEN_ADMIN": "admin",
    "BAIN_TOKEN_DOCTOR": "doctor",
    "BAIN_TOKEN_NURSE": "nurse",
    "BAIN_TOKEN_PHARMACIST": "pharmacist",
    "BAIN_TOKEN_BILLING_SPECIALIST": "billing_specialist",
    "BAIN_TOKEN_TRANSPORT_STAFF": "transport_staff",
    "BAIN_TOKEN_HOUSEKEEPING": "housekeeping",
}


def load_demo_tokens(environ: Mapping[str, str] | None = None) -> dict[str, tuple[str, Role]]:
    source = os.environ if environ is None else environ
    return {
        token: (f"demo-{role}", role)
        for env_name, role in TOKEN_ENV_BY_ROLE.items()
        if (token := source.get(env_name, "").strip())
    }


TOKENS = load_demo_tokens()


def validate_demo_tokens() -> None:
    global TOKENS
    missing = [name for name in TOKEN_ENV_BY_ROLE if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError(
            "Missing local demo bearer tokens: " + ", ".join(missing)
            + ". Set them from .env.example before starting FastAPI."
        )
    values = [os.environ[name].strip() for name in TOKEN_ENV_BY_ROLE]
    if len(values) != len(set(values)):
        raise RuntimeError("Each BAIN_TOKEN_* demo token must have a distinct value.")
    TOKENS = load_demo_tokens()

MILESTONES: dict[str, dict[str, Any]] = {
    "clinical_clearance": {"label": "Clinical clearance", "dependencies": [], "role": "doctor"},
    "pharmacy": {"label": "Pharmacy", "dependencies": ["clinical_clearance"], "role": "pharmacist"},
    "insurance": {"label": "Insurance authorization", "dependencies": ["clinical_clearance"], "role": "billing_specialist"},
    "transport": {"label": "Transport ready", "dependencies": ["clinical_clearance"], "role": "transport_staff"},
    "patient_departed": {"label": "Patient departed", "dependencies": ["pharmacy", "insurance"], "role": "transport_staff"},
    "housekeeping_dispatched": {"label": "Housekeeping dispatched", "dependencies": ["patient_departed"], "role": "housekeeping"},
    "bed_ready": {"label": "Bed ready", "dependencies": ["housekeeping_dispatched"], "role": "housekeeping"},
}

class APIError(Exception):
    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class MilestoneRequest(BaseModel):
    milestone_id: str = Field(min_length=1, max_length=64)
    target_status: Status
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


class ResolveRequest(BaseModel):
    resolution: str = Field(min_length=3, max_length=1000)
    outcome: ExceptionOutcome
    expected_version: int = Field(ge=1)


class AnalyzeRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=64)
    question: str | None = Field(default=None, max_length=1000)


ADVISORY_PROVIDER: AdvisoryProvider | None = None
_SECRET_PATTERNS = (
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\b(sk-|gsk_|AIza)[A-Za-z0-9_\-]{6,}\b"),
)


def _scrub_activity_message(message: str) -> str:
    cleaned = _SECRET_PATTERNS[0].sub(r"\1[REDACTED]", message)
    cleaned = _SECRET_PATTERNS[1].sub("[REDACTED]", cleaned)
    return cleaned.strip()[:1000]


def record_activity(
    *,
    patient_id: str | None,
    activity_type: str,
    message: str,
    generation_mode: str | None = None,
) -> str:
    activity_id = f"act-{uuid.uuid4().hex[:10]}"
    db = connect()
    try:
        db.execute(
            """INSERT INTO activity
               (activity_id, occurred_at, patient_id, activity_type, message, generation_mode)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (activity_id, now_iso(), patient_id, activity_type,
             _scrub_activity_message(message), generation_mode),
        )
        return activity_id
    finally:
        db.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 10000")
    return db


def _case(
    patient_id: str,
    statuses: dict[str, str],
    *,
    transport_required: bool = True,
    clearance_at: str | None = "2026-11-19T09:00:00Z",
    summary: str = "Synthetic discharge handoff. No real patient information is present.",
) -> dict[str, Any]:
    return {
        "patient_id": patient_id,
        "display_name": None,
        "clinical_clearance_at": clearance_at,
        "clinical_summary": summary,
        "transport_required": transport_required,
        "milestones": statuses,
    }


def seed_cases() -> list[dict[str, Any]]:
    return [
        _case("402", {
            "clinical_clearance": "completed", "pharmacy": "blocked", "insurance": "blocked",
            "transport": "ready", "patient_departed": "pending",
            "housekeeping_dispatched": "pending", "bed_ready": "pending",
        }),
        _case("403", {
            "clinical_clearance": "completed", "pharmacy": "blocked", "insurance": "completed",
            "transport": "ready", "patient_departed": "pending",
            "housekeeping_dispatched": "pending", "bed_ready": "pending",
        }),
        _case("404", {
            "clinical_clearance": "completed", "pharmacy": "completed", "insurance": "blocked",
            "transport": "ready", "patient_departed": "pending",
            "housekeeping_dispatched": "pending", "bed_ready": "pending",
        }),
        _case("405", {
            "clinical_clearance": "completed", "pharmacy": "completed", "insurance": "completed",
            "transport": "blocked", "patient_departed": "pending",
            "housekeeping_dispatched": "pending", "bed_ready": "pending",
        }),
        _case("406", {
            "clinical_clearance": "completed", "pharmacy": "ready", "insurance": "ready",
            "transport": "ready", "patient_departed": "pending",
            "housekeeping_dispatched": "pending", "bed_ready": "pending",
        }),
        _case("407", {
            "clinical_clearance": "completed", "pharmacy": "completed", "insurance": "completed",
            "transport": "completed", "patient_departed": "completed",
            "housekeeping_dispatched": "completed", "bed_ready": "completed",
        }, clearance_at="2026-11-19T08:00:00Z"),
    ]


def init_db() -> None:
    with connect() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS cases (
                patient_id TEXT PRIMARY KEY,
                version INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS exceptions (
                exception_id TEXT PRIMARY KEY,
                patient_id TEXT NOT NULL REFERENCES cases(patient_id),
                department TEXT NOT NULL,
                code TEXT NOT NULL,
                status TEXT NOT NULL,
                summary TEXT NOT NULL,
                linked_milestone TEXT NOT NULL,
                version INTEGER NOT NULL,
                resolution_outcome TEXT
            );
            CREATE TABLE IF NOT EXISTS department_service_state (
                patient_id TEXT NOT NULL REFERENCES cases(patient_id),
                department TEXT NOT NULL CHECK (department IN ('pharmacy', 'insurance')),
                status TEXT NOT NULL CHECK (status IN ('pending', 'ready', 'in_progress', 'blocked', 'completed')),
                PRIMARY KEY (patient_id, department)
            );
            CREATE TABLE IF NOT EXISTS audit_events (
                event_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                patient_id TEXT NOT NULL REFERENCES cases(patient_id),
                action TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT,
                version INTEGER NOT NULL,
                previous_state TEXT,
                new_state TEXT
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                key TEXT PRIMARY KEY,
                actor_id TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                activity_id TEXT PRIMARY KEY,
                occurred_at TEXT NOT NULL,
                patient_id TEXT,
                activity_type TEXT NOT NULL,
                message TEXT NOT NULL,
                generation_mode TEXT
            );
        """)
        exception_columns = {row["name"] for row in db.execute("PRAGMA table_info(exceptions)")}
        if "resolution_outcome" not in exception_columns:
            db.execute("ALTER TABLE exceptions ADD COLUMN resolution_outcome TEXT")
        # Additive migration for local databases created before state snapshots
        # were included in audit events.
        audit_columns = {row["name"] for row in db.execute("PRAGMA table_info(audit_events)")}
        if "previous_state" not in audit_columns:
            db.execute("ALTER TABLE audit_events ADD COLUMN previous_state TEXT")
        if "new_state" not in audit_columns:
            db.execute("ALTER TABLE audit_events ADD COLUMN new_state TEXT")
        if db.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0:
            for case in seed_cases():
                db.execute(
                    "INSERT INTO cases(patient_id, version, payload) VALUES (?, 1, ?)",
                    (case["patient_id"], json.dumps(case)),
                )
            for exc in (
                ("ex-ph-402", "402", "pharmacy", "MEDICATION_STOCKOUT",
                 "Medication unavailable; pharmacist review required", "pharmacy"),
                ("ex-ins-402", "402", "insurance", "ERR_AUTH_409",
                 "Authorization rejected; billing specialist review required", "insurance"),
                ("ex-ph-403", "403", "pharmacy", "MEDICATION_STOCKOUT",
                 "Medication unavailable; pharmacist review required", "pharmacy"),
                ("ex-ins-404", "404", "insurance", "ERR_AUTH_409",
                 "Authorization rejected; billing specialist review required", "insurance"),
            ):
                db.execute(
                    """INSERT INTO exceptions
                       (exception_id, patient_id, department, code, status, summary, linked_milestone, version)
                       VALUES (?, ?, ?, ?, 'open', ?, ?, 1)""",
                    exc,
                )
            db.execute(
                "INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?)",
                ("act-seed-402", now_iso(), "402", "bottleneck",
                 "Pharmacy stockout and insurance authorization rejection routed for human review.",
                 "deterministic_fallback"),
            )
        # Backfill a synthetic result for each independent mock department while
        # preserving any service state that already exists in an older database.
        for row in db.execute("SELECT patient_id, payload FROM cases"):
            case = json.loads(row["payload"])
            for department in ("pharmacy", "insurance"):
                db.execute(
                    "INSERT OR IGNORE INTO department_service_state VALUES (?, ?, ?)",
                    (row["patient_id"], department, case["milestones"][department]),
                )
        # Keep transport delays visible to the assigned specialist, including for
        # databases created by earlier app versions. INSERT OR IGNORE makes this
        # startup backfill safe across restarts and preserves resolved cases.
        for row in db.execute("SELECT patient_id, version, payload FROM cases"):
            case = json.loads(row["payload"])
            if case["milestones"].get("transport") == "blocked":
                db.execute(
                    """INSERT OR IGNORE INTO exceptions
                       (exception_id, patient_id, department, code, status, summary, linked_milestone, version)
                       VALUES (?, ?, 'transport', 'TRANSPORT_DELAY', 'open', ?, 'transport', ?)""",
                    (f"ex-tr-{row['patient_id']}", row["patient_id"],
                     "Transport availability pending; transport team confirmation required.",
                     row["version"]),
                )


@asynccontextmanager
async def lifespan(_: FastAPI):
    validate_demo_tokens()
    init_db()
    yield


app = FastAPI(title="bAin Discharge Coordination API", version="1.0.0", lifespan=lifespan)


@app.exception_handler(APIError)
async def api_error_handler(_: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.code.lower(), "code": exc.code, "message": exc.message, "details": exc.details},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Do not echo raw request bodies; validation locations/messages contain no patient payload.
    return JSONResponse(
        status_code=422,
        content={"error": "validation_error", "code": "VALIDATION_ERROR",
                 "message": "Request validation failed.",
                 "details": {"fields": [str(item.get("loc", [""])[-1]) for item in exc.errors()]}},
    )


def get_actor(authorization: str | None = Header(default=None)) -> tuple[str, Role]:
    if not authorization or not authorization.startswith("Bearer "):
        raise APIError(401, "UNAUTHENTICATED", "A valid demo bearer token is required.")
    token = authorization[7:].strip()
    actor = TOKENS.get(token)
    if actor is None:
        raise APIError(401, "UNAUTHENTICATED", "The bearer token is not recognized.")
    return actor


def load_case(db: sqlite3.Connection, patient_id: str) -> tuple[dict[str, Any], int]:
    row = db.execute("SELECT version, payload FROM cases WHERE patient_id = ?", (patient_id,)).fetchone()
    if row is None:
        raise APIError(404, "NOT_FOUND", "Case was not found.")
    return json.loads(row["payload"]), int(row["version"])


def case_exceptions(db: sqlite3.Connection, patient_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        "SELECT * FROM exceptions WHERE patient_id = ? AND status = 'open' ORDER BY exception_id",
        (patient_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def blockers_for(db: sqlite3.Connection, case: dict[str, Any], role: Role) -> list[str]:
    blockers = []
    for exc in case_exceptions(db, case["patient_id"]):
        if role in {"housekeeping", "transport_staff"}:
            # Keep blocked cases understandable without disclosing which clinical or
            # financial department owns the exception.
            blockers.append("Specialist review pending")
            continue
        if role == "pharmacist" and exc["department"] == "insurance":
            blockers.append("Another specialist review is pending")
        elif role == "billing_specialist" and exc["department"] == "pharmacy":
            blockers.append("Another specialist review is pending")
        else:
            blockers.append(exc["code"] if role in {"doctor", "nurse", "admin", "pharmacist", "billing_specialist"} else "Specialist review pending")
    upheld = db.execute(
        "SELECT department, code FROM exceptions WHERE patient_id = ? AND status = 'resolved' AND resolution_outcome = 'denial_upheld'",
        (case["patient_id"],),
    ).fetchall()
    for exc in upheld:
        if role in {"transport_staff", "housekeeping"}:
            blockers.append("Specialist follow-up required")
        elif role in {"billing_specialist", "doctor", "nurse", "admin"}:
            blockers.append(f"{exc['code']}: denial upheld; follow-up required")
    if case["milestones"].get("transport") == "blocked":
        blockers.append("Transport availability pending")
    return list(dict.fromkeys(blockers))


def aggregate_status(db: sqlite3.Connection, case: dict[str, Any]) -> str:
    if case["milestones"].get("bed_ready") == "completed":
        return "completed"
    if case_exceptions(db, case["patient_id"]) or "blocked" in case["milestones"].values():
        return "blocked"
    return "in_progress"


def is_locked(case: dict[str, Any], milestone_id: str) -> bool:
    milestone = MILESTONES[milestone_id]
    statuses = case["milestones"]
    for dependency in milestone["dependencies"]:
        if statuses.get(dependency) != "completed":
            return True
    if milestone_id == "patient_departed" and case["transport_required"]:
        if statuses.get("transport") != "completed":
            return True
    return False


def workflow_view(case: dict[str, Any], version: int, role: Role, db: sqlite3.Connection) -> dict[str, Any]:
    milestones = []
    for milestone_id, spec in MILESTONES.items():
        if milestone_id == "transport" and not case["transport_required"]:
            continue
        dependencies = list(spec["dependencies"])
        if milestone_id == "patient_departed" and case["transport_required"]:
            dependencies.append("transport")
        milestones.append({
            "milestone_id": milestone_id,
            "label": spec["label"],
            "status": case["milestones"][milestone_id],
            "dependencies": dependencies,
            "locked": is_locked(case, milestone_id),
        })
    return {
        "patient_id": case["patient_id"],
        "version": version,
        "milestones": milestones,
        "transport_required": case["transport_required"],
        "blockers": blockers_for(db, case, role),
    }


def summary_for(db: sqlite3.Connection, case: dict[str, Any], version: int, role: Role) -> dict[str, Any]:
    return {
        "patient_id": case["patient_id"],
        "display_name": None if role in {"housekeeping", "transport_staff"} else case["display_name"],
        "workflow_status": aggregate_status(db, case),
        "version": version,
        "blockers": blockers_for(db, case, role),
        "clinical_clearance_at": case["clinical_clearance_at"] if role in {"doctor", "nurse"} else None,
    }


def require_patient(case: dict[str, Any], role: Role) -> dict[str, Any]:
    view = dict(case)
    if role in {"housekeeping", "transport_staff", "pharmacist", "billing_specialist", "admin"}:
        view["clinical_summary"] = None
    if role in {"housekeeping", "transport_staff"}:
        view["display_name"] = None
        view["clinical_clearance_at"] = None
    return view


def record_audit(
    db: sqlite3.Connection,
    actor: tuple[str, Role],
    patient_id: str,
    action: str,
    outcome: str,
    reason: str | None,
    version: int,
    previous_state: str | None = None,
    new_state: str | None = None,
) -> str:
    event_id = f"audit-{uuid.uuid4().hex[:12]}"
    safe_reason = (reason or "").strip()[:1000] or None
    db.execute(
        """INSERT INTO audit_events
           (event_id, occurred_at, actor_id, actor_role, patient_id, action, outcome,
            reason, version, previous_state, new_state)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, now_iso(), actor[0], actor[1], patient_id, action, outcome,
         safe_reason, version, previous_state, new_state),
    )
    return event_id


def _write_transition(
    patient_id: str,
    milestone_id: str,
    target_status: str,
    actor: tuple[str, Role],
    expected_version: int,
    idempotency_key: str,
    reason: str,
) -> dict[str, Any]:
    if len(idempotency_key) < 8 or len(idempotency_key) > 128:
        raise APIError(422, "VALIDATION_ERROR", "Idempotency-Key must be 8 to 128 characters.")
    if milestone_id not in MILESTONES:
        raise APIError(422, "VALIDATION_ERROR", "Unknown milestone identifier.")
    if target_status not in {"in_progress", "completed"}:
        raise APIError(409, "INVALID_TRANSITION", "Milestones can only be advanced to in_progress or completed by a user.")
    if not reason or len(reason.strip()) < 3:
        raise APIError(422, "VALIDATION_ERROR", "A reason of at least 3 characters is required.")

    fingerprint = hashlib.sha256(
        json.dumps([patient_id, milestone_id, target_status, expected_version, reason.strip()], separators=(",", ":")).encode()
    ).hexdigest()
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT actor_id, request_hash, response_json FROM idempotency WHERE key = ?", (idempotency_key,)).fetchone()
        if prior:
            if prior["actor_id"] != actor[0] or prior["request_hash"] != fingerprint:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "This idempotency key was already used for another request.")
            response = json.loads(prior["response_json"])
            db.commit()
            return response

        case, version = load_case(db, patient_id)
        current = case["milestones"][milestone_id]
        if expected_version != version:
            record_audit(db, actor, patient_id, f"milestone:{milestone_id}", "rejected",
                         "STALE_VERSION", version, current, current)
            db.commit()
            raise APIError(409, "STALE_VERSION", "The case changed. Refresh workflow state and retry with the current version.",
                           {"current_version": version})
        if actor[1] != MILESTONES[milestone_id]["role"]:
            record_audit(db, actor, patient_id, f"milestone:{milestone_id}", "rejected",
                         "FORBIDDEN", version, current, current)
            db.commit()
            raise APIError(403, "FORBIDDEN", "Your role cannot update this milestone.")
        if is_locked(case, milestone_id):
            record_audit(db, actor, patient_id, f"milestone:{milestone_id}", "rejected",
                         "INVALID_TRANSITION", version, current, current)
            db.commit()
            raise APIError(409, "INVALID_TRANSITION", "Required workflow dependencies are not complete.")

        allowed_current = {"pending", "ready", "in_progress"} if target_status == "in_progress" else {"ready", "in_progress"}
        if current == "completed" or current not in allowed_current:
            record_audit(db, actor, patient_id, f"milestone:{milestone_id}", "rejected",
                         "INVALID_TRANSITION", version, current, current)
            db.commit()
            raise APIError(409, "INVALID_TRANSITION", f"Cannot move milestone from {current} to {target_status}.")

        case["milestones"][milestone_id] = target_status
        new_version = version + 1
        db.execute("UPDATE cases SET version = ?, payload = ? WHERE patient_id = ? AND version = ?",
                   (new_version, json.dumps(case), patient_id, version))
        if milestone_id in {"pharmacy", "insurance"}:
            db.execute(
                "UPDATE department_service_state SET status=? WHERE patient_id=? AND department=?",
                (target_status, patient_id, milestone_id),
            )
        db.execute("UPDATE exceptions SET version = ? WHERE patient_id = ? AND status = 'open'",
                   (new_version, patient_id))
        audit_id = record_audit(db, actor, patient_id, f"milestone:{milestone_id}",
                                "success", reason, new_version, current, target_status)
        response = {
            "patient_id": patient_id, "milestone_id": milestone_id,
            "status": target_status, "version": new_version, "audit_event_id": audit_id,
        }
        db.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)",
                   (idempotency_key, actor[0], fingerprint, json.dumps(response)))
        db.commit()
        return response
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


def get_discharge_dag_state(patient_id: str, role: Role = "admin") -> dict[str, Any]:
    """Recommended read tool: return deterministic DAG state for the authenticated role."""
    db = connect()
    try:
        case, version = load_case(db, patient_id)
        return workflow_view(case, version, role, db)
    finally:
        db.close()


def _read_synthetic_department(patient_id: str, department: str) -> tuple[str, int]:
    """Query one isolated synthetic department service."""
    db = connect()
    try:
        row = db.execute(
            """SELECT service.status, cases.version
               FROM department_service_state AS service
               JOIN cases ON cases.patient_id = service.patient_id
               WHERE service.patient_id=? AND service.department=?""",
            (patient_id, department),
        ).fetchone()
        if row is None:
            load_case(db, patient_id)  # Raise the normal NOT_FOUND error for an unknown case.
            raise APIError(503, "SERVICE_UNAVAILABLE", f"The synthetic {department} service is not initialized.")
        return row["status"], int(row["version"])
    finally:
        db.close()


def check_pharmacy_and_insurance(patient_id: str) -> dict[str, str]:
    """Query both synthetic services in parallel and persist blocked results safely."""
    department_codes = {
        "pharmacy": ("MEDICATION_STOCKOUT", "Medication unavailable; pharmacist review required", "ph"),
        "insurance": ("ERR_AUTH_409", "Authorization rejected; billing specialist review required", "ins"),
    }
    system_actor: tuple[str, Role] = ("bain-system", "admin")

    # Retry if either the parallel reads or the subsequent transaction observe a
    # concurrent case update. The function never writes a result from stale reads.
    for _ in range(3):
        with ThreadPoolExecutor(max_workers=2) as pool:
            pharmacy_future = pool.submit(_read_synthetic_department, patient_id, "pharmacy")
            insurance_future = pool.submit(_read_synthetic_department, patient_id, "insurance")
            results = {}
            for department, future in (("pharmacy", pharmacy_future), ("insurance", insurance_future)):
                try:
                    results[department] = future.result()
                except APIError as error:
                    if error.code == "NOT_FOUND":
                        raise
                    results[department] = None
                except Exception:
                    # Retain the independent service's failure as unavailable;
                    # the other result can still be committed and escalated.
                    results[department] = None

        available_versions = {result[1] for result in results.values() if result is not None}
        if not available_versions:
            raise APIError(503, "SERVICE_UNAVAILABLE", "Neither synthetic department service returned a result.")
        if len(available_versions) != 1:
            continue

        observed_version = next(iter(available_versions))
        statuses = {
            department: result[0] if result is not None else "unavailable"
            for department, result in results.items()
        }
        db = connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            case, current_version = load_case(db, patient_id)
            if current_version != observed_version:
                db.rollback()
                continue
            if case["milestones"]["clinical_clearance"] != "completed":
                raise APIError(409, "INVALID_TRANSITION", "Pharmacy and Insurance checks require clinical clearance.")

            milestone_changes: dict[str, str] = {}
            exception_changes: list[tuple[str, str, str, str, str | None]] = []
            for department, status in statuses.items():
                if status != "blocked":
                    continue
                code, summary, department_code = department_codes[department]
                exception_id = f"ex-{department_code}-{patient_id}"
                previous_milestone_status = case["milestones"][department]
                if previous_milestone_status != "blocked":
                    case["milestones"][department] = "blocked"
                    milestone_changes[department] = previous_milestone_status
                row = db.execute(
                    "SELECT status, version, resolution_outcome FROM exceptions WHERE exception_id=?",
                    (exception_id,),
                ).fetchone()
                if row is None:
                    db.execute(
                        """INSERT INTO exceptions
                           (exception_id, patient_id, department, code, status, summary, linked_milestone, version)
                           VALUES (?, ?, ?, ?, 'open', ?, ?, ?)""",
                        (exception_id, patient_id, department, code, summary, department, current_version),
                    )
                    exception_changes.append((exception_id, department, code, summary, None))
                elif row["status"] == "resolved" and row["resolution_outcome"] == "denial_upheld":
                    # Preserve human billing specialist's upheld denial decision; repeat
                    # department checks must not silently reset review state to 'open'.
                    continue
                elif row["status"] != "open":
                    db.execute(
                        "UPDATE exceptions SET status='open', resolution_outcome=NULL, version=?, summary=? WHERE exception_id=?",
                        (current_version, summary, exception_id),
                    )
                    exception_changes.append((exception_id, department, code, summary, row["status"]))

            changed = bool(milestone_changes or exception_changes)
            if changed:
                new_version = current_version + 1
                db.execute("UPDATE cases SET version=?, payload=? WHERE patient_id=? AND version=?",
                           (new_version, json.dumps(case), patient_id, current_version))
                db.execute("UPDATE exceptions SET version=? WHERE patient_id=? AND status='open'",
                           (new_version, patient_id))
                for department, previous_status in milestone_changes.items():
                    record_audit(
                        db, system_actor, patient_id, f"milestone:{department}:blocked",
                        "success", "Synthetic department service returned blocked.",
                        new_version, previous_status, "blocked")
                for exception_id, department, _, summary, previous_status in exception_changes:
                    record_audit(
                        db, system_actor, patient_id, f"exception:{exception_id}:open",
                        "success", summary, new_version, previous_status, "open")
            else:
                db.execute(
                    "UPDATE exceptions SET version=? WHERE patient_id=? AND status='open' AND version!=?",
                    (current_version, patient_id, current_version),
                )

            for department, status in statuses.items():
                if status == "unavailable":
                    db.execute(
                        "INSERT INTO activity VALUES (?, ?, ?, ?, ?, ?)",
                        (f"act-{uuid.uuid4().hex[:10]}", now_iso(), patient_id, "error",
                         f"{department.title()} synthetic service unavailable; other department result retained.", None),
                    )
            db.commit()
            return statuses
        except Exception:
            if db.in_transaction:
                db.rollback()
            raise
        finally:
            db.close()

    raise APIError(409, "STALE_VERSION", "Department status changed during the check; refresh and retry.")


def update_department_milestone(
    patient_id: str, milestone_id: str, target_status: str, actor: tuple[str, Role],
    expected_version: int, idempotency_key: str, reason: str,
) -> dict[str, Any]:
    """The only user state-write path; validates RBAC, DAG, version, idempotency and audit."""
    return _write_transition(patient_id, milestone_id, target_status, actor,
                             expected_version, idempotency_key, reason)


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    return {"status": "ok", "service": "bAin-api"}


@app.get("/api/dashboard/summary", tags=["dashboard"])
def dashboard_summary(actor: tuple[str, Role] = Depends(get_actor)) -> dict[str, Any]:
    db = connect()
    try:
        cases = [load_case(db, row["patient_id"]) for row in db.execute("SELECT patient_id FROM cases ORDER BY patient_id")]
        statuses = [aggregate_status(db, case) for case, _ in cases]
        return {
            "generated_at": now_iso(),
            "active_cases": sum(status != "completed" for status in statuses),
            "blocked_cases": statuses.count("blocked"),
            "completed_today": statuses.count("completed"),
            "available_beds": sum(case["milestones"]["bed_ready"] == "completed" for case, _ in cases),
            "pending_specialist_reviews": db.execute("SELECT COUNT(*) FROM exceptions WHERE status='open'").fetchone()[0],
        }
    finally:
        db.close()


@app.get("/api/patients", tags=["patients"])
def list_patients(
    status: Status | None = Query(default=None),
    actor: tuple[str, Role] = Depends(get_actor),
) -> dict[str, Any]:
    db = connect()
    try:
        items = []
        for row in db.execute("SELECT patient_id, version, payload FROM cases ORDER BY CAST(patient_id AS INTEGER)"):
            case = json.loads(row["payload"])
            item = summary_for(db, case, int(row["version"]), actor[1])
            if status is None or item["workflow_status"] == status:
                items.append(item)
        return {"items": items}
    finally:
        db.close()


@app.get("/api/patients/{patient_id}", tags=["patients"])
def patient_detail(patient_id: str, actor: tuple[str, Role] = Depends(get_actor)) -> dict[str, Any]:
    db = connect()
    try:
        case, version = load_case(db, patient_id)
        result = summary_for(db, case, version, actor[1])
        result.update({
            "departments": {
                "pharmacy": case["milestones"]["pharmacy"],
                "insurance": case["milestones"]["insurance"],
                "transport": case["milestones"]["transport"],
                "housekeeping": case["milestones"]["housekeeping_dispatched"],
            },
            "clinical_summary": require_patient(case, actor[1])["clinical_summary"],
            "transport_required": case["transport_required"],
        })
        return result
    finally:
        db.close()


@app.get("/api/patients/{patient_id}/workflow", tags=["workflow"])
def patient_workflow(patient_id: str, actor: tuple[str, Role] = Depends(get_actor)) -> dict[str, Any]:
    return get_discharge_dag_state(patient_id, actor[1])


@app.post("/api/patients/{patient_id}/milestones", tags=["workflow"])
def update_milestone(
    patient_id: str,
    payload: MilestoneRequest,
    actor: tuple[str, Role] = Depends(get_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key:
        raise APIError(422, "VALIDATION_ERROR", "Idempotency-Key header is required.")
    return update_department_milestone(
        patient_id, payload.milestone_id, payload.target_status, actor,
        payload.expected_version, idempotency_key, payload.reason,
    )


@app.get("/api/patients/{patient_id}/audit", tags=["workflow"])
def patient_audit(patient_id: str, actor: tuple[str, Role] = Depends(get_actor)) -> dict[str, Any]:
    db = connect()
    try:
        load_case(db, patient_id)
        rows = db.execute("SELECT * FROM audit_events WHERE patient_id=? ORDER BY occurred_at DESC",
                          (patient_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            if actor[1] in {"housekeeping", "transport_staff"}:
                item["reason"] = None
                item["actor_id"] = None
            elif actor[1] == "admin":
                # Operations administrators can verify accountability while
                # free-text specialist notes remain hidden from their view.
                item["reason"] = None
            elif actor[1] == "pharmacist" and not (
                ":pharmacy" in item["action"] or ":ex-ph-" in item["action"]
            ):
                item["reason"] = None
            elif actor[1] == "billing_specialist" and not (
                ":insurance" in item["action"] or ":ex-ins-" in item["action"]
            ):
                item["reason"] = None
            items.append(item)
        return {"items": items}
    finally:
        db.close()


@app.get("/api/departments/status", tags=["departments"])
def departments_status(actor: tuple[str, Role] = Depends(get_actor)) -> dict[str, Any]:
    db = connect()
    try:
        names = ["pharmacy", "insurance", "transport", "housekeeping"]
        items = []
        for department in names:
            milestone_id = "housekeeping_dispatched" if department == "housekeeping" else department
            statuses = []
            for row in db.execute("SELECT payload FROM cases"):
                case = json.loads(row["payload"])
                statuses.append(case["milestones"][milestone_id])
            open_cases = sum(status != "completed" for status in statuses)
            blocked_cases = statuses.count("blocked")
            overall = "blocked" if blocked_cases else ("in_progress" if open_cases else "completed")
            items.append({"department": department, "status": overall,
                          "open_cases": open_cases, "blocked_cases": blocked_cases})
        if actor[1] == "housekeeping":
            items = [item for item in items if item["department"] == "housekeeping"]
        elif actor[1] == "transport_staff":
            items = [item for item in items if item["department"] == "transport"]
        elif actor[1] == "pharmacist":
            items = [item for item in items if item["department"] == "pharmacy"]
        elif actor[1] == "billing_specialist":
            items = [item for item in items if item["department"] == "insurance"]
        return {"items": items}
    finally:
        db.close()


@app.get("/api/exceptions", tags=["exceptions"])
def list_exceptions(
    status: Literal["open", "resolved"] | None = Query(default=None),
    actor: tuple[str, Role] = Depends(get_actor),
) -> dict[str, Any]:
    visibility = {
        "pharmacist": {"pharmacy"}, "billing_specialist": {"insurance"},
        "transport_staff": {"transport"}, "housekeeping": {"housekeeping"},
        "admin": {"pharmacy", "insurance", "transport", "housekeeping"},
        "doctor": set(), "nurse": set(),
    }
    departments = visibility[actor[1]]
    if not departments:
        return {"items": []}
    db = connect()
    try:
        query = "SELECT * FROM exceptions WHERE department IN (" + ",".join("?" for _ in departments) + ")"
        params: list[Any] = list(departments)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY patient_id, exception_id"
        items = []
        for row in db.execute(query, params):
            item = {key: row[key] for key in ("exception_id", "patient_id", "department", "code", "status", "summary", "version", "resolution_outcome")}
            if actor[1] == "admin":
                item["summary"] = "Specialist review required; operational summary only."
            items.append(item)
        return {"items": items}
    finally:
        db.close()


@app.post("/api/exceptions/{exception_id}/resolve", tags=["exceptions"])
def resolve_exception(
    exception_id: str,
    payload: ResolveRequest,
    actor: tuple[str, Role] = Depends(get_actor),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> dict[str, Any]:
    if not idempotency_key or not 8 <= len(idempotency_key) <= 128:
        raise APIError(422, "VALIDATION_ERROR", "A valid Idempotency-Key header is required.")
    fingerprint = hashlib.sha256(json.dumps([exception_id, payload.resolution.strip(), payload.outcome, payload.expected_version],
                                               separators=(",", ":")).encode()).hexdigest()
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        prior = db.execute("SELECT actor_id, request_hash, response_json FROM idempotency WHERE key=?",
                           (idempotency_key,)).fetchone()
        if prior:
            if prior["actor_id"] != actor[0] or prior["request_hash"] != fingerprint:
                raise APIError(409, "IDEMPOTENCY_CONFLICT", "This idempotency key was already used for another request.")
            response = json.loads(prior["response_json"])
            db.commit()
            return response
        exc = db.execute("SELECT * FROM exceptions WHERE exception_id=?", (exception_id,)).fetchone()
        if exc is None:
            raise APIError(404, "NOT_FOUND", "Exception was not found.")
        if payload.outcome == "denial_upheld" and exc["department"] != "insurance":
            raise APIError(422, "VALIDATION_ERROR", "Denial upheld is only a valid outcome for insurance exceptions.")
        expected_role = {
            "pharmacy": "pharmacist",
            "insurance": "billing_specialist",
            "transport": "transport_staff",
        }.get(exc["department"])
        if actor[1] != expected_role:
            record_audit(db, actor, exc["patient_id"], f"exception:{exception_id}:resolve",
                         "rejected", "FORBIDDEN", exc["version"], exc["status"], exc["status"])
            db.commit()
            raise APIError(403, "FORBIDDEN", "Only the assigned human specialist can resolve this exception.")
        case, case_version = load_case(db, exc["patient_id"])
        if payload.expected_version != int(exc["version"]) or int(exc["version"]) != case_version:
            record_audit(db, actor, exc["patient_id"], f"exception:{exception_id}:resolve",
                         "rejected", "STALE_VERSION", case_version,
                         case["milestones"][exc["linked_milestone"]],
                         case["milestones"][exc["linked_milestone"]])
            db.commit()
            raise APIError(409, "STALE_VERSION", "The case changed. Refresh the exception list and retry.",
                           {"current_version": case_version})
        if exc["status"] != "open":
            record_audit(db, actor, exc["patient_id"], f"exception:{exception_id}:resolve",
                         "rejected", "INVALID_TRANSITION", case_version,
                         exc["status"], exc["status"])
            db.commit()
            raise APIError(409, "INVALID_TRANSITION", "This exception has already been resolved.")
        previous_milestone_status = case["milestones"][exc["linked_milestone"]]
        milestone_status = "blocked" if payload.outcome == "denial_upheld" else "ready"
        case["milestones"][exc["linked_milestone"]] = milestone_status
        new_version = case_version + 1
        db.execute("UPDATE cases SET version=?, payload=? WHERE patient_id=? AND version=?",
                   (new_version, json.dumps(case), exc["patient_id"], case_version))
        if exc["department"] in {"pharmacy", "insurance"}:
            db.execute(
                "UPDATE department_service_state SET status=? WHERE patient_id=? AND department=?",
                (milestone_status, exc["patient_id"], exc["department"]),
            )
        db.execute("UPDATE exceptions SET status='resolved', resolution_outcome=?, version=? WHERE exception_id=? AND status='open'",
                   (payload.outcome, new_version, exception_id))
        db.execute("UPDATE exceptions SET version=? WHERE patient_id=? AND status='open'",
                   (new_version, exc["patient_id"]))
        record_audit(db, actor, exc["patient_id"], f"exception:{exception_id}:resolve",
                     "success", payload.resolution.strip(), new_version,
                     previous_milestone_status, milestone_status)
        response = {
            "exception_id": exception_id, "patient_id": exc["patient_id"],
            "department": exc["department"], "code": exc["code"],
            "status": "resolved", "summary": exc["summary"], "version": new_version,
            "resolution_outcome": payload.outcome, "milestone_status": milestone_status,
        }
        db.execute("INSERT INTO idempotency VALUES (?, ?, ?, ?)",
                   (idempotency_key, actor[0], fingerprint, json.dumps(response)))
        db.commit()
        return response
    except Exception:
        if db.in_transaction:
            db.rollback()
        raise
    finally:
        db.close()


@app.get("/api/agent/activity", tags=["agent"])
def agent_activity(actor: tuple[str, Role] = __import__("fastapi").Depends(get_actor)) -> dict[str, Any]:
    db = connect()
    try:
        rows = db.execute("SELECT * FROM activity ORDER BY occurred_at DESC LIMIT 50").fetchall()
        items = [dict(row) for row in rows]
        if actor[1] in {"housekeeping", "transport_staff"}:
            items = [{**item, "patient_id": None, "message": "Operational workflow activity recorded."}
                     for item in items]
        return {"items": items}
    finally:
        db.close()


@app.post("/api/agent/analyze", tags=["agent"])
def analyze_case(
    payload: AnalyzeRequest,
    actor: tuple[str, Role] = Depends(get_actor),
) -> dict[str, Any]:
    service = AdvisoryService(provider=ADVISORY_PROVIDER)
    return service.analyze_case(
        patient_id=payload.patient_id,
        role=actor[1],
        question=payload.question,
    )
