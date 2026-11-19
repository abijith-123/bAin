"""Server-side bearer token authentication, RBAC matrix, and field-level PHI redaction."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from backend.models import (
    AgentActivityItem,
    AuditEventItem,
    Department,
    ErrorCode,
    ExceptionItem,
    PatientDetail,
    PatientSummary,
    Role,
    WorkflowResponse,
    WorkflowStatus,
)


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: Role
    display_name: str


class AuthError(Exception):
    """Raised for 401 UNAUTHENTICATED or 403 FORBIDDEN errors."""

    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details or {}


# Synthetic demo tokens for offline local simulation and automated tests only.
# Override or extend via BAIN_DEMO_TOKENS_JSON environment variable in local setups.
DEFAULT_DEMO_TOKENS: dict[str, Actor] = {
    "demo-token-admin": Actor("actor-admin-01", Role.admin, "Synthetic Ops Admin"),
    "demo-token-doctor": Actor("actor-doc-01", Role.doctor, "Dr. Synthetic Clinician"),
    "demo-token-nurse": Actor("actor-nurse-01", Role.nurse, "RN Synthetic Nurse"),
    "demo-token-pharmacist": Actor("actor-pharm-01", Role.pharmacist, "PharmD Synthetic"),
    "demo-token-billing": Actor("actor-bill-01", Role.billing_specialist, "Synthetic Billing Specialist"),
    "demo-token-transport": Actor("actor-trans-01", Role.transport_staff, "Synthetic Transport Staff"),
    "demo-token-housekeeping": Actor("actor-hk-01", Role.housekeeping, "Synthetic Housekeeping Staff"),
}

# Strict milestone RBAC matrix. Admin is intentionally NOT a default bypass role.
MILESTONE_ALLOWED_ROLES: dict[str, frozenset[Role]] = {
    "clinical_clearance": frozenset({Role.doctor}),
    "pharmacy": frozenset({Role.pharmacist}),
    "insurance": frozenset({Role.billing_specialist}),
    "transport": frozenset({Role.transport_staff}),
    "patient_departed": frozenset({Role.nurse, Role.transport_staff}),
    "housekeeping_dispatched": frozenset({Role.housekeeping}),
    "bed_ready": frozenset({Role.housekeeping}),
}

# Strict specialist exception resolution RBAC matrix. Admin cannot override specialist decisions.
EXCEPTION_RESOLVE_ALLOWED_ROLES: dict[Department, frozenset[Role]] = {
    Department.pharmacy: frozenset({Role.pharmacist}),
    Department.insurance: frozenset({Role.billing_specialist}),
    Department.transport: frozenset({Role.transport_staff}),
    Department.housekeeping: frozenset({Role.housekeeping}),
}

# Roles permitted to view clinical PHI (clinical_summary, clinical_clearance_at, clinical audit text).
CLINICAL_PHI_ROLES: frozenset[Role] = frozenset({Role.doctor, Role.nurse})

# Roles permitted to view patient display_name.
DISPLAY_NAME_ROLES: frozenset[Role] = frozenset(
    {Role.doctor, Role.nurse, Role.pharmacist, Role.billing_specialist}
)

# Roles strictly prohibited from seeing clinical details, pharmacy/insurance blockers, or clinical notes.
NON_CLINICAL_OPS_ROLES: frozenset[Role] = frozenset({Role.housekeeping, Role.transport_staff})

_bearer_scheme = HTTPBearer(auto_error=False)


def _load_token_registry() -> dict[str, Actor]:
    registry = dict(DEFAULT_DEMO_TOKENS)
    raw_override = os.getenv("BAIN_DEMO_TOKENS_JSON", "").strip()
    if raw_override:
        parsed = json.loads(raw_override)
        for token, entry in parsed.items():
            registry[token] = Actor(
                actor_id=str(entry["actor_id"]),
                role=Role(entry["role"]),
                display_name=str(entry.get("display_name", entry["actor_id"])),
            )
    return registry


def authenticate_token(token: str | None) -> Actor:
    if not token:
        raise AuthError(
            status_code=401,
            code=ErrorCode.UNAUTHENTICATED,
            message="Missing bearer authentication token.",
        )
    registry = _load_token_registry()
    actor = registry.get(token)
    if actor is None:
        raise AuthError(
            status_code=401,
            code=ErrorCode.UNAUTHENTICATED,
            message="Invalid bearer authentication token.",
        )
    return actor


def get_current_actor(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> Actor:
    token = credentials.credentials if credentials else None
    return authenticate_token(token)


def can_update_milestone(role: Role, milestone_id: str) -> bool:
    allowed = MILESTONE_ALLOWED_ROLES.get(milestone_id, frozenset())
    return role in allowed


def can_resolve_exception(role: Role, department: Department) -> bool:
    allowed = EXCEPTION_RESOLVE_ALLOWED_ROLES.get(department, frozenset())
    return role in allowed


def filter_blockers_for_role(
    role: Role,
    blockers: list[str],
    milestone_statuses: dict[str, WorkflowStatus],
) -> list[str]:
    """Filter or generalize blockers so non-clinical roles never receive clinical PHI."""
    if role == Role.housekeeping:
        if milestone_statuses.get("patient_departed") != WorkflowStatus.completed:
            return ["Awaiting patient departure"]
        return []
    if role == Role.transport_staff:
        ops_blockers: list[str] = []
        if milestone_statuses.get("pharmacy") != WorkflowStatus.completed:
            ops_blockers.append("Pharmacy clearance pending")
        if milestone_statuses.get("insurance") != WorkflowStatus.completed:
            ops_blockers.append("Insurance clearance pending")
        return ops_blockers
    if role == Role.pharmacist:
        return [b for b in blockers if "pharmacy" in b.lower() or "medication" in b.lower()]
    if role == Role.billing_specialist:
        return [b for b in blockers if "insurance" in b.lower() or "auth" in b.lower()]
    return list(blockers)


def filter_patient_summary_for_role(
    role: Role,
    summary: PatientSummary,
    milestone_statuses: dict[str, WorkflowStatus],
) -> PatientSummary:
    return PatientSummary(
        patient_id=summary.patient_id,
        display_name=summary.display_name if role in DISPLAY_NAME_ROLES else None,
        workflow_status=summary.workflow_status,
        version=summary.version,
        blockers=filter_blockers_for_role(role, summary.blockers, milestone_statuses),
        clinical_clearance_at=(
            summary.clinical_clearance_at if role in CLINICAL_PHI_ROLES or role == Role.admin else None
        ),
    )


def filter_patient_detail_for_role(
    role: Role,
    detail: PatientDetail,
    milestone_statuses: dict[str, WorkflowStatus],
) -> PatientDetail:
    if role == Role.housekeeping:
        departments = {
            k: v for k, v in detail.departments.items() if k == "housekeeping"
        }
    elif role == Role.transport_staff:
        departments = {
            k: v for k, v in detail.departments.items() if k in {"transport", "housekeeping"}
        }
    else:
        departments = dict(detail.departments)

    return PatientDetail(
        patient_id=detail.patient_id,
        display_name=detail.display_name if role in DISPLAY_NAME_ROLES else None,
        workflow_status=detail.workflow_status,
        version=detail.version,
        blockers=filter_blockers_for_role(role, detail.blockers, milestone_statuses),
        clinical_clearance_at=(
            detail.clinical_clearance_at if role in CLINICAL_PHI_ROLES or role == Role.admin else None
        ),
        departments=departments,
        clinical_summary=detail.clinical_summary if role in CLINICAL_PHI_ROLES else None,
        transport_required=detail.transport_required,
    )


def filter_workflow_for_role(
    role: Role,
    workflow: WorkflowResponse,
    milestone_statuses: dict[str, WorkflowStatus],
) -> WorkflowResponse:
    return WorkflowResponse(
        patient_id=workflow.patient_id,
        version=workflow.version,
        milestones=workflow.milestones,
        transport_required=workflow.transport_required,
        blockers=filter_blockers_for_role(role, workflow.blockers, milestone_statuses),
    )


def filter_exceptions_for_role(
    role: Role,
    exceptions: list[ExceptionItem],
) -> list[ExceptionItem]:
    if role in {Role.admin, Role.doctor, Role.nurse}:
        return list(exceptions)
    if role == Role.pharmacist:
        return [e for e in exceptions if e.department == Department.pharmacy]
    if role == Role.billing_specialist:
        return [e for e in exceptions if e.department == Department.insurance]
    if role == Role.transport_staff:
        return [e for e in exceptions if e.department == Department.transport]
    if role == Role.housekeeping:
        return [e for e in exceptions if e.department == Department.housekeeping]
    return []


def filter_audit_events_for_role(
    role: Role,
    events: list[AuditEventItem],
) -> list[AuditEventItem]:
    """Filter and redact audit events so non-clinical roles never see clinical notes or PHI."""
    if role in {Role.admin, Role.doctor, Role.nurse}:
        return list(events)

    filtered: list[AuditEventItem] = []
    for ev in events:
        if role == Role.housekeeping:
            if not any(
                key in ev.action
                for key in ("housekeeping_dispatched", "bed_ready", "patient_departed")
            ):
                continue
            filtered.append(
                ev.model_copy(update={"reason": _redact_reason_for_ops(ev)})
            )
        elif role == Role.transport_staff:
            if not any(
                key in ev.action
                for key in ("patient_departed", "transport", "housekeeping_dispatched")
            ):
                continue
            filtered.append(
                ev.model_copy(update={"reason": _redact_reason_for_ops(ev)})
            )
        elif role == Role.pharmacist:
            if "pharmacy" in ev.action or "exception:ex-ph" in ev.action or ev.actor_role == Role.pharmacist:
                filtered.append(ev)
        elif role == Role.billing_specialist:
            if "insurance" in ev.action or "exception:ex-ins" in ev.action or ev.actor_role == Role.billing_specialist:
                filtered.append(ev)
    return filtered


def _redact_reason_for_ops(event: AuditEventItem) -> str | None:
    if event.reason is None:
        return None
    if event.outcome.value == "rejected":
        # Preserve deterministic rejection code prefix if present, strip free-text notes.
        code_prefix = event.reason.split(":", 1)[0].strip()
        return f"{code_prefix}: action not permitted in current operational state"
    return "Operational status updated"


def redact_advisory_text_for_role(role: Role, text: str | None) -> str | None:
    """Placeholder/guard for AI advisory outputs: strip clinical text for non-clinical roles."""
    if text is None:
        return None
    if role in NON_CLINICAL_OPS_ROLES:
        return "Operational readiness summary only; clinical details redacted by policy."
    return text


# Clinical and specialist roles permitted to call POST /api/agent/analyze.
# Operational-only roles (housekeeping, transport_staff) are denied server-side (403 FORBIDDEN).
ANALYZE_ALLOWED_ROLES: frozenset[Role] = frozenset(
    {Role.admin, Role.doctor, Role.nurse, Role.pharmacist, Role.billing_specialist}
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"\b(?:sk|gsk|AIza)[A-Za-z0-9_\-]{8,}\b"),
    re.compile(r"(?i)(api[_-]?key|token|secret)\s*[:=]\s*\S+"),
)


def can_access_analyze(role: Role) -> bool:
    """Return True if role is permitted to request read-only advisory analysis."""
    return role in ANALYZE_ALLOWED_ROLES


def visible_departments_for_role(role: Role) -> list[Department]:
    """Return departments visible to the caller's role."""
    if role == Role.housekeeping:
        return [Department.housekeeping]
    if role == Role.transport_staff:
        return [Department.transport, Department.housekeeping]
    if role == Role.pharmacist:
        return [Department.pharmacy]
    if role == Role.billing_specialist:
        return [Department.insurance]
    return [
        Department.pharmacy,
        Department.insurance,
        Department.transport,
        Department.housekeeping,
    ]


