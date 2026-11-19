"""Phase 3B deterministic discharge orchestrator and local CLI runner.

Python deterministic code owns all workflow decisions, state checks, routing,
and concurrency safety. The LLM is never used as the workflow state machine.
"""

from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from backend.auth import Actor, DEFAULT_DEMO_TOKENS
from backend.db import ClearTurnStore
from backend.models import AgentActivityType, Department, WorkflowStatus
from backend.tools import (
    DepartmentChecker,
    DepartmentCheckOutcome,
    DepartmentCheckSignal,
    ORCHESTRATOR_ACTOR,
    check_pharmacy_and_insurance,
    get_discharge_dag_state,
)


@dataclass(frozen=True)
class SpecialistRoute:
    department: str
    target_role: str
    exception_id: str | None
    code: str | None
    action_required: str
    human_review_required: bool = True


@dataclass(frozen=True)
class OrchestrationRunResult:
    trace_id: str
    patient_id: str
    version_before: int
    version_after: int
    workflow_status: str
    department_checks: dict[str, Any] | None
    blockers: list[str]
    specialist_routing: list[dict[str, Any]]
    next_actions: list[str]
    permitted_parallel_actions: list[str]
    protected_gates: list[str]


class DischargeOrchestrator:
    """Bounded, deterministic, idempotent orchestration engine for ClearTurn cases."""

    def __init__(
        self,
        store: ClearTurnStore,
        *,
        pharmacy_checker: DepartmentChecker | None = None,
        insurance_checker: DepartmentChecker | None = None,
    ) -> None:
        self.store = store
        self.pharmacy_checker = pharmacy_checker
        self.insurance_checker = insurance_checker

    def run_case(
        self,
        patient_id: str,
        *,
        actor: Actor | None = None,
        trace_id: str | None = None,
    ) -> OrchestrationRunResult:
        """Execute one bounded deterministic orchestration pass for a synthetic patient case."""
        effective_actor = actor or ORCHESTRATOR_ACTOR
        run_trace_id = trace_id or f"trc-{uuid.uuid4().hex[:12]}"

        # 1. Read real workflow state (read-only)
        self.store.record_agent_activity(
            patient_id=patient_id,
            activity_type=AgentActivityType.tool_call,
            message=f"[{run_trace_id}] Inspecting DAG state for Patient #{patient_id}.",
            visibility_scope="operational",
            trace_id=run_trace_id,
        )
        initial_state = get_discharge_dag_state(self.store, patient_id, effective_actor)
        version_before = int(initial_state["version"])

        milestone_map = {
            m["milestone_id"]: m for m in initial_state["milestones"]
        }

        # 2. Check independent departments concurrently if clinical clearance is done
        #    and either Pharmacy or Insurance is not yet completed.
        dept_checks: dict[str, Any] | None = None
        if (
            milestone_map["clinical_clearance"]["status"] == WorkflowStatus.completed.value
            and (
                milestone_map["pharmacy"]["status"] != WorkflowStatus.completed.value
                or milestone_map["insurance"]["status"] != WorkflowStatus.completed.value
            )
        ):
            dept_checks = check_pharmacy_and_insurance(
                self.store,
                patient_id,
                pharmacy_checker=self.pharmacy_checker,
                insurance_checker=self.insurance_checker,
                actor=effective_actor,
                trace_id=run_trace_id,
            )

        # 3. Re-read post-check workflow state
        current_state = get_discharge_dag_state(self.store, patient_id, effective_actor)
        current_ms = {m["milestone_id"]: m for m in current_state["milestones"]}
        open_exceptions = current_state["open_exceptions"]

        # 4. Build specialist routing, next actions, and permitted parallel preparation
        routing: list[SpecialistRoute] = []
        next_actions: list[str] = []
        parallel_actions: list[str] = []
        protected_gates: list[str] = []

        for exc in open_exceptions:
            dept = exc["department"]
            if dept == Department.pharmacy.value:
                routing.append(
                    SpecialistRoute(
                        department=dept,
                        target_role="pharmacist",
                        exception_id=exc["exception_id"],
                        code=exc["code"],
                        action_required=(
                            f"Pharmacist must review and resolve {exc['code']} ({exc['exception_id']}), "
                            "then confirm Pharmacy milestone completion."
                        ),
                    )
                )
                next_actions.append(
                    f"Route {exc['code']} ({exc['exception_id']}) to Pharmacist for human resolution."
                )
                self.store.record_agent_activity(
                    patient_id=patient_id,
                    activity_type=AgentActivityType.escalation,
                    message=(
                        f"[{run_trace_id}] Escalated {exc['code']} ({exc['exception_id']}) "
                        "to Pharmacist; auto-resolution prohibited."
                    ),
                    visibility_scope="pharmacy",
                    trace_id=run_trace_id,
                )
            elif dept == Department.insurance.value:
                routing.append(
                    SpecialistRoute(
                        department=dept,
                        target_role="billing_specialist",
                        exception_id=exc["exception_id"],
                        code=exc["code"],
                        action_required=(
                            f"Billing Specialist must review and resolve {exc['code']} ({exc['exception_id']}), "
                            "then confirm Insurance authorization milestone completion."
                        ),
                    )
                )
                next_actions.append(
                    f"Route {exc['code']} ({exc['exception_id']}) to Billing Specialist for human resolution."
                )
                self.store.record_agent_activity(
                    patient_id=patient_id,
                    activity_type=AgentActivityType.escalation,
                    message=(
                        f"[{run_trace_id}] Escalated {exc['code']} ({exc['exception_id']}) "
                        "to Billing Specialist; insurer denials require human review."
                    ),
                    visibility_scope="insurance",
                    trace_id=run_trace_id,
                )

        # Unrelated permitted preparation actions when another branch is blocked
        if (
            current_ms["pharmacy"]["status"] == WorkflowStatus.ready.value
            and current_ms["insurance"]["status"] == WorkflowStatus.blocked.value
        ):
            parallel_actions.append(
                "Pharmacy can complete discharge medication dispensing independently while Insurance resolves."
            )
        if (
            current_ms["insurance"]["status"] == WorkflowStatus.ready.value
            and current_ms["pharmacy"]["status"] == WorkflowStatus.blocked.value
        ):
            parallel_actions.append(
                "Insurance can finalize payer authorization independently while Pharmacy resolves stockout."
            )
        if current_state["transport_required"] and current_ms["patient_departed"]["status"] != WorkflowStatus.completed.value:
            parallel_actions.append(
                "Transport team can stage pickup readiness in parallel; physical departure remains gated."
            )

        # Ready milestone actions (human confirmation required)
        if current_ms["pharmacy"]["status"] == WorkflowStatus.ready.value:
            next_actions.append("Pharmacist can confirm Pharmacy milestone completion.")
        if current_ms["insurance"]["status"] == WorkflowStatus.ready.value:
            next_actions.append("Billing Specialist can confirm Insurance milestone completion.")

        # Hard protected gates (orchestrator NEVER auto-confirms departure or dispatches Housekeeping)
        if current_ms["patient_departed"]["status"] != WorkflowStatus.completed.value:
            protected_gates.append(
                "patient_departed requires explicit Nurse or Transport Staff confirmation."
            )
            if current_ms["patient_departed"]["status"] == WorkflowStatus.ready.value:
                next_actions.append(
                    "Authorized Nurse or Transport Staff must confirm physical patient departure."
                )
        if current_ms["housekeeping_dispatched"]["status"] != WorkflowStatus.completed.value:
            protected_gates.append(
                "housekeeping_dispatched is locked until physical patient departure is confirmed."
            )
            if current_ms["housekeeping_dispatched"]["status"] == WorkflowStatus.ready.value:
                next_actions.append(
                    "Housekeeping Staff can now dispatch room turnover cleaning."
                )
        if current_ms["bed_ready"]["status"] == WorkflowStatus.ready.value:
            next_actions.append("Housekeeping Staff can mark the bed ready once cleaning completes.")

        # 5. Record durable operational summary activity
        summary_msg = (
            f"[{run_trace_id}] Orchestration pass complete for Patient #{patient_id}: "
            f"status={current_state['workflow_status']}, version=v{current_state['version']}, "
            f"open_exceptions={len(open_exceptions)}."
        )
        self.store.record_agent_activity(
            patient_id=patient_id,
            activity_type=AgentActivityType.summary,
            message=summary_msg,
            visibility_scope="operational",
            trace_id=run_trace_id,
        )

        return OrchestrationRunResult(
            trace_id=run_trace_id,
            patient_id=patient_id,
            version_before=version_before,
            version_after=int(current_state["version"]),
            workflow_status=str(current_state["workflow_status"]),
            department_checks=dept_checks,
            blockers=list(current_state["blockers"]),
            specialist_routing=[asdict(r) for r in routing],
            next_actions=next_actions,
            permitted_parallel_actions=parallel_actions,
            protected_gates=protected_gates,
        )


