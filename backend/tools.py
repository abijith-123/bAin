"""Official Phase 3A backend tools for ClearTurn discharge orchestration.

All state mutations go through ClearTurnStore's transactional boundaries.
No LLM has direct SQLite access or bypass authority.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from backend.auth import (
    Actor,
    filter_exceptions_for_role,
    filter_workflow_for_role,
)
from backend.dag import compute_overall_workflow_status
from backend.db import ClearTurnStore
from backend.models import (
    AgentActivityType,
    Department,
    ExceptionStatus,
    MilestoneUpdateResponse,
    Role,
    WorkflowStatus,
)

ORCHESTRATOR_ACTOR = Actor(
    actor_id="agent-clearturn-orchestrator",
    role=Role.admin,
    display_name="ClearTurn Deterministic Orchestrator",
)


class DepartmentCheckOutcome(str, Enum):
    available = "available"
    stockout = "stockout"
    approved = "approved"
    rejected = "rejected"
    timeout = "timeout"
    service_unavailable = "service_unavailable"


@dataclass(frozen=True)
class DepartmentCheckSignal:
    department: Department
    outcome: DepartmentCheckOutcome
    code: str | None = None
    summary: str | None = None


DepartmentChecker = Callable[[str], DepartmentCheckSignal]


def default_pharmacy_checker(patient_id: str) -> DepartmentCheckSignal:
    """Deterministic synthetic Pharmacy service simulator.

    Patient #402 faithfully simulates medication stockout requiring pharmacist review.
    """
    if patient_id == "402":
        return DepartmentCheckSignal(
            department=Department.pharmacy,
            outcome=DepartmentCheckOutcome.stockout,
            code="MEDICATION_STOCKOUT",
            summary="Pharmacy stockout: medication unavailable; pharmacist review required",
        )
    return DepartmentCheckSignal(
        department=Department.pharmacy,
        outcome=DepartmentCheckOutcome.available,
        code=None,
        summary="Discharge medication available and verified in synthetic pharmacy inventory",
    )


def default_insurance_checker(patient_id: str) -> DepartmentCheckSignal:
    """Deterministic synthetic Insurance/Payer service simulator.

    Patient #402 faithfully simulates ERR_AUTH_409 requiring billing specialist review.
    """
    if patient_id == "402":
        return DepartmentCheckSignal(
            department=Department.insurance,
            outcome=DepartmentCheckOutcome.rejected,
            code="ERR_AUTH_409",
            summary="Insurance authorization ERR_AUTH_409: billing specialist review required",
        )
    return DepartmentCheckSignal(
        department=Department.insurance,
        outcome=DepartmentCheckOutcome.approved,
        code=None,
        summary="Synthetic payer authorization approved",
    )


def get_discharge_dag_state(
    store: ClearTurnStore,
    patient_id: str,
    actor: Actor,
) -> dict[str, Any]:
    """Tool 1 (Read-Only): Return real DAG state, dependencies, version, locks, and role-filtered blockers."""
    workflow, statuses = store.get_workflow(patient_id)
    filtered_wf = filter_workflow_for_role(actor.role, workflow, statuses)

    all_open_exc = [
        exc
        for exc in store.list_exceptions(status_filter=ExceptionStatus.open)
        if exc.patient_id == patient_id
    ]
    visible_open_exc = filter_exceptions_for_role(actor.role, all_open_exc)

    locked_milestones = [m.milestone_id for m in filtered_wf.milestones if m.locked]
    ready_milestones = [
        m.milestone_id
        for m in filtered_wf.milestones
        if m.status == WorkflowStatus.ready and not m.locked
    ]
    blocked_milestones = [
        m.milestone_id
        for m in filtered_wf.milestones
        if m.status == WorkflowStatus.blocked
    ]

    return {
        "patient_id": filtered_wf.patient_id,
        "version": filtered_wf.version,
        "workflow_status": compute_overall_workflow_status(statuses).value,
        "transport_required": filtered_wf.transport_required,
        "milestones": [m.model_dump(mode="json") for m in filtered_wf.milestones],
        "locked_milestones": locked_milestones,
        "ready_milestones": ready_milestones,
        "blocked_milestones": blocked_milestones,
        "blockers": list(filtered_wf.blockers),
        "open_exceptions": [exc.model_dump(mode="json") for exc in visible_open_exc],
    }


def _invoke_checker_safely(
    checker: DepartmentChecker,
    department: Department,
    patient_id: str,
) -> DepartmentCheckSignal:
    try:
        return checker(patient_id)
    except TimeoutError:
        return DepartmentCheckSignal(
            department=department,
            outcome=DepartmentCheckOutcome.timeout,
            code=f"{department.value.upper()}_TIMEOUT",
            summary=f"{department.value.title()} service check timed out",
        )
    except Exception:
        return DepartmentCheckSignal(
            department=department,
            outcome=DepartmentCheckOutcome.service_unavailable,
            code=f"{department.value.upper()}_UNAVAILABLE",
            summary=f"{department.value.title()} service is temporarily unavailable",
        )


def check_pharmacy_and_insurance(
    store: ClearTurnStore,
    patient_id: str,
    *,
    pharmacy_checker: DepartmentChecker | None = None,
    insurance_checker: DepartmentChecker | None = None,
    actor: Actor | None = None,
    trace_id: str | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    """Tool 2: Execute independent Pharmacy and Insurance checks concurrently and persist permitted outcomes.

    - One service's timeout or unavailability never erases or blocks the other department's result.
    - Repeated checks are idempotent (never duplicate unresolved exceptions or transitions).
    - Existing human-review exceptions are never auto-resolved.
    """
    effective_actor = actor or ORCHESTRATOR_ACTOR
    ph_fn = pharmacy_checker or default_pharmacy_checker
    ins_fn = insurance_checker or default_insurance_checker

    # Validate patient exists before launching concurrent checks
    store.get_workflow(patient_id)

    store.record_agent_activity(
        patient_id=patient_id,
        activity_type=AgentActivityType.tool_call,
        message=f"Running concurrent Pharmacy and Insurance checks for Patient #{patient_id}.",
        visibility_scope="operational",
        trace_id=trace_id,
    )

    signals: dict[Department, DepartmentCheckSignal] = {}
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        ph_future = pool.submit(_invoke_checker_safely, ph_fn, Department.pharmacy, patient_id)
        ins_future = pool.submit(_invoke_checker_safely, ins_fn, Department.insurance, patient_id)

        for dept, fut in ((Department.pharmacy, ph_future), (Department.insurance, ins_future)):
            try:
                signals[dept] = fut.result(timeout=timeout_seconds)
            except FuturesTimeoutError:
                signals[dept] = DepartmentCheckSignal(
                    department=dept,
                    outcome=DepartmentCheckOutcome.timeout,
                    code=f"{dept.value.upper()}_TIMEOUT",
                    summary=f"{dept.value.title()} check exceeded {timeout_seconds:.1f}s timeout",
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    results: dict[str, Any] = {}
    for dept in (Department.pharmacy, Department.insurance):
        signal = signals[dept]
        persisted = store.record_department_check_outcome(
            patient_id=patient_id,
            department=dept,
            outcome=signal.outcome.value,
            code=signal.code,
            summary=signal.summary,
            actor=effective_actor,
        )
        results[dept.value] = persisted

        scope = "pharmacy" if dept == Department.pharmacy else "insurance"
        if signal.outcome in {
            DepartmentCheckOutcome.timeout,
            DepartmentCheckOutcome.service_unavailable,
        }:
            store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.error,
                message=(
                    f"{dept.value.title()} check degraded ({signal.outcome.value}); "
                    "isolated so sibling department check continues independently."
                ),
                visibility_scope=scope,
                trace_id=trace_id,
            )
        elif signal.outcome in {
            DepartmentCheckOutcome.stockout,
            DepartmentCheckOutcome.rejected,
        }:
            store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.bottleneck,
                message=(
                    f"{dept.value.title()} blocked with {signal.code or signal.outcome.value}; "
                    "specialist review required."
                ),
                visibility_scope=scope,
                trace_id=trace_id,
            )
        elif persisted.get("changed"):
            store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.recovery,
                message=f"{dept.value.title()} check returned {signal.outcome.value}; milestone updated.",
                visibility_scope=scope,
                trace_id=trace_id,
            )

    wf_after, statuses_after = store.get_workflow(patient_id)
    return {
        "patient_id": patient_id,
        "version": wf_after.version,
        "workflow_status": compute_overall_workflow_status(statuses_after).value,
        "pharmacy": results["pharmacy"],
        "insurance": results["insurance"],
    }


def update_department_milestone(
    store: ClearTurnStore,
    *,
    patient_id: str,
    milestone_id: str,
    target_status: WorkflowStatus,
    actor: Actor,
    expected_version: int,
    idempotency_key: str,
    reason: str,
) -> MilestoneUpdateResponse:
    """Tool 3 (Validated Write Path): Wrap ClearTurnStore.update_milestone with strict safety checks.

    Never blindly retries state-changing human actions after 409 STALE_VERSION.
    """
    return store.update_milestone(
        patient_id=patient_id,
        milestone_id=milestone_id,
        target_status=target_status,
        expected_version=expected_version,
        reason=reason,
        actor=actor,
        idempotency_key=idempotency_key,
    )
