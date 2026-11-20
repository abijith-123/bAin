"""Phase 4 real LLM advisory integration tests for POST /api/agent/analyze."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import main as api
from backend.llm_advisory import (
    DEFAULT_GROQ_MODEL,
    GeminiProvider,
    GroqProvider,
    MinimizedAdvisoryContext,
    OllamaProvider,
    _extract_json_object,
    build_provider_from_env,
)


DEMO_TOKENS = {
    "admin": "llm-test-admin-token",
    "doctor": "llm-test-doctor-token",
    "nurse": "llm-test-nurse-token",
    "pharmacist": "llm-test-pharmacist-token",
    "billing_specialist": "llm-test-billing-token",
    "transport_staff": "llm-test-transport-token",
    "housekeeping": "llm-test-housekeeping-token",
}


class _FakeProvider:
    name = "fake-groq"
    model = DEFAULT_GROQ_MODEL

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


def _auth(role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {DEMO_TOKENS[role]}"}


def _snapshot_workflow_tables() -> dict[str, list[tuple[Any, ...]]]:
    db = api.connect()
    try:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}").fetchall()]
            for table in ("cases", "exceptions", "department_service_state", "audit_events")
        }
    finally:
        db.close()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "bain-llm-test.sqlite3")
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", None)
    monkeypatch.setenv("BAIN_LLM_PROVIDER", "disabled")
    for env_name, role in api.TOKEN_ENV_BY_ROLE.items():
        monkeypatch.setenv(env_name, DEMO_TOKENS[role])
    with TestClient(api.app) as test_client:
        yield test_client


def test_valid_llm_json_response_returns_llm_mode_and_preserves_db(
    client: TestClient,
    monkeypatch,
) -> None:
    provider = _FakeProvider(
        response={
            "summary": (
                "Synthetic case 402 is blocked by a pharmacy stockout and insurance denial ERR_AUTH_409. "
                "Do not override ERR_AUTH_409; route both exceptions to human specialists."
            ),
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": [
                "Escalate MEDICATION_STOCKOUT (ex-ph-402) to the pharmacist for substitution review.",
                "Escalate ERR_AUTH_409 (ex-ins-402) to the billing specialist for payer resubmission.",
            ],
            "escalation_draft": (
                "Pharmacy & Billing Teams: Please review synthetic case 402 (ex-ph-402, ex-ins-402) "
                "and record your human decisions in ClearTurn."
            ),
        }
    )
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", provider)
    before = _snapshot_workflow_tables()

    resp = client.post(
        "/api/agent/analyze",
        headers=_auth("doctor"),
        json={"patient_id": "402", "question": "Summarize blockers and draft escalations."},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_mode"] == "llm"
    assert set(body["blockers"]) == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}
    assert len(body["suggested_actions"]) == 2
    assert body["escalation_draft"] is not None
    assert _snapshot_workflow_tables() == before

    activities = client.get("/api/agent/activity", headers=_auth("doctor")).json()["items"]
    assert any(item["generation_mode"] == "llm" and item["activity_type"] == "summary" for item in activities)


def test_malformed_json_and_invalid_schema_fall_back_deterministically(
    client: TestClient,
    monkeypatch,
) -> None:
    # Direct malformed JSON parsing raises ValueError
    with pytest.raises(ValueError):
        _extract_json_object("not valid json {{{")

    # Provider returning malformed JSON error -> deterministic_fallback + recovery activity
    monkeypatch.setattr(
        api,
        "ADVISORY_PROVIDER",
        _FakeProvider(error=ValueError("Expecting value: line 1 column 1")),
    )
    r1 = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert r1.status_code == 200
    assert r1.json()["generation_mode"] == "deterministic_fallback"
    assert set(r1.json()["blockers"]) == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}

    # Provider returning wrong field types -> deterministic_fallback
    monkeypatch.setattr(
        api,
        "ADVISORY_PROVIDER",
        _FakeProvider(response={"summary": 123, "blockers": "MEDICATION_STOCKOUT", "suggested_actions": []}),
    )
    r2 = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert r2.status_code == 200
    assert r2.json()["generation_mode"] == "deterministic_fallback"

    activities = client.get("/api/agent/activity", headers=_auth("doctor")).json()["items"]
    assert any(item["activity_type"] == "recovery" for item in activities)


def test_hallucinated_blocker_is_stripped_and_anchored_to_db_state(
    client: TestClient,
    monkeypatch,
) -> None:
    provider = _FakeProvider(
        response={
            "summary": "Synthetic case 403 has a pharmacy stockout awaiting pharmacist review.",
            "blockers": ["MEDICATION_STOCKOUT", "HALLUCINATED_LAB_HOLD", "ERR_AUTH_409"],
            "suggested_actions": ["Route ex-ph-403 to the pharmacist for human review."],
            "escalation_draft": "Pharmacy Team: Please review ex-ph-403 for case 403.",
        }
    )
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", provider)

    resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "403"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_mode"] == "llm"
    # Case #403 only has MEDICATION_STOCKOUT in SQLite; hallucinated blockers are stripped.
    assert body["blockers"] == ["MEDICATION_STOCKOUT"]


@pytest.mark.parametrize(
    "unsafe_response",
    [
        {
            "summary": "Auto-approve ERR_AUTH_409 immediately to speed up discharge.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Proceed to departure."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Override the insurance denial and dispatch housekeeping now."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 needs attention.",
            "blockers": ["MEDICATION_STOCKOUT"],
            "suggested_actions": ["Modify the clinical order to remove the stocked-out drug."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Dispatch housekeeping before the patient departs."],
            "escalation_draft": None,
        },
        {
            "summary": "Dispatch housekeeping before the patient departs.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Route blockers to specialists."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Send housekeeping to room 402."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Start room turnover cleaning."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Housekeeping should start cleaning the room."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Mark the bed ready."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Do not delay and dispatch housekeeping."],
            "escalation_draft": None,
        },
        {
            "summary": "Do not delay and override the insurance denial.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Proceed with discharge."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Do not wait, override the insurance denial."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Do not hesitate to override the insurance denial."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Do not auto-approve and override the insurance denial."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Do not delay or override the insurance denial."],
            "escalation_draft": None,
        },
        {
            "summary": "Case 402 is blocked.",
            "blockers": ["ERR_AUTH_409"],
            "suggested_actions": ["Do not wait and auto-approve ERR_AUTH_409."],
            "escalation_draft": None,
        },
    ],
)
def test_unsafe_llm_recommendations_trigger_deterministic_fallback(
    client: TestClient,
    monkeypatch,
    unsafe_response: dict[str, Any],
) -> None:
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", _FakeProvider(response=unsafe_response))
    before = _snapshot_workflow_tables()

    resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_mode"] == "deterministic_fallback"
    assert set(body["blockers"]) == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}
    assert _snapshot_workflow_tables() == before


@pytest.mark.parametrize(
    "safe_response",
    [
        {
            "summary": "Case 402 is blocked. Do not override the insurance denial.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": ["Route ERR_AUTH_409 to billing specialist."],
            "escalation_draft": None,
        },
        {
            "summary": "You cannot auto-approve or override the insurance denial.",
            "blockers": ["MEDICATION_STOCKOUT", "ERR_AUTH_409"],
            "suggested_actions": [
                "Housekeeping can dispatch room turnover cleaning after confirmed departure.",
                "Do not dispatch housekeeping until patient departure is confirmed.",
            ],
            "escalation_draft": "Avoid altering the clinical order; pharmacist review required.",
        },
    ],
)
def test_safe_negated_reminders_and_departure_gated_advice_stay_llm(
    client: TestClient,
    monkeypatch,
    safe_response: dict[str, Any],
) -> None:
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", _FakeProvider(response=safe_response))
    resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert resp.status_code == 200
    assert resp.json()["generation_mode"] == "llm"


def test_housekeeping_recommendation_respects_confirmed_departure_dag_state(
    client: TestClient,
    monkeypatch,
) -> None:
    # Advance case #406 through departure so housekeeping_dispatched is genuinely unlocked.
    version = 1
    steps = [
        ("pharmacy", "completed", "pharmacist"),
        ("insurance", "completed", "billing_specialist"),
        ("transport", "completed", "transport_staff"),
        ("patient_departed", "in_progress", "transport_staff"),
        ("patient_departed", "completed", "transport_staff"),
    ]
    for idx, (ms_id, target, role) in enumerate(steps):
        res = client.post(
            "/api/patients/406/milestones",
            headers={**_auth(role), "Idempotency-Key": f"advance-406-hk-{idx}"},
            json={
                "milestone_id": ms_id,
                "target_status": target,
                "expected_version": version,
                "reason": "Synthetic test progression to confirmed departure.",
            },
        )
        assert res.status_code == 200
        version = res.json()["version"]

    # Now affirmative housekeeping dispatch is valid and remains generation_mode='llm'
    monkeypatch.setattr(
        api,
        "ADVISORY_PROVIDER",
        _FakeProvider(
            response={
                "summary": "Patient departure is confirmed; housekeeping turnover can now proceed.",
                "blockers": [],
                "suggested_actions": ["Dispatch housekeeping to clean the room."],
                "escalation_draft": None,
            }
        ),
    )
    ok_resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "406"})
    assert ok_resp.status_code == 200
    assert ok_resp.json()["generation_mode"] == "llm"

    # Contradicting DAG timing ("before the patient departs") still falls back
    monkeypatch.setattr(
        api,
        "ADVISORY_PROVIDER",
        _FakeProvider(
            response={
                "summary": "Dispatch housekeeping before the patient departs.",
                "blockers": [],
                "suggested_actions": ["Dispatch housekeeping before the patient departs."],
                "escalation_draft": None,
            }
        ),
    )
    bad_resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "406"})
    assert bad_resp.status_code == 200
    assert bad_resp.json()["generation_mode"] == "deterministic_fallback"


def test_provider_timeout_and_network_failure_fall_back_gracefully(
    client: TestClient,
    monkeypatch,
) -> None:
    for err in (
        httpx.ReadTimeout("Simulated upstream timeout"),
        httpx.ConnectError("Simulated network failure"),
    ):
        monkeypatch.setattr(api, "ADVISORY_PROVIDER", _FakeProvider(error=err))
        resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["generation_mode"] == "deterministic_fallback"
        assert set(body["blockers"]) == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}


def test_role_based_prompt_minimization_before_llm_invocation(
    client: TestClient,
    monkeypatch,
) -> None:
    provider = _FakeProvider(
        response={
            "summary": "Role-scoped discharge advisory summary.",
            "blockers": ["MEDICATION_STOCKOUT"],
            "suggested_actions": ["Complete authorized specialist review."],
            "escalation_draft": None,
        }
    )
    monkeypatch.setattr(api, "ADVISORY_PROVIDER", provider)

    # Operational roles rejected BEFORE calling provider
    for denied_role in ("housekeeping", "transport_staff"):
        denied = client.post("/api/agent/analyze", headers=_auth(denied_role), json={"patient_id": "402"})
        assert denied.status_code == 403
        assert denied.json()["code"] == "FORBIDDEN"
    assert len(provider.captured_contexts) == 0

    # Pharmacist: no clinical_summary, no display_name, only pharmacy exceptions
    r_ph = client.post("/api/agent/analyze", headers=_auth("pharmacist"), json={"patient_id": "402"})
    assert r_ph.status_code == 200
    ph_ctx = provider.captured_contexts[-1]
    assert ph_ctx.clinical_summary is None
    assert not hasattr(ph_ctx, "display_name")
    assert ph_ctx.blockers == ["MEDICATION_STOCKOUT"]
    assert all(exc["department"] == "pharmacy" for exc in ph_ctx.open_exceptions)

    # Billing specialist: no clinical_summary, only insurance exceptions
    r_bill = client.post("/api/agent/analyze", headers=_auth("billing_specialist"), json={"patient_id": "402"})
    assert r_bill.status_code == 200
    bill_ctx = provider.captured_contexts[-1]
    assert bill_ctx.clinical_summary is None
    assert bill_ctx.blockers == ["ERR_AUTH_409"]
    assert all(exc["department"] == "insurance" for exc in bill_ctx.open_exceptions)

    # Admin: no clinical_summary, operational overview of blockers
    r_admin = client.post("/api/agent/analyze", headers=_auth("admin"), json={"patient_id": "402"})
    assert r_admin.status_code == 200
    admin_ctx = provider.captured_contexts[-1]
    assert admin_ctx.clinical_summary is None
    assert set(admin_ctx.blockers) == {"MEDICATION_STOCKOUT", "ERR_AUTH_409"}

    # Doctor: includes synthetic clinical_summary, still no display_name
    r_doc = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert r_doc.status_code == 200
    doc_ctx = provider.captured_contexts[-1]
    assert doc_ctx.clinical_summary is not None
    assert not hasattr(doc_ctx, "display_name")


def test_missing_api_credentials_and_env_configuration(
    client: TestClient,
    monkeypatch,
) -> None:
    # Verify Groq defaults to openai/gpt-oss-20b and respects configurable timeout
    monkeypatch.setenv("BAIN_LLM_PROVIDER", "groq")
    monkeypatch.setenv("BAIN_LLM_TIMEOUT_SECONDS", "6.5")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("BAIN_GROQ_MODEL", raising=False)

    provider = build_provider_from_env()
    assert isinstance(provider, GroqProvider)
    assert provider.model == "openai/gpt-oss-20b"
    assert provider.timeout_seconds == 6.5

    # Missing GROQ_API_KEY falls back deterministically without leaking error details
    resp = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
    assert resp.status_code == 200
    assert resp.json()["generation_mode"] == "deterministic_fallback"

    # Gemini and Ollama also fall back cleanly when credentials/model are missing
    for env_provider, expected_cls in (("gemini", GeminiProvider), ("ollama", OllamaProvider)):
        monkeypatch.setenv("BAIN_LLM_PROVIDER", env_provider)
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        monkeypatch.delenv("BAIN_OLLAMA_MODEL", raising=False)
        p = build_provider_from_env()
        assert isinstance(p, expected_cls)
        r = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": "402"})
        assert r.status_code == 200
        assert r.json()["generation_mode"] == "deterministic_fallback"


def test_secret_scrubbing_and_health_and_analyze_smoke(client: TestClient) -> None:
    assert client.get("/api/health").json() == {"status": "ok", "service": "bAin-api"}

    api.record_activity(
        patient_id="402",
        activity_type="error",
        message="Provider auth failed with Bearer gsk_supersecret123456 and sk-secret987654",
        generation_mode=None,
    )
    items = client.get("/api/agent/activity", headers=_auth("doctor")).json()["items"]
    for item in items:
        assert "gsk_supersecret123456" not in item["message"]
        assert "sk-secret987654" not in item["message"]

    # Smoke test POST /api/agent/analyze across all 6 synthetic cases
    for pid in ("402", "403", "404", "405", "406", "407"):
        r = client.post("/api/agent/analyze", headers=_auth("doctor"), json={"patient_id": pid})
        assert r.status_code == 200
        data = r.json()
        assert set(data.keys()) == {
            "summary", "blockers", "suggested_actions", "escalation_draft", "generation_mode"
        }
        assert data["generation_mode"] == "deterministic_fallback"


def test_repeat_department_check_preserves_denial_upheld_and_advisory_reflects_it(
    client: TestClient,
) -> None:
    # Billing specialist upholds insurance denial on synthetic case #404
    resolve_resp = client.post(
        "/api/exceptions/ex-ins-404/resolve",
        headers={**_auth("billing_specialist"), "Idempotency-Key": "uphold-denial-404-key"},
        json={
            "resolution": "Payer denial confirmed and upheld; formal appeal required.",
            "outcome": "denial_upheld",
            "expected_version": 1,
        },
    )
    assert resolve_resp.status_code == 200
    resolved_body = resolve_resp.json()
    assert resolved_body["status"] == "resolved"
    assert resolved_body["resolution_outcome"] == "denial_upheld"
    assert resolved_body["milestone_status"] == "blocked"
    version_after_resolve = resolved_body["version"]

    # Repeat parallel department check must NOT silently reopen the exception or clear resolution_outcome
    check_result = api.check_pharmacy_and_insurance("404")
    assert check_result == {"pharmacy": "completed", "insurance": "blocked"}

    # Verify exception remains resolved with resolution_outcome='denial_upheld' and version unchanged
    open_items = [
        item for item in client.get("/api/exceptions?status=open", headers=_auth("billing_specialist")).json()["items"]
        if item["patient_id"] == "404"
    ]
    assert open_items == []
    resolved_items = [
        item for item in client.get("/api/exceptions?status=resolved", headers=_auth("billing_specialist")).json()["items"]
        if item["exception_id"] == "ex-ins-404"
    ]
    assert len(resolved_items) == 1
    assert resolved_items[0]["resolution_outcome"] == "denial_upheld"

    wf = client.get("/api/patients/404/workflow", headers=_auth("billing_specialist")).json()
    assert wf["version"] == version_after_resolve
    by_id = {m["milestone_id"]: m for m in wf["milestones"]}
    assert by_id["insurance"]["status"] == "blocked"
    assert by_id["patient_departed"]["locked"] is True

    # Advisory endpoint reflects upheld denial for billing_specialist/doctor, not for pharmacist
    bill_adv = client.post("/api/agent/analyze", headers=_auth("billing_specialist"), json={"patient_id": "404"})
    assert bill_adv.status_code == 200
    assert any("denial upheld" in b.lower() for b in bill_adv.json()["blockers"])

    ph_adv = client.post("/api/agent/analyze", headers=_auth("pharmacist"), json={"patient_id": "404"})
    assert ph_adv.status_code == 200
    assert not any("denial upheld" in b.lower() for b in ph_adv.json()["blockers"])

