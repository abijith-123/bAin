"""Regression coverage for the transport exception workflow."""

import pytest
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend import main as api


@pytest.fixture
def database(tmp_path, monkeypatch):
    """Give each test an isolated synthetic database."""
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "bain-test.sqlite3")
    api.init_db()


def actor(role: str) -> tuple[str, str]:
    return f"test-{role}", role


def test_dashboard_fixtures_match_seeded_backend_contract(database):
    fixture_dir = Path(__file__).resolve().parents[2] / "shared" / "sample_responses"

    summary = api.dashboard_summary(actor=actor("doctor"))
    expected_summary = json.loads((fixture_dir / "dashboard_summary.json").read_text())
    assert {key: value for key, value in summary.items() if key != "generated_at"} == {
        key: value for key, value in expected_summary.items() if key != "generated_at"
    }

    assert api.departments_status(actor=actor("admin")) == json.loads(
        (fixture_dir / "departments_status.json").read_text()
    )
    assert api.patient_detail("402", actor=actor("doctor")) == json.loads(
        (fixture_dir / "patient_402_detail.json").read_text()
    )
    assert api.patient_workflow("402", actor=actor("doctor")) == json.loads(
        (fixture_dir / "patient_402_workflow.json").read_text()
    )


def test_demo_tokens_are_required_distinct_and_loaded_from_environment(monkeypatch):
    original_tokens = api.TOKENS.copy()
    monkeypatch.setattr(api, "TOKENS", original_tokens)
    for name in api.TOKEN_ENV_BY_ROLE:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="Missing local demo bearer tokens"):
        api.validate_demo_tokens()

    for index, name in enumerate(api.TOKEN_ENV_BY_ROLE):
        monkeypatch.setenv(name, f"test-local-token-{index}")

    api.validate_demo_tokens()
    assert api.TOKENS["test-local-token-0"][1] == "admin"
    assert len(api.TOKENS) == len(api.TOKEN_ENV_BY_ROLE)

    duplicate_name = next(iter(api.TOKEN_ENV_BY_ROLE))
    monkeypatch.setenv(duplicate_name, "test-local-token-1")
    with pytest.raises(RuntimeError, match="distinct value"):
        api.validate_demo_tokens()


def test_transport_delay_is_visible_only_to_transport_specialist(database):
    items = api.list_exceptions(status=None, actor=actor("transport_staff"))["items"]
    transport_issue = next(item for item in items if item["patient_id"] == "405")
    assert transport_issue["exception_id"] == "ex-tr-405"
    assert transport_issue["code"] == "TRANSPORT_DELAY"
    assert transport_issue["department"] == "transport"

    housekeeping = api.list_exceptions(status=None, actor=actor("housekeeping"))["items"]
    assert all(item["department"] == "housekeeping" for item in housekeeping)
    assert all(item["patient_id"] != "405" for item in housekeeping)


def test_transport_backfill_uses_existing_case_version(database):
    db = api.connect()
    try:
        db.execute("UPDATE cases SET version = 3 WHERE patient_id = '405'")
        db.execute("DELETE FROM exceptions WHERE exception_id = 'ex-tr-405'")
    finally:
        db.close()

    api.init_db()
    transport_issue = next(
        item for item in api.list_exceptions(status=None, actor=actor("transport_staff"))["items"]
        if item["patient_id"] == "405"
    )
    assert transport_issue["version"] == 3


def test_legacy_audit_table_gets_state_snapshot_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "DB_PATH", tmp_path / "legacy.sqlite3")
    db = api.connect()
    try:
        db.execute("""CREATE TABLE audit_events (
            event_id TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, actor_id TEXT NOT NULL,
            actor_role TEXT NOT NULL, patient_id TEXT NOT NULL, action TEXT NOT NULL,
            outcome TEXT NOT NULL, reason TEXT, version INTEGER NOT NULL)""")
    finally:
        db.close()

    api.init_db()
    db = api.connect()
    try:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(audit_events)")}
    finally:
        db.close()
    assert {"previous_state", "new_state"} <= columns


def test_department_check_runs_both_results_and_opens_missing_reviews(database):
    db = api.connect()
    try:
        db.execute("DELETE FROM exceptions WHERE patient_id='402'")
    finally:
        db.close()

    result = api.check_pharmacy_and_insurance("402")
    assert result == {"pharmacy": "blocked", "insurance": "blocked"}

    items = api.list_exceptions(status="open", actor=actor("admin"))["items"]
    case_402 = [item for item in items if item["patient_id"] == "402"]
    assert {item["department"] for item in case_402} == {"pharmacy", "insurance"}
    assert all(item["version"] == 2 for item in case_402)
    audit = api.patient_audit("402", actor=actor("admin"))["items"]
    opened = [item for item in audit if item["action"].endswith(":open")]
    assert len(opened) == 2
    assert all(item["actor_id"] == "bain-system" for item in opened)
    assert all(item["previous_state"] is None and item["new_state"] == "open" for item in opened)

    # Repeating the tool check is safe and does not duplicate the alerts or bump the case.
    assert api.check_pharmacy_and_insurance("402") == result
    workflow = api.patient_workflow("402", actor=actor("admin"))
    assert workflow["version"] == 2