def sanitize_activity_text(message: str, max_length: int = 320) -> str:
    """Scrub credentials, bearer tokens, and excessive prompt text before durable persistence."""
    cleaned = " ".join(message.strip().split())
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    if len(cleaned) > max_length:
        cleaned = cleaned[: max_length - 3].rstrip() + "..."
    return cleaned


def filter_agent_activity_for_role(
    role: Role,
    rows: list[tuple[AgentActivityItem, str]],
) -> list[AgentActivityItem]:
    """Filter and redact persisted agent activity items according to caller role.

    visibility_scope values: 'operational', 'pharmacy', 'insurance', 'clinical', 'admin'.
    """
    if role in {Role.admin, Role.doctor, Role.nurse}:
        return [item for item, _scope in rows]

    filtered: list[AgentActivityItem] = []
    for item, scope in rows:
        if role == Role.pharmacist:
            if scope in {"pharmacy", "operational"}:
                filtered.append(item)
        elif role == Role.billing_specialist:
            if scope in {"insurance", "operational"}:
                filtered.append(item)
        elif role in {Role.housekeeping, Role.transport_staff}:
            if scope == "operational":
                filtered.append(
                    item.model_copy(
                        update={
                            "message": (
                                f"Operational orchestration event ({item.activity_type.value})"
                                if any(
                                    term in item.message.lower()
                                    for term in ("clinical", "diagnosis", "medication", "stockout", "auth", "insurance", "pharmacy")
                                )
                                else item.message
                            )
                        }
                    )
                )
    return filtered
