"""Pydantic v2 models aligned with shared/api_contract.yaml (v1.0.0)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class WorkflowStatus(str, Enum):
    pending = "pending"
    ready = "ready"
    in_progress = "in_progress"
    blocked = "blocked"
    completed = "completed"


class Role(str, Enum):
    admin = "admin"
    doctor = "doctor"
    nurse = "nurse"
    pharmacist = "pharmacist"
    billing_specialist = "billing_specialist"
    transport_staff = "transport_staff"
    housekeeping = "housekeeping"


class Department(str, Enum):
    pharmacy = "pharmacy"
    insurance = "insurance"
    transport = "transport"
    housekeeping = "housekeeping"


class ExceptionStatus(str, Enum):
    open = "open"
    resolved = "resolved"


class AuditOutcome(str, Enum):
    success = "success"
    rejected = "rejected"


class ErrorCode(str, Enum):
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN = "FORBIDDEN"
    NOT_FOUND = "NOT_FOUND"
    STALE_VERSION = "STALE_VERSION"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ErrorResponse(BaseModel):
    error: str
    code: ErrorCode
    message: str
    details: dict[str, Any] | None = None


class PatientSummary(BaseModel):
    patient_id: str
    display_name: str | None = None
    workflow_status: WorkflowStatus
    version: int = Field(ge=1)
    blockers: list[str]
    clinical_clearance_at: datetime | None = None


class PatientListResponse(BaseModel):
    items: list[PatientSummary]


class PatientDetail(PatientSummary):
    departments: dict[str, WorkflowStatus] = Field(default_factory=dict)
    clinical_summary: str | None = None
    transport_required: bool = False


class MilestoneItem(BaseModel):
    milestone_id: str
    label: str
    status: WorkflowStatus
    dependencies: list[str]
    locked: bool


class WorkflowResponse(BaseModel):
    patient_id: str
    version: int = Field(ge=1)
    milestones: list[MilestoneItem]
    transport_required: bool = False
    blockers: list[str]


class MilestoneUpdateRequest(BaseModel):
    milestone_id: str = Field(min_length=1, max_length=64)
    target_status: WorkflowStatus
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=3, max_length=1000)


class MilestoneUpdateResponse(BaseModel):
    patient_id: str
    milestone_id: str
    status: WorkflowStatus
    version: int = Field(ge=1)
    audit_event_id: str


class ExceptionItem(BaseModel):
    exception_id: str
    patient_id: str
    department: Department
    code: str
    status: ExceptionStatus
    summary: str | None = None
    version: int = Field(ge=1)


class ExceptionListResponse(BaseModel):
    items: list[ExceptionItem]


class ExceptionResolveRequest(BaseModel):
    resolution: str = Field(min_length=3, max_length=1000)
    expected_version: int = Field(ge=1)


class AuditEventItem(BaseModel):
    event_id: str
    occurred_at: datetime
    actor_id: str | None = None
    actor_role: Role
    patient_id: str | None = None
    action: str
    outcome: AuditOutcome
    reason: str | None = None
    version: int = Field(ge=1)


class AuditListResponse(BaseModel):
    items: list[AuditEventItem]
