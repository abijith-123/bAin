"""Phase 3 backend orchestration, tools, activity, advisory LLM, and contract tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.auth import DEFAULT_DEMO_TOKENS
from backend.db import ClearTurnStore, StaleVersionError, WorkflowError
from backend.llm_advisory import MinimizedAdvisoryContext
from backend.main import create_app
from backend.models import (
    AgentActivityType,
    Department,
    ErrorCode,
    ExceptionStatus,
    GenerationMode,
    WorkflowStatus,
)
from backend.orchestrator import DischargeOrchestrator
from backend.tools import (
    DepartmentCheckOutcome,
    DepartmentCheckSignal,
    check_pharmacy_and_insurance,
    get_discharge_dag_state,
    update_department_milestone,
)


def _auth(token: str, idem_key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if idem_key:
        headers["Idempotency-Key"] = idem_key
    return headers


class _CapturingMockProvider:
    """Mock LLM provider that records the exact minimized context it received."""

    name = "mock-llm"

    def __init__(self, response: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.captured_contexts: list[MinimizedAdvisoryContext] = []

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        self.captured_contexts.append(context)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _make_store(tmp_path: Path, name: str = "phase3.db") -> ClearTurnStore:
    store = ClearTurnStore(db_path=str(tmp_path / name))
    store.init_and_seed()
    return store


def test_patient_402_orchestrator_produces_both_required_exceptions_and_is_idempotent(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    orchestrator = DischargeOrchestrator(store)
    admin = DEFAULT_DEMO_TOKENS["demo-token-admin"]

    run1 = orchestrator.run_case("402", actor=admin, trace_id="trc-test-402-1")
    run2 = orchestrator.run_case("402", actor=admin, trace_id="trc-test-402-2")

    assert run1.workflow_status == "blocked"
    assert run2.workflow_status == "blocked"
    assert run1.version_after == run1.version_before == 1
    assert run2.version_after == 1

    open_excs = store.list_exceptions(status_filter=ExceptionStatus.open)
    codes = sorted(e.code for e in open_excs if e.patient_id == "402")
    assert codes == ["ERR_AUTH_409", "MEDICATION_STOCKOUT"]

    # Repeated checks did not duplicate exceptions
    assert run1.department_checks is not None
    assert run1.department_checks["pharmacy"]["duplicate_prevented"] is True
    assert run1.department_checks["insurance"]["duplicate_prevented"] is True

    # Specialist routing targets Pharmacist and Billing Specialist
    target_roles = {r["target_role"] for r in run1.specialist_routing}
    assert target_roles == {"pharmacist", "billing_specialist"}


def test_fresh_case_concurrent_check_creates_both_exceptions_without_duplicates(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    store.create_synthetic_case(
        patient_id="402B",
        display_name="Synthetic Patient 402B",
        clinical_summary="Synthetic case for exception creation.",
        transport_required=True,
    )

    def stockout_ph(_pid: str) -> DepartmentCheckSignal:
        return DepartmentCheckSignal(
            department=Department.pharmacy,
            outcome=DepartmentCheckOutcome.stockout,
            code="MEDICATION_STOCKOUT",
            summary="Pharmacy stockout: medication unavailable",
        )

    def denied_ins(_pid: str) -> DepartmentCheckSignal:
        return DepartmentCheckSignal(
            department=Department.insurance,
            outcome=DepartmentCheckOutcome.rejected,
            code="ERR_AUTH_409",
            summary="Insurance authorization ERR_AUTH_409",
        )

    first = check_pharmacy_and_insurance(
        store, "402B", pharmacy_checker=stockout_ph, insurance_checker=denied_ins
    )
    second = check_pharmacy_and_insurance(
        store, "402B", pharmacy_checker=stockout_ph, insurance_checker=denied_ins
    )

    assert first["pharmacy"]["changed"] is True
    assert first["insurance"]["changed"] is True
    assert second["pharmacy"]["changed"] is False
    assert second["pharmacy"]["duplicate_prevented"] is True
    assert second["insurance"]["changed"] is False
    assert second["insurance"]["duplicate_prevented"] is True

    open_excs = [
        e for e in store.list_exceptions(status_filter=ExceptionStatus.open) if e.patient_id == "402B"
    ]
    assert len(open_excs) == 2
    assert {e.code for e in open_excs} == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}


def test_pharmacy_timeout_allows_insurance_to_complete_independently(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create_synthetic_case(
        patient_id="403",
        display_name="Synthetic Patient 403",
        clinical_summary="Synthetic case for partial outage isolation.",
        transport_required=False,
    )

    def pharmacy_timeout(_pid: str) -> DepartmentCheckSignal:
        raise TimeoutError("Simulated Pharmacy upstream timeout")

    def insurance_ok(_pid: str) -> DepartmentCheckSignal:
        return DepartmentCheckSignal(
            department=Department.insurance,
            outcome=DepartmentCheckOutcome.approved,
            code=None,
            summary="Synthetic payer approved",
        )

    result = check_pharmacy_and_insurance(
        store,
        "403",
        pharmacy_checker=pharmacy_timeout,
        insurance_checker=insurance_ok,
        trace_id="trc-timeout-403",
    )

    assert result["pharmacy"]["outcome"] == "timeout"
    assert result["pharmacy"]["degraded"] is True
    assert result["pharmacy"]["milestone_status"] == "ready"
    assert result["insurance"]["outcome"] == "approved"
    assert result["insurance"]["changed"] is True
    assert result["insurance"]["milestone_status"] == "completed"

    # Patient departure must STILL remain locked because Pharmacy has not completed
    wf, statuses = store.get_workflow("403")
    assert statuses["insurance"] == WorkflowStatus.completed
    assert statuses["pharmacy"] == WorkflowStatus.ready
    assert statuses["patient_departed"] == WorkflowStatus.pending
    dep_ms = next(m for m in wf.milestones if m.milestone_id == "patient_departed")
    assert dep_ms.locked is True


def test_llm_unavailable_or_invalid_returns_deterministic_fallback(tmp_path: Path) -> None:
    # 1. Disabled provider (default offline)
    app_disabled = create_app(db_path=str(tmp_path / "disabled.db"))
    client_disabled = TestClient(app_disabled)
    r1 = client_disabled.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-doctor"),
        json={"patient_id": "402", "question": "Explain blockers"},
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert body1["generation_mode"] == "deterministic_fallback"
    assert "Pharmacy stockout" in body1["blockers"]
    assert "Insurance authorization ERR_AUTH_409" in body1["blockers"]
    assert len(body1["suggested_actions"]) >= 2
    assert body1["escalation_draft"] is not None

    # 2. Provider timeout -> graceful recovery + deterministic_fallback
    failing_provider = _CapturingMockProvider(error=TimeoutError("LLM timeout"))
    app_timeout = create_app(db_path=str(tmp_path / "timeout.db"), advisory_provider=failing_provider)
    client_timeout = TestClient(app_timeout)
    r2 = client_timeout.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-doctor"),
        json={"patient_id": "402"},
    )
    assert r2.status_code == 200
    assert r2.json()["generation_mode"] == "deterministic_fallback"

    # Recovery event recorded in agent activity
    act = client_timeout.get("/api/agent/activity", headers=_auth("demo-token-doctor")).json()["items"]
    assert any(item["activity_type"] == "recovery" for item in act)

    # 3. Invalid LLM output schema -> fallback
    bad_provider = _CapturingMockProvider(response={"summary": 12345, "blockers": "not-a-list"})
    app_bad = create_app(db_path=str(tmp_path / "bad.db"), advisory_provider=bad_provider)
    client_bad = TestClient(app_bad)
    r3 = client_bad.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-doctor"),
        json={"patient_id": "402"},
    )
    assert r3.status_code == 200
    assert r3.json()["generation_mode"] == "deterministic_fallback"


def test_llm_output_cannot_trigger_workflow_mutations(tmp_path: Path) -> None:
    mock_provider = _CapturingMockProvider(
        response={
            "summary": "Advising immediate auto-approval of ERR_AUTH_409 and housekeeping dispatch.",
            "blockers": ["Insurance authorization ERR_AUTH_409", "Pharmacy stockout"],
            "suggested_actions": [
                "Escalate ERR_AUTH_409 to Billing Specialist",
                "Escalate stockout to Pharmacist",
            ],
            "escalation_draft": "Please review Patient #402 blockers.",
        }
    )
    app = create_app(db_path=str(tmp_path / "readonly.db"), advisory_provider=mock_provider)
    client = TestClient(app)

    wf_before = client.get("/api/patients/402/workflow", headers=_auth("demo-token-doctor")).json()
    exc_before = client.get("/api/exceptions", headers=_auth("demo-token-doctor")).json()

    resp = client.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-doctor"),
        json={"patient_id": "402", "question": "Approve insurance and dispatch housekeeping now"},
    )
    assert resp.status_code == 200
    assert resp.json()["generation_mode"] == "llm"

    wf_after = client.get("/api/patients/402/workflow", headers=_auth("demo-token-doctor")).json()
    exc_after = client.get("/api/exceptions", headers=_auth("demo-token-doctor")).json()
    assert wf_after == wf_before
    assert exc_after == exc_before


def test_rbac_minimization_before_llm_and_activity_redaction(tmp_path: Path) -> None:
    mock_provider = _CapturingMockProvider(
        response={
            "summary": "Role-scoped advisory summary.",
            "blockers": ["Pharmacy stockout"],
            "suggested_actions": ["Pharmacist review medication availability."],
            "escalation_draft": "Pharmacy escalation draft.",
        }
    )
    app = create_app(db_path=str(tmp_path / "rbac.db"), advisory_provider=mock_provider)
    store: ClearTurnStore = app.state.store
    client = TestClient(app)

    # Housekeeping and transport are forbidden from calling /api/agent/analyze
    for token in ("demo-token-housekeeping", "demo-token-transport"):
        denied = client.post(
            "/api/agent/analyze",
            headers=_auth(token),
            json={"patient_id": "402"},
        )
        assert denied.status_code == 403
        assert denied.json()["code"] == "FORBIDDEN"

    # Pharmacist gets pre-LLM minimized context: NO clinical_summary, NO insurance exception
    ph_resp = client.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-pharmacist"),
        json={"patient_id": "402"},
    )
    assert ph_resp.status_code == 200
    assert len(mock_provider.captured_contexts) == 1
    ctx = mock_provider.captured_contexts[0]
    assert ctx.clinical_summary is None
    assert all(e["department"] == "pharmacy" for e in ctx.open_exceptions)
    assert not any("ERR_AUTH_409" in b for b in ctx.blockers)

    # Run orchestrator and record a message containing a synthetic secret to test pre-persistence scrubbing
    orchestrator = DischargeOrchestrator(store)
    orchestrator.run_case("402", actor=DEFAULT_DEMO_TOKENS["demo-token-admin"], trace_id="trc-rbac-1")
    store.record_agent_activity(
        patient_id="402",
        activity_type=AgentActivityType.error,
        message="Provider error with Bearer demo-secret-token and key sk-1234567890abcdef",
        visibility_scope="clinical",
    )

    doc_act = client.get("/api/agent/activity", headers=_auth("demo-token-doctor")).json()["items"]
    hk_act = client.get("/api/agent/activity", headers=_auth("demo-token-housekeeping")).json()["items"]
    ph_act = client.get("/api/agent/activity", headers=_auth("demo-token-pharmacist")).json()["items"]

    # Secrets scrubbed before persistence even for doctor
    for item in doc_act:
        assert "demo-secret-token" not in item["message"]
        assert "sk-1234567890abcdef" not in item["message"]

    # Housekeeping never sees pharmacy/insurance escalations or clinical errors
    assert len(hk_act) > 0
    for item in hk_act:
        lower = item["message"].lower()
        assert "err_auth_409" not in lower
        assert "stockout" not in lower
        assert "sk-" not in lower
        assert "clinical" not in lower

    # Pharmacist never sees insurance ERR_AUTH_409 escalations
    for item in ph_act:
        assert "err_auth_409" not in item["message"].lower()


def test_dashboard_summary_and_departments_status_match_contract_and_live_sqlite(
    tmp_path: Path,
) -> None:
    app = create_app(db_path=str(tmp_path / "metrics.db"))
    client = TestClient(app)

    # Unauthenticated rejected
    assert client.get("/api/dashboard/summary").status_code == 401
    assert client.get("/api/departments/status").status_code == 401

    # Initial Patient #402 state as doctor
    doc_dash = client.get("/api/dashboard/summary", headers=_auth("demo-token-doctor"))
    assert doc_dash.status_code == 200
    d = doc_dash.json()
    assert set(d.keys()) == {
        "generated_at",
        "active_cases",
        "blocked_cases",
        "completed_today",
        "available_beds",
        "pending_specialist_reviews",
    }
    assert d["active_cases"] == 1
    assert d["blocked_cases"] == 1
    assert d["completed_today"] == 0
    assert d["available_beds"] == 0
    assert d["pending_specialist_reviews"] == 2

    # Role-filtered pending_specialist_reviews & departments for housekeeping
    hk_dash = client.get("/api/dashboard/summary", headers=_auth("demo-token-housekeeping")).json()
    assert hk_dash["pending_specialist_reviews"] == 0

    hk_depts = client.get("/api/departments/status", headers=_auth("demo-token-housekeeping"))
    assert hk_depts.status_code == 200
    hk_items = hk_depts.json()["items"]
    assert [item["department"] for item in hk_items] == ["housekeeping"]
    assert hk_items[0]["open_cases"] == 1
    assert hk_items[0]["blocked_cases"] == 0

    doc_depts = client.get("/api/departments/status", headers=_auth("demo-token-doctor")).json()["items"]
    by_dept = {item["department"]: item for item in doc_depts}
    assert set(by_dept.keys()) == {"pharmacy", "insurance", "transport", "housekeeping"}
    assert by_dept["pharmacy"]["status"] == "blocked"
    assert by_dept["pharmacy"]["blocked_cases"] == 1
    assert by_dept["insurance"]["status"] == "blocked"
    assert by_dept["insurance"]["blocked_cases"] == 1


def test_update_department_milestone_enforces_stale_version_and_idempotency(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create_synthetic_case(
        patient_id="404",
        display_name="Synthetic Patient 404",
        clinical_summary="Ready for pharmacy completion.",
    )
    pharmacist = DEFAULT_DEMO_TOKENS["demo-token-pharmacist"]

    # Exact replay with same Idempotency-Key returns same response
    res1 = update_department_milestone(
        store,
        patient_id="404",
        milestone_id="pharmacy",
        target_status=WorkflowStatus.completed,
        actor=pharmacist,
        expected_version=1,
        idempotency_key="tool-ms-404-pharm-01",
        reason="Medication dispensed",
    )
    res2 = update_department_milestone(
        store,
        patient_id="404",
        milestone_id="pharmacy",
        target_status=WorkflowStatus.completed,
        actor=pharmacist,
        expected_version=1,
        idempotency_key="tool-ms-404-pharm-01",
        reason="Medication dispensed",
    )
    assert res1 == res2

    # Stale version is NEVER blindly retried
    billing = DEFAULT_DEMO_TOKENS["demo-token-billing"]
    with pytest.raises(StaleVersionError) as exc_info:
        update_department_milestone(
            store,
            patient_id="404",
            milestone_id="insurance",
            target_status=WorkflowStatus.completed,
            actor=billing,
            expected_version=1,  # stale: current version is 2
            idempotency_key="tool-ms-404-ins-stale",
            reason="Payer cleared",
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCode.STALE_VERSION


def test_transport_required_blocks_departure_when_transport_not_ready(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    store.create_synthetic_case(
        patient_id="405",
        display_name="Synthetic Patient 405",
        clinical_summary="Requires hospital transport before departure.",
        transport_required=True,
        transport_status=WorkflowStatus.pending,
        milestone_statuses={
            "clinical_clearance": WorkflowStatus.completed,
            "pharmacy": WorkflowStatus.completed,
            "insurance": WorkflowStatus.completed,
            "patient_departed": WorkflowStatus.ready,
            "housekeeping_dispatched": WorkflowStatus.pending,
            "bed_ready": WorkflowStatus.pending,
        },
    )
    nurse = DEFAULT_DEMO_TOKENS["demo-token-nurse"]
    admin = DEFAULT_DEMO_TOKENS["demo-token-admin"]
    transport = DEFAULT_DEMO_TOKENS["demo-token-transport"]
    housekeeping = DEFAULT_DEMO_TOKENS["demo-token-housekeeping"]

    # 1. Departure is blocked while transport_status is pending
    with pytest.raises(WorkflowError) as exc_info:
        update_department_milestone(
            store,
            patient_id="405",
            milestone_id="patient_departed",
            target_status=WorkflowStatus.completed,
            actor=nurse,
            expected_version=1,
            idempotency_key="tool-405-dep-no-transport",
            reason="Attempting departure before transport ready",
        )
    assert exc_info.value.status_code == 409
    assert exc_info.value.code == ErrorCode.INVALID_TRANSITION
    assert "transport" in exc_info.value.message.lower()

    # 2. Unauthorized role (admin) cannot mark transport ready
    with pytest.raises(WorkflowError) as forbidden_exc:
        update_department_milestone(
            store,
            patient_id="405",
            milestone_id="transport",
            target_status=WorkflowStatus.ready,
            actor=admin,
            expected_version=1,
            idempotency_key="tool-405-transport-admin-forbidden",
            reason="Admin attempting unauthorized transport readiness update",
        )
    assert forbidden_exc.value.status_code == 403
    assert forbidden_exc.value.code == ErrorCode.FORBIDDEN

    # 3. Authorized transport_staff updates transport readiness -> ready
    tr_res = update_department_milestone(
        store,
        patient_id="405",
        milestone_id="transport",
        target_status=WorkflowStatus.ready,
        actor=transport,
        expected_version=1,
        idempotency_key="tool-405-transport-ready",
        reason="Wheelchair and transport escort staged at Ward 4",
    )
    assert tr_res.milestone_id == "transport"
    assert tr_res.status == WorkflowStatus.ready
    assert tr_res.version == 2

    # 4. Now authorized nurse can confirm physical departure, unlocking housekeeping
    dep_res = update_department_milestone(
        store,
        patient_id="405",
        milestone_id="patient_departed",
        target_status=WorkflowStatus.completed,
        actor=nurse,
        expected_version=2,
        idempotency_key="tool-405-departed-after-transport",
        reason="Patient escorted to exit with hospital transport",
    )
    assert dep_res.status == WorkflowStatus.completed
    assert dep_res.version == 3

    # 5. Housekeeping can now dispatch and complete bed turnover
    hk_res = update_department_milestone(
        store,
        patient_id="405",
        milestone_id="housekeeping_dispatched",
        target_status=WorkflowStatus.completed,
        actor=housekeeping,
        expected_version=3,
        idempotency_key="tool-405-hk-dispatch",
        reason="EVS dispatched after confirmed departure",
    )
    assert hk_res.status == WorkflowStatus.completed
    assert hk_res.version == 4


def test_blank_insurance_resolution_rejected_and_llm_failure_does_not_block_workflow(
    tmp_path: Path,
) -> None:
    failing_provider = _CapturingMockProvider(error=TimeoutError("Upstream LLM timeout"))
    app = create_app(db_path=str(tmp_path / "safety.db"), advisory_provider=failing_provider)
    client = TestClient(app)

    # 1. LLM timeout falls back gracefully without blocking deterministic endpoints
    advisory_resp = client.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-doctor"),
        json={"patient_id": "402"},
    )
    assert advisory_resp.status_code == 200
    assert advisory_resp.json()["generation_mode"] == "deterministic_fallback"

    # 2. Blank / whitespace-only human resolution is rejected with 422 VALIDATION_ERROR
    blank_res = client.post(
        "/api/exceptions/ex-ins-402/resolve",
        headers=_auth("demo-token-billing", "idem-blank-ins-402"),
        json={"resolution": "     ", "expected_version": 1},
    )
    assert blank_res.status_code == 422
    assert blank_res.json()["code"] == "VALIDATION_ERROR"

    # 3. Valid recorded billing specialist resolution succeeds despite LLM outage
    valid_res = client.post(
        "/api/exceptions/ex-ins-402/resolve",
        headers=_auth("demo-token-billing", "idem-valid-ins-402"),
        json={"resolution": "Resubmitted payer auth ERR_AUTH_409; approval #9912 recorded", "expected_version": 1},
    )
    assert valid_res.status_code == 200
    assert valid_res.json()["status"] == "resolved"
    assert valid_res.json()["version"] == 2


def test_llm_cannot_leak_unauthorized_blockers_across_roles(tmp_path: Path) -> None:
    # Even if a buggy LLM attempts to echo an Insurance blocker to a Pharmacist,
    # backend response validation anchors blockers strictly to the role-filtered ground truth.
    leaky_provider = _CapturingMockProvider(
        response={
            "summary": "Pharmacy summary.",
            "blockers": ["Pharmacy stockout", "Insurance authorization ERR_AUTH_409"],
            "suggested_actions": ["Review medication stockout."],
            "escalation_draft": None,
        }
    )
    app = create_app(db_path=str(tmp_path / "blocker_anchor.db"), advisory_provider=leaky_provider)
    client = TestClient(app)

    resp = client.post(
        "/api/agent/analyze",
        headers=_auth("demo-token-pharmacist"),
        json={"patient_id": "402"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_mode"] == "llm"
    assert body["blockers"] == ["Pharmacy stockout"]
    assert all("ERR_AUTH_409" not in b for b in body["blockers"])


def test_insurance_rejection_requires_human_resolution_and_departure_housekeeping_stay_protected(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    orchestrator = DischargeOrchestrator(
        store,
        # Even if a later automated check returns approved/available, open human exceptions MUST NOT auto-resolve
        pharmacy_checker=lambda _pid: DepartmentCheckSignal(
            department=Department.pharmacy,
            outcome=DepartmentCheckOutcome.available,
        ),
        insurance_checker=lambda _pid: DepartmentCheckSignal(
            department=Department.insurance,
            outcome=DepartmentCheckOutcome.approved,
        ),
    )

    run = orchestrator.run_case("402", actor=DEFAULT_DEMO_TOKENS["demo-token-admin"])
    assert run.workflow_status == "blocked"
    assert run.department_checks is not None
    assert run.department_checks["pharmacy"]["held_for_human_review"] is True
    assert run.department_checks["insurance"]["held_for_human_review"] is True

    # Open exceptions remain open until authorized human resolves them
    open_excs = store.list_exceptions(status_filter=ExceptionStatus.open)
    assert {e.code for e in open_excs} == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}

    # Even on a clean case where both pharmacy and insurance complete via orchestrator,
    # patient_departed and housekeeping_dispatched are NEVER auto-confirmed or auto-dispatched.
    store.create_synthetic_case(
        patient_id="406",
        display_name="Synthetic Patient 406",
        clinical_summary="Clean case; both checks pass.",
        transport_required=True,
        transport_status=WorkflowStatus.ready,
    )
    clean_run = orchestrator.run_case("406", actor=DEFAULT_DEMO_TOKENS["demo-token-admin"])
    dag_state = get_discharge_dag_state(store, "406", DEFAULT_DEMO_TOKENS["demo-token-admin"])
    by_id = {m["milestone_id"]: m for m in dag_state["milestones"]}

    assert by_id["pharmacy"]["status"] == "completed"
    assert by_id["insurance"]["status"] == "completed"
    assert by_id["patient_departed"]["status"] == "ready"
    assert by_id["patient_departed"]["locked"] is False
    assert by_id["housekeeping_dispatched"]["status"] == "pending"
    assert by_id["housekeeping_dispatched"]["locked"] is True
    assert by_id["bed_ready"]["status"] == "pending"
    assert len(clean_run.protected_gates) >= 2