def test_one_mock_service_failure_does_not_erase_the_other_result(database, monkeypatch):
    original_read = api._read_synthetic_department

    def partial_service(patient_id: str, department: str):
        if department == "pharmacy":
            raise TimeoutError("simulated pharmacy service timeout")
        db = api.connect()
        try:
            db.execute(
                "UPDATE department_service_state SET status='blocked' WHERE patient_id=? AND department='insurance'",
                (patient_id,),
            )
        finally:
            db.close()
        return original_read(patient_id, department)

    monkeypatch.setattr(api, "_read_synthetic_department", partial_service)
    result = api.check_pharmacy_and_insurance("406")

    assert result == {"pharmacy": "unavailable", "insurance": "blocked"}
    billing_items = api.list_exceptions(status="open", actor=actor("billing_specialist"))["items"]
    assert any(item["exception_id"] == "ex-ins-406" for item in billing_items)
    pharmacist_items = api.list_exceptions(status="open", actor=actor("pharmacist"))["items"]
    assert all(item["patient_id"] != "406" for item in pharmacist_items)
    activity = api.agent_activity(actor=actor("admin"))["items"]
    assert any(item["activity_type"] == "error" and "Pharmacy synthetic service unavailable" in item["message"]
               for item in activity)


def test_pharmacy_and_billing_specialists_resolve_independently(database):
    api.check_pharmacy_and_insurance("402")
    pharmacy = api.resolve_exception(
        "ex-ph-402",
        api.ResolveRequest(resolution="Pharmacist confirmed medication supply.", expected_version=1),
        actor("pharmacist"),
        "resolve-pharmacy-402",
    )
    insurance = api.resolve_exception(
        "ex-ins-402",
        api.ResolveRequest(resolution="Billing specialist reviewed the authorization.", expected_version=2),
        actor("billing_specialist"),
        "resolve-insurance-402",
    )
    assert pharmacy["status"] == insurance["status"] == "resolved"
    workflow = api.patient_workflow("402", actor=actor("doctor"))
    statuses = {item["milestone_id"]: item["status"] for item in workflow["milestones"]}
    assert statuses["pharmacy"] == statuses["insurance"] == "ready"
    assert statuses["patient_departed"] == "pending"

    pharmacist_audit = api.patient_audit("402", actor=actor("pharmacist"))["items"]
    billing_audit = api.patient_audit("402", actor=actor("billing_specialist"))["items"]
    pharmacist_resolution = next(item for item in pharmacist_audit if item["action"] == "exception:ex-ph-402:resolve")
    billing_resolution = next(item for item in billing_audit if item["action"] == "exception:ex-ins-402:resolve")
    assert pharmacist_resolution["reason"] == "Pharmacist confirmed medication supply."
    assert billing_resolution["reason"] == "Billing specialist reviewed the authorization."
    assert next(item for item in pharmacist_audit if item["action"] == "exception:ex-ins-402:resolve")["reason"] is None
    assert next(item for item in billing_audit if item["action"] == "exception:ex-ph-402:resolve")["reason"] is None


def test_transport_specialist_resolves_exception_idempotently_and_audits(database):
    request = api.ResolveRequest(
        resolution="Transport team confirmed pickup availability.",
        expected_version=1,
    )
    first = api.resolve_exception("ex-tr-405", request, actor("transport_staff"), "transport-405-confirm")
    replay = api.resolve_exception("ex-tr-405", request, actor("transport_staff"), "transport-405-confirm")

    assert first == replay
    assert first["status"] == "resolved"
    assert first["version"] == 2

    workflow = api.patient_workflow("405", actor=actor("transport_staff"))
    transport = next(item for item in workflow["milestones"] if item["milestone_id"] == "transport")
    departed = next(item for item in workflow["milestones"] if item["milestone_id"] == "patient_departed")
    assert transport["status"] == "ready"
    assert departed["locked"] is True
    assert "transport" in departed["dependencies"]

    audit = api.patient_audit("405", actor=actor("transport_staff"))["items"]
    events = [event for event in audit if event["action"] == "exception:ex-tr-405:resolve"]
    assert len(events) == 1
    assert events[0]["outcome"] == "success"
    assert events[0]["version"] == 2
    assert events[0]["previous_state"] == "blocked"
    assert events[0]["new_state"] == "ready"