def _build_scenario_checkers(
    scenario: str,
) -> tuple[DepartmentChecker | None, DepartmentChecker | None]:
    if scenario == "default":
        return None, None
    if scenario == "pharmacy_timeout_insurance_approved":
        return (
            lambda _pid: DepartmentCheckSignal(
                department=Department.pharmacy,
                outcome=DepartmentCheckOutcome.timeout,
                code="PHARMACY_TIMEOUT",
                summary="Synthetic pharmacy service timeout",
            ),
            lambda _pid: DepartmentCheckSignal(
                department=Department.insurance,
                outcome=DepartmentCheckOutcome.approved,
                code=None,
                summary="Synthetic payer authorization approved",
            ),
        )
    raise ValueError(f"Unknown scenario '{scenario}'.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run ClearTurn deterministic discharge orchestrator for a synthetic patient."
    )
    parser.add_argument("--patient", default="402", help="Synthetic patient ID (default: 402)")
    parser.add_argument("--db-path", default=None, help="Optional SQLite DB path override")
    parser.add_argument(
        "--role",
        default="admin",
        choices=["admin", "doctor", "nurse", "pharmacist", "billing_specialist"],
        help="Synthetic actor role for state inspection (default: admin)",
    )
    parser.add_argument(
        "--scenario",
        default="default",
        choices=["default", "pharmacy_timeout_insurance_approved"],
        help="Synthetic department checker scenario (default: default)",
    )
    args = parser.parse_args(argv)

    store = ClearTurnStore(db_path=args.db_path)
    store.init_and_seed()

    role_token_map = {
        "admin": "demo-token-admin",
        "doctor": "demo-token-doctor",
        "nurse": "demo-token-nurse",
        "pharmacist": "demo-token-pharmacist",
        "billing_specialist": "demo-token-billing",
    }
    actor = DEFAULT_DEMO_TOKENS[role_token_map[args.role]]
    ph_checker, ins_checker = _build_scenario_checkers(args.scenario)

    orchestrator = DischargeOrchestrator(
        store,
        pharmacy_checker=ph_checker,
        insurance_checker=ins_checker,
    )
    result = orchestrator.run_case(args.patient, actor=actor)
    print(json.dumps(asdict(result), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
