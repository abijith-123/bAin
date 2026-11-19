"""Strict deterministic Python DAG for hospital discharge orchestration.

Dependency graph:
    clinical_clearance -> pharmacy ----+
                       -> insurance ---+-> patient_departed -> housekeeping_dispatched -> bed_ready

All workflow transitions and dependency locks are enforced deterministically here.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.models import Department, WorkflowStatus


@dataclass(frozen=True)
class MilestoneSpec:
    milestone_id: str
    label: str
    dependencies: tuple[str, ...]
    order: int
    department: Department | None = None


DAG_SPECS: tuple[MilestoneSpec, ...] = (
    MilestoneSpec(
        milestone_id="clinical_clearance",
        label="Clinical clearance",
        dependencies=(),
        order=0,
        department=None,
    ),
    MilestoneSpec(
        milestone_id="pharmacy",
        label="Pharmacy",
        dependencies=("clinical_clearance",),
        order=1,
        department=Department.pharmacy,
    ),
    MilestoneSpec(
        milestone_id="insurance",
        label="Insurance authorization",
        dependencies=("clinical_clearance",),
        order=2,
        department=Department.insurance,
    ),
    MilestoneSpec(
        milestone_id="patient_departed",
        label="Patient departed",
        dependencies=("pharmacy", "insurance"),
        order=3,
        department=Department.transport,
    ),
    MilestoneSpec(
        milestone_id="housekeeping_dispatched",
        label="Housekeeping dispatched",
        dependencies=("patient_departed",),
        order=4,
        department=Department.housekeeping,
    ),
    MilestoneSpec(
        milestone_id="bed_ready",
        label="Bed ready",
        dependencies=("housekeeping_dispatched",),
        order=5,
        department=Department.housekeeping,
    ),
)

DAG_BY_ID: dict[str, MilestoneSpec] = {spec.milestone_id: spec for spec in DAG_SPECS}

DEPARTMENT_TO_MILESTONE: dict[Department, str] = {
    Department.pharmacy: "pharmacy",
    Department.insurance: "insurance",
    Department.transport: "patient_departed",
    Department.housekeeping: "housekeeping_dispatched",
}


class DAGValidationError(Exception):
    """Raised when a requested workflow transition violates DAG rules."""

    def __init__(self, message: str, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


def is_milestone_locked(
    milestone_id: str,
    milestone_statuses: dict[str, WorkflowStatus],
) -> bool:
    """Return True if any upstream prerequisite milestone is not completed."""
    spec = DAG_BY_ID.get(milestone_id)
    if spec is None:
        raise DAGValidationError(f"Unknown milestone '{milestone_id}'.")
    return any(
        milestone_statuses.get(dep) != WorkflowStatus.completed
        for dep in spec.dependencies
    )


def missing_dependencies(
    milestone_id: str,
    milestone_statuses: dict[str, WorkflowStatus],
) -> list[str]:
    """Return upstream milestone IDs that are not yet completed."""
    spec = DAG_BY_ID.get(milestone_id)
    if spec is None:
        raise DAGValidationError(f"Unknown milestone '{milestone_id}'.")
    return [
        dep
        for dep in spec.dependencies
        if milestone_statuses.get(dep) != WorkflowStatus.completed
    ]


def validate_milestone_transition(
    milestone_id: str,
    target_status: WorkflowStatus,
    milestone_statuses: dict[str, WorkflowStatus],
    open_exception_codes: list[str],
    transport_required: bool = False,
    transport_ready: bool = True,
) -> None:
    """Deterministically validate whether a milestone transition is allowed.

    Raises DAGValidationError on any unsafe or invalid transition.
    """
    if milestone_id not in DAG_BY_ID:
        raise DAGValidationError(
            f"Unknown milestone '{milestone_id}'.",
            details={"milestone_id": milestone_id},
        )

    current_status = milestone_statuses.get(milestone_id)
    if current_status is None:
        raise DAGValidationError(
            f"Milestone '{milestone_id}' is not initialized for this case.",
            details={"milestone_id": milestone_id},
        )

    if current_status == target_status:
        raise DAGValidationError(
            f"Milestone '{milestone_id}' is already in status '{target_status.value}'.",
            details={
                "milestone_id": milestone_id,
                "current_status": current_status.value,
                "target_status": target_status.value,
            },
        )

    if current_status == WorkflowStatus.completed:
        raise DAGValidationError(
            f"Completed milestone '{milestone_id}' cannot be modified.",
            details={
                "milestone_id": milestone_id,
                "current_status": current_status.value,
                "target_status": target_status.value,
            },
        )

    unmet = missing_dependencies(milestone_id, milestone_statuses)
    if unmet:
        raise DAGValidationError(
            f"Milestone '{milestone_id}' is locked until prerequisites complete: {', '.join(unmet)}.",
            details={
                "milestone_id": milestone_id,
                "unmet_dependencies": unmet,
            },
        )

    # Explicit hard safety guard: housekeeping cannot be dispatched until patient departure is confirmed.
    if milestone_id in {"housekeeping_dispatched", "bed_ready"}:
        if milestone_statuses.get("patient_departed") != WorkflowStatus.completed:
            raise DAGValidationError(
                "Housekeeping cannot proceed before confirmed patient departure.",
                details={
                    "milestone_id": milestone_id,
                    "required_milestone": "patient_departed",
                },
            )

    if milestone_id == "patient_departed" and transport_required and not transport_ready:
        raise DAGValidationError(
            "Patient departure requires transport readiness for this case.",
            details={"milestone_id": milestone_id, "missing_prerequisite": "transport_ready"},
        )

    # Unresolved exceptions keep a milestone blocked until resolved by an authorized specialist.
    if open_exception_codes and target_status != WorkflowStatus.blocked:
        raise DAGValidationError(
            f"Milestone '{milestone_id}' has unresolved exceptions ({', '.join(open_exception_codes)}) "
            "and cannot advance until resolved by an authorized specialist.",
            details={
                "milestone_id": milestone_id,
                "open_exceptions": open_exception_codes,
            },
        )

    if current_status == WorkflowStatus.blocked and target_status != WorkflowStatus.blocked:
        raise DAGValidationError(
            f"Blocked milestone '{milestone_id}' cannot be cleared directly via milestone update; "
            "resolve the blocking exception first.",
            details={
                "milestone_id": milestone_id,
                "current_status": current_status.value,
                "target_status": target_status.value,
            },
        )

    if target_status == WorkflowStatus.pending:
        raise DAGValidationError(
            f"Cannot transition milestone '{milestone_id}' back to 'pending'.",
            details={
                "milestone_id": milestone_id,
                "current_status": current_status.value,
                "target_status": target_status.value,
            },
        )


def compute_unlocked_promotions(
    milestone_statuses: dict[str, WorkflowStatus],
) -> dict[str, WorkflowStatus]:
    """Return milestones that should move from 'pending' to 'ready' because all dependencies completed."""
    promotions: dict[str, WorkflowStatus] = {}
    for spec in DAG_SPECS:
        if milestone_statuses.get(spec.milestone_id) == WorkflowStatus.pending:
            if not is_milestone_locked(spec.milestone_id, milestone_statuses):
                promotions[spec.milestone_id] = WorkflowStatus.ready
    return promotions


def compute_overall_workflow_status(
    milestone_statuses: dict[str, WorkflowStatus],
) -> WorkflowStatus:
    """Derive case-level workflow_status deterministically from milestone statuses."""
    statuses = list(milestone_statuses.values())
    if statuses and all(s == WorkflowStatus.completed for s in statuses):
        return WorkflowStatus.completed
    if any(s == WorkflowStatus.blocked for s in statuses):
        return WorkflowStatus.blocked
    if any(s == WorkflowStatus.in_progress for s in statuses):
        return WorkflowStatus.in_progress
    if any(s == WorkflowStatus.ready for s in statuses):
        return WorkflowStatus.ready
    return WorkflowStatus.pending