def test_other_role_cannot_resolve_transport_exception_and_attempt_is_audited(database):
    with pytest.raises(api.APIError) as error:
        api.resolve_exception(
            "ex-tr-405",
            api.ResolveRequest(resolution="Attempt unauthorized resolution", expected_version=1),
            actor("housekeeping"),
            "housekeeping-transport",
        )

    assert error.value.status == 403
    assert error.value.code == "FORBIDDEN"
    audit = api.patient_audit("405", actor=actor("housekeeping"))["items"]
    rejected = next(
        event for event in audit
        if event["action"] == "exception:ex-tr-405:resolve"
    )
    assert (
        rejected["outcome"] == "rejected"
        and rejected["previous_state"] == "open"
        and rejected["new_state"] == "open"
    )


def test_housekeeping_cannot_dispatch_before_patient_departure(database):
    with pytest.raises(api.APIError) as error:
        api.update_milestone(
            "405",
            api.MilestoneRequest(
                milestone_id="housekeeping_dispatched",
                target_status="in_progress",
                expected_version=1,
                reason="Attempt early room cleaning",
            ),
            actor("housekeeping"),
            "early-housekeeping-405",
        )

    assert error.value.status == 409
    assert error.value.code == "INVALID_TRANSITION"
    workflow = api.patient_workflow("405", actor=actor("housekeeping"))
    housekeeping = next(
        item for item in workflow["milestones"]
        if item["milestone_id"] == "housekeeping_dispatched"
    )
    assert housekeeping["status"] == "pending"
    assert housekeeping["locked"] is True
    audit = api.patient_audit("405", actor=actor("housekeeping"))["items"]
    rejected = next(event for event in audit if event["action"] == "milestone:housekeeping_dispatched")
    assert rejected["previous_state"] == rejected["new_state"] == "pending"


def test_housekeeping_patient_detail_redacts_clinical_information(database):
    detail = api.patient_detail("405", actor=actor("housekeeping"))
    assert detail["display_name"] is None
    assert detail["clinical_summary"] is None
    assert detail["clinical_clearance_at"] is None


def test_parallel_department_updates_do_not_lose_a_write(database):
    def complete(milestone_id: str, role: str, key: str):
        try:
            return api.update_department_milestone(
                "406", milestone_id, "completed", actor(role), 1, key,
                "Synthetic department completion confirmation.",
            )
        except api.APIError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        pharmacy = pool.submit(complete, "pharmacy", "pharmacist", "parallel-pharmacy-406")
        insurance = pool.submit(complete, "insurance", "billing_specialist", "parallel-insurance-406")
        outcomes = [pharmacy.result(), insurance.result()]

    successes = [item for item in outcomes if isinstance(item, dict)]
    failures = [item for item in outcomes if isinstance(item, api.APIError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].code == "STALE_VERSION"

    workflow = api.patient_workflow("406", actor=actor("admin"))
    assert workflow["version"] == 2
    statuses = {item["milestone_id"]: item["status"] for item in workflow["milestones"]}
    assert sorted([statuses["pharmacy"], statuses["insurance"]]) == ["completed", "ready"]
    audit = api.patient_audit("406", actor=actor("admin"))["items"]
    stale = next(event for event in audit if event["outcome"] == "rejected")
    assert stale["previous_state"] == stale["new_state"] == "ready"


def test_successful_milestone_audit_records_before_and_after(database):
    api.update_department_milestone(
        "406", "pharmacy", "completed", actor("pharmacist"), 1,
        "audit-pharmacy-406", "Medication availability confirmed.",
    )
    events = api.patient_audit("406", actor=actor("admin"))["items"]
    event = next(item for item in events if item["action"] == "milestone:pharmacy")
    assert event["previous_state"] == "ready"
    assert event["new_state"] == "completed"


def test_normal_discharge_completes_every_milestone_in_dag_order(database):
    version = 1
    sequence = [
        ("pharmacy", "completed", "pharmacist"),
        ("insurance", "completed", "billing_specialist"),
        ("transport", "completed", "transport_staff"),
        ("patient_departed", "in_progress", "transport_staff"),
        ("patient_departed", "completed", "transport_staff"),
        ("housekeeping_dispatched", "in_progress", "housekeeping"),
        ("housekeeping_dispatched", "completed", "housekeeping"),
        ("bed_ready", "in_progress", "housekeeping"),
        ("bed_ready", "completed", "housekeeping"),
    ]
    for index, (milestone_id, target_status, role) in enumerate(sequence):
        result = api.update_department_milestone(
            "406", milestone_id, target_status, actor(role), version,
            f"normal-406-step-{index}", "Synthetic normal discharge confirmation.",
        )
        version = result["version"]

    workflow = api.patient_workflow("406", actor=actor("admin"))
    statuses = {item["milestone_id"]: item["status"] for item in workflow["milestones"]}
    assert workflow["version"] == 10
    assert all(status == "completed" for status in statuses.values())
