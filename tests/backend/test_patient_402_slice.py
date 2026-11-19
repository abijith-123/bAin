"""Phase 1 vertical slice tests for synthetic Patient #402 (ClearTurn / bAin)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.auth import (
    Actor,
    DEFAULT_DEMO_TOKENS,
    redact_advisory_text_for_role,
)
from backend.db import ClearTurnStore, WorkflowError
from backend.main import create_app
from backend.models import ErrorCode, Role, WorkflowStatus


def _auth(role_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer demo-token-{role_key}"}


def _headers(role_key: str, idem_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer demo-token-{role_key}",
        "Idempotency-Key": idem_key,
    }


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    db_file = str(tmp_path / "clearturn_test.db")
    app = create_app(db_path=db_file)
    return TestClient(app)


@pytest.fixture()
def store(tmp_path: Path) -> ClearTurnStore:
    s = ClearTurnStore(db_path=str(tmp_path / "clearturn_store.db"))
    s.init_and_seed()
    return s


def test_health_endpoint_preserved(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "bAin-api"}


def test_patient_402_initial_seed_state(client: TestClient) -> None:
    wf_resp = client.get("/api/patients/402/workflow", headers=_auth("doctor"))
    assert wf_resp.status_code == 200
    wf = wf_resp.json()
    assert wf["patient_id"] == "402"
    assert wf["version"] == 1
    assert set(wf["blockers"]) == {"Pharmacy stockout", "Insurance authorization ERR_AUTH_409"}

    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["clinical_clearance"]["status"] == "completed"
    assert by_id["clinical_clearance"]["locked"] is False
    assert by_id["pharmacy"]["status"] == "blocked"
    assert by_id["pharmacy"]["locked"] is False
    assert by_id["insurance"]["status"] == "blocked"
    assert by_id["insurance"]["locked"] is False
    assert by_id["patient_departed"]["status"] == "pending"
    assert by_id["patient_departed"]["locked"] is True
    assert by_id["housekeeping_dispatched"]["status"] == "pending"
    assert by_id["housekeeping_dispatched"]["locked"] is True
    assert by_id["bed_ready"]["status"] == "pending"
    assert by_id["bed_ready"]["locked"] is True

    exc_resp = client.get("/api/exceptions", headers=_auth("admin"))
    assert exc_resp.status_code == 200
    excs = {e["exception_id"]: e for e in exc_resp.json()["items"]}
    assert excs["ex-ph-402"]["code"] == "MEDICATION_STOCKOUT"
    assert excs["ex-ph-402"]["status"] == "open"
    assert excs["ex-ins-402"]["code"] == "ERR_AUTH_409"
    assert excs["ex-ins-402"]["status"] == "open"


def test_premature_departure_and_housekeeping_rejected_and_audited(client: TestClient) -> None:
    # Premature departure attempt by nurse while pharmacy & insurance are blocked
    dep_resp = client.post(
        "/api/patients/402/milestones",
        headers=_headers("nurse", "idem-premature-dep-001"),
        json={
            "milestone_id": "patient_departed",
            "target_status": "completed",
            "expected_version": 1,
            "reason": "Attempting premature departure",
        },
    )
    assert dep_resp.status_code == 409
    assert dep_resp.json()["code"] == "INVALID_TRANSITION"

    # Premature housekeeping dispatch before confirmed departure
    hk_resp = client.post(
        "/api/patients/402/milestones",
        headers=_headers("housekeeping", "idem-premature-hk-001"),
        json={
            "milestone_id": "housekeeping_dispatched",
            "target_status": "completed",
            "expected_version": 1,
            "reason": "Attempting premature room cleaning",
        },
    )
    assert hk_resp.status_code == 409
    assert hk_resp.json()["code"] == "INVALID_TRANSITION"

    # Verify state and version were NOT mutated
    wf = client.get("/api/patients/402/workflow", headers=_auth("admin")).json()
    assert wf["version"] == 1
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["patient_departed"]["status"] == "pending"
    assert by_id["housekeeping_dispatched"]["status"] == "pending"

    # Verify both rejected attempts produced durable audit events
    audit = client.get("/api/patients/402/audit", headers=_auth("admin")).json()["items"]
    assert len(audit) == 2
    assert all(ev["outcome"] == "rejected" for ev in audit)
    assert audit[0]["action"] == "milestone:patient_departed->completed"
    assert audit[1]["action"] == "milestone:housekeeping_dispatched->completed"


def test_rbac_enforcement_and_no_admin_bypass(client: TestClient) -> None:
    # Unauthenticated request rejected with 401
    unauth = client.get("/api/patients/402")
    assert unauth.status_code == 401
    assert unauth.json()["code"] == "UNAUTHENTICATED"

    # Admin cannot resolve pharmacy or insurance exceptions
    admin_ph = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("admin", "idem-admin-ph-001"),
        json={"resolution": "Admin override attempt", "expected_version": 1},
    )
    assert admin_ph.status_code == 403
    assert admin_ph.json()["code"] == "FORBIDDEN"

    # Billing cannot resolve pharmacy exception; pharmacist cannot resolve insurance exception
    cross_1 = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("billing", "idem-cross-001"),
        json={"resolution": "Cross-dept override attempt", "expected_version": 1},
    )
    assert cross_1.status_code == 403

    cross_2 = client.post(
        "/api/exceptions/ex-ins-402/resolve",
        headers=_headers("pharmacist", "idem-cross-002"),
        json={"resolution": "Cross-dept override attempt", "expected_version": 1},
    )
    assert cross_2.status_code == 403

    # Nurse cannot override insurance milestone directly
    nurse_ins = client.post(
        "/api/patients/402/milestones",
        headers=_headers("nurse", "idem-nurse-ins-001"),
        json={
            "milestone_id": "insurance",
            "target_status": "completed",
            "expected_version": 1,
            "reason": "Nurse attempting insurance override",
        },
    )
    assert nurse_ins.status_code == 403
    assert nurse_ins.json()["code"] == "FORBIDDEN"

    # Verify version stayed 1 and all 4 denials were audited as rejected
    wf = client.get("/api/patients/402/workflow", headers=_auth("admin")).json()
    assert wf["version"] == 1
    audit = client.get("/api/patients/402/audit", headers=_auth("admin")).json()["items"]
    assert len(audit) == 4
    assert all(ev["outcome"] == "rejected" for ev in audit)


def test_full_patient_402_workflow_resolution_and_departure_guards(client: TestClient) -> None:
    # 1. Pharmacist resolves pharmacy stockout -> moves pharmacy blocked -> ready (NOT completed!)
    r_ph = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("pharmacist", "idem-res-ph-001"),
        json={
            "resolution": "Approved therapeutic equivalent and restocked dose",
            "expected_version": 1,
        },
    )
    assert r_ph.status_code == 200
    assert r_ph.json()["status"] == "resolved"
    assert r_ph.json()["version"] == 2

    wf = client.get("/api/patients/402/workflow", headers=_auth("doctor")).json()
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["pharmacy"]["status"] == "ready"
    assert by_id["insurance"]["status"] == "blocked"

    # 2. Billing specialist resolves ERR_AUTH_409 -> moves insurance blocked -> ready (NOT completed!)
    r_ins = client.post(
        "/api/exceptions/ex-ins-402/resolve",
        headers=_headers("billing", "idem-res-ins-001"),
        json={
            "resolution": "Submitted updated ICD-10 documentation; insurer confirmed clearance",
            "expected_version": 2,
        },
    )
    assert r_ins.status_code == 200
    assert r_ins.json()["status"] == "resolved"
    assert r_ins.json()["version"] == 3

    wf = client.get("/api/patients/402/workflow", headers=_auth("doctor")).json()
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["pharmacy"]["status"] == "ready"
    assert by_id["insurance"]["status"] == "ready"
    assert by_id["patient_departed"]["locked"] is True

    # Resolution alone must NOT unlock departure
    dep_too_early = client.post(
        "/api/patients/402/milestones",
        headers=_headers("transport", "idem-dep-early-001"),
        json={
            "milestone_id": "patient_departed",
            "target_status": "completed",
            "expected_version": 3,
            "reason": "Departure before pharmacy/insurance completion",
        },
    )
    assert dep_too_early.status_code == 409
    assert dep_too_early.json()["code"] == "INVALID_TRANSITION"

    # 3. Pharmacist confirms pharmacy completed (version 3 -> 4)
    c_ph = client.post(
        "/api/patients/402/milestones",
        headers=_headers("pharmacist", "idem-comp-ph-001"),
        json={
            "milestone_id": "pharmacy",
            "target_status": "completed",
            "expected_version": 3,
            "reason": "Discharge medication dispensed and counselled",
        },
    )
    assert c_ph.status_code == 200
    assert c_ph.json()["status"] == "completed"
    assert c_ph.json()["version"] == 4

    # 4. Billing specialist confirms insurance completed (version 4 -> 5)
    c_ins = client.post(
        "/api/patients/402/milestones",
        headers=_headers("billing", "idem-comp-ins-001"),
        json={
            "milestone_id": "insurance",
            "target_status": "completed",
            "expected_version": 4,
            "reason": "Final payer authorization number verified",
        },
    )
    assert c_ins.status_code == 200
    assert c_ins.json()["status"] == "completed"
    assert c_ins.json()["version"] == 5

    # Departure is now unlocked & ready, but housekeeping MUST remain locked!
    wf = client.get("/api/patients/402/workflow", headers=_auth("doctor")).json()
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["patient_departed"]["status"] == "ready"
    assert by_id["patient_departed"]["locked"] is False
    assert by_id["housekeeping_dispatched"]["status"] == "pending"
    assert by_id["housekeeping_dispatched"]["locked"] is True

    # Premature housekeeping STILL rejected before departure confirmation
    hk_before_dep = client.post(
        "/api/patients/402/milestones",
        headers=_headers("housekeeping", "idem-hk-early-002"),
        json={
            "milestone_id": "housekeeping_dispatched",
            "target_status": "completed",
            "expected_version": 5,
            "reason": "Trying to dispatch housekeeping before departure",
        },
    )
    assert hk_before_dep.status_code == 409
    assert hk_before_dep.json()["code"] == "INVALID_TRANSITION"

    # 5. Transport staff confirms actual patient departure (version 5 -> 6)
    c_dep = client.post(
        "/api/patients/402/milestones",
        headers=_headers("transport", "idem-dep-ok-001"),
        json={
            "milestone_id": "patient_departed",
            "target_status": "completed",
            "expected_version": 5,
            "reason": "Patient escorted to curbside pickup; room vacated",
        },
    )
    assert c_dep.status_code == 200
    assert c_dep.json()["version"] == 6

    # Housekeeping is now unlocked!
    wf = client.get("/api/patients/402/workflow", headers=_auth("housekeeping")).json()
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["housekeeping_dispatched"]["status"] == "ready"
    assert by_id["housekeeping_dispatched"]["locked"] is False

    # 6. Housekeeping dispatches and marks bed ready (version 6 -> 7 -> 8)
    c_hk = client.post(
        "/api/patients/402/milestones",
        headers=_headers("housekeeping", "idem-hk-ok-001"),
        json={
            "milestone_id": "housekeeping_dispatched",
            "target_status": "completed",
            "expected_version": 6,
            "reason": "Environmental services sanitization completed",
        },
    )
    assert c_hk.status_code == 200
    assert c_hk.json()["version"] == 7

    c_bed = client.post(
        "/api/patients/402/milestones",
        headers=_headers("housekeeping", "idem-bed-ok-001"),
        json={
            "milestone_id": "bed_ready",
            "target_status": "completed",
            "expected_version": 7,
            "reason": "Bed inspected and marked ready for next admission",
        },
    )
    assert c_bed.status_code == 200
    assert c_bed.json()["version"] == 8

    detail = client.get("/api/patients/402", headers=_auth("doctor")).json()
    assert detail["workflow_status"] == "completed"
    assert detail["version"] == 8


def test_role_based_phi_redaction_for_housekeeping_and_transport(client: TestClient) -> None:
    # Doctor sees full clinical fields
    doc_detail = client.get("/api/patients/402", headers=_auth("doctor")).json()
    assert doc_detail["display_name"] == "Synthetic Patient 402"
    assert doc_detail["clinical_summary"] is not None
    assert doc_detail["clinical_clearance_at"] is not None

    # Housekeeping must NEVER receive clinical PHI, display_name, or pharmacy/insurance exceptions
    hk_detail = client.get("/api/patients/402", headers=_auth("housekeeping")).json()
    assert hk_detail["display_name"] is None
    assert hk_detail["clinical_summary"] is None
    assert hk_detail["clinical_clearance_at"] is None
    assert set(hk_detail["departments"].keys()) == {"housekeeping"}
    assert hk_detail["blockers"] == ["Awaiting patient departure"]

    hk_excs = client.get("/api/exceptions", headers=_auth("housekeeping")).json()["items"]
    assert hk_excs == []

    # Transport must NEVER receive clinical PHI or display_name
    tr_detail = client.get("/api/patients/402", headers=_auth("transport")).json()
    assert tr_detail["display_name"] is None
    assert tr_detail["clinical_summary"] is None
    assert tr_detail["clinical_clearance_at"] is None
    assert "pharmacy" not in tr_detail["departments"]
    assert "insurance" not in tr_detail["departments"]
    assert "ERR_AUTH_409" not in " ".join(tr_detail["blockers"])
    assert "stockout" not in " ".join(tr_detail["blockers"]).lower()

    tr_excs = client.get("/api/exceptions", headers=_auth("transport")).json()["items"]
    assert tr_excs == []

    # AI advisory redaction helper strips clinical content for housekeeping & transport
    clinical_text = "Patient has post-op anticoagulation requirement."
    assert "anticoagulation" not in (redact_advisory_text_for_role(Role.housekeeping, clinical_text) or "")
    assert "anticoagulation" not in (redact_advisory_text_for_role(Role.transport_staff, clinical_text) or "")
    assert redact_advisory_text_for_role(Role.doctor, clinical_text) == clinical_text


def test_concurrent_updates_prevent_silent_overwrite_and_support_safe_retry(
    client: TestClient, store: ClearTurnStore
) -> None:
    # Part A: API-level concurrent resolution with identical stale expected_version=1
    def resolve_ph() -> int:
        r = client.post(
            "/api/exceptions/ex-ph-402/resolve",
            headers=_headers("pharmacist", "idem-conc-ph-001"),
            json={"resolution": "Pharmacist cleared stockout", "expected_version": 1},
        )
        return r.status_code

    def resolve_ins() -> int:
        r = client.post(
            "/api/exceptions/ex-ins-402/resolve",
            headers=_headers("billing", "idem-conc-ins-001"),
            json={"resolution": "Billing cleared ERR_AUTH_409", "expected_version": 1},
        )
        return r.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        f1 = pool.submit(resolve_ph)
        f2 = pool.submit(resolve_ins)
        statuses = sorted([f1.result(), f2.result()])

    assert statuses == [200, 409]

    # Verify stale version error includes current_version
    stale_resp = client.post(
        "/api/exceptions/ex-ins-402/resolve",
        headers=_headers("billing", "idem-stale-check-001"),
        json={"resolution": "Stale version attempt", "expected_version": 1},
    )
    assert stale_resp.status_code == 409
    assert stale_resp.json()["code"] == "STALE_VERSION"
    assert stale_resp.json()["details"]["current_version"] == 2

    # Part B: Safe refresh-and-retry helper for independent department operations
    ph_actor: Actor = DEFAULT_DEMO_TOKENS["demo-token-pharmacist"]
    bill_actor: Actor = DEFAULT_DEMO_TOKENS["demo-token-billing"]
    hk_actor: Actor = DEFAULT_DEMO_TOKENS["demo-token-housekeeping"]

    def retry_ph() -> int:
        res = store.execute_with_optimistic_retry(
            "402",
            lambda ver: store.resolve_exception(
                exception_id="ex-ph-402",
                resolution="Pharmacist resolved with safe retry",
                expected_version=ver,
                actor=ph_actor,
                idempotency_key="idem-retry-ph-001",
            ),
        )
        return res.version

    def retry_ins() -> int:
        res = store.execute_with_optimistic_retry(
            "402",
            lambda ver: store.resolve_exception(
                exception_id="ex-ins-402",
                resolution="Billing resolved with safe retry",
                expected_version=ver,
                actor=bill_actor,
                idempotency_key="idem-retry-ins-001",
            ),
        )
        return res.version

    with ThreadPoolExecutor(max_workers=2) as pool:
        v1 = pool.submit(retry_ph).result()
        v2 = pool.submit(retry_ins).result()

    assert sorted([v1, v2]) == [2, 3]

    # Retry helper must NEVER bypass DAG validation
    with pytest.raises(WorkflowError) as exc_info:
        store.execute_with_optimistic_retry(
            "402",
            lambda ver: store.update_milestone(
                patient_id="402",
                milestone_id="housekeeping_dispatched",
                target_status=WorkflowStatus.completed,
                expected_version=ver,
                reason="Invalid retry must fail validation",
                actor=hk_actor,
                idempotency_key="idem-retry-invalid-001",
            ),
        )
    assert exc_info.value.code == ErrorCode.INVALID_TRANSITION


def test_idempotent_replay_and_payload_mismatch_rejection(client: TestClient) -> None:
    key = "idem-replay-test-001"
    payload = {
        "resolution": "Restocked medication and verified dosage",
        "expected_version": 1,
    }

    first = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("pharmacist", key),
        json=payload,
    )
    assert first.status_code == 200
    assert first.json()["version"] == 2

    # Exact replay must return identical payload without incrementing version or duplicating audit
    replay = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("pharmacist", key),
        json=payload,
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()

    audit_after_replay = client.get("/api/patients/402/audit", headers=_auth("admin")).json()["items"]
    assert len(audit_after_replay) == 1

    # Reusing the same Idempotency-Key with a DIFFERENT payload must return 409 IDEMPOTENCY_CONFLICT
    mismatch = client.post(
        "/api/exceptions/ex-ph-402/resolve",
        headers=_headers("pharmacist", key),
        json={"resolution": "Different resolution text", "expected_version": 1},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "IDEMPOTENCY_CONFLICT"

    audit_after_mismatch = client.get("/api/patients/402/audit", headers=_auth("admin")).json()["items"]
    assert len(audit_after_mismatch) == 2
    assert audit_after_mismatch[-1]["outcome"] == "rejected"
