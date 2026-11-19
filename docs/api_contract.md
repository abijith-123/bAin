# bAin shared API contract

**Version:** 1.0.0 · **Status:** reviewed against the official IEEE TechX 2026 Domain 3 University Track brief · **Data:** synthetic only.

The OpenAPI file at `shared/api_contract.yaml` is the shared wire-format source of truth. Both developers should use its exact field names and enums.

## Workflow and safety rules

- Clinical clearance is recorded by a physician; the API never creates or edits a clinical order.
- Once cleared, Pharmacy and Insurance can progress independently. A medication stockout or ERR_AUTH_409 opens a human review exception and does not stop unrelated preparation.
- Patient departure is blocked until Pharmacy is ready and Insurance is cleared. If a case requires hospital transport, transport readiness is an additional departure prerequisite.
- Housekeeping cannot be dispatched until the backend records that the patient has departed. Bed-ready follows housekeeping completion.
- These rules are deterministic backend checks. The LLM summarizes, explains, and drafts alerts; it cannot change state or decide clinical or insurance outcomes.

## Authentication and role permissions

Use a server-validated bearer token mapped to a synthetic actor and role. Never trust a caller-supplied role field. Demo tokens stay in local environment configuration and are not committed.

| Role | State-changing permissions | Data scope |
|---|---|---|
| admin | Operational coordination and audit; cannot sign clinical clearance or resolve pharmacist/billing decisions | Operational overview |
| doctor | Record clinical clearance | Clinical handoff and clearance details |
| nurse | Nurse-owned checklist or instruction sign-off when enabled | Clinical details needed for nursing work |
| pharmacist | Update Pharmacy milestones and resolve Pharmacy exceptions | Medication details needed for review |
| billing_specialist | Update Insurance review and resolve Insurance exceptions | Authorization and billing details needed for review |
| transport_staff | Update transport readiness and confirm departure | Pickup and transport details; no diagnosis or clinical notes |
| housekeeping | Dispatch cleaning and mark a bed ready, after departure only | Room/bed and readiness status only |

No role or AI action may autonomously modify a clinical order or override an insurer denial. A billing specialist records a human review outcome; the agent cannot treat a denial as approval.

## Endpoints

| Method and path | Caller | Purpose and dashboard use |
|---|---|---|
| GET /api/health | Public | API connection indicator; no patient data |
| GET /api/dashboard/summary | Any authenticated role | KPI cards with aggregate counts |
| GET /api/patients | Any authenticated role | Role-filtered active case list and filters |
| GET /api/patients/{patient_id} | Any authenticated role | Role-filtered patient detail panel |
| GET /api/patients/{patient_id}/workflow | Any authenticated role | Milestone DAG, dependencies, blockers, and case version |
| POST /api/patients/{patient_id}/milestones | Role allowed for that milestone | Specialist action; server validates role, DAG, version, and idempotency |
| GET /api/patients/{patient_id}/audit | Any authenticated role | Permission-filtered audit timeline |
| GET /api/departments/status | Any authenticated role | Role-filtered department workload and blockers |
| GET /api/exceptions | Assigned specialists; admin gets operational summaries | Specialist review queue |
| POST /api/exceptions/{exception_id}/resolve | Pharmacist for Pharmacy; billing specialist for Insurance | Human resolution form and result |
| GET /api/agent/activity | Any authenticated role | Role-filtered agent activity feed |
| POST /api/agent/analyze | Clinical or relevant specialist roles | Explain blockers and draft a targeted escalation; read-only |

Exact request/response schemas and endpoint errors are defined in `shared/api_contract.yaml`. State-changing requests require an Idempotency-Key header and expected_version in the JSON body.

## Concurrency, validation, and errors

Each case has a monotonically increasing version. A state change, version increment, exception update, and audit event commit in one SQLite transaction. If two parallel requests use the same old version, one may receive 409 STALE_VERSION; the client refetches workflow state and asks the authorized user to retry. Never blindly replay a stale request.

Return 409 INVALID_TRANSITION when a DAG edge is not satisfied and 409 IDEMPOTENCY_CONFLICT when a key is reused with a different request. Other common errors: 401 UNAUTHENTICATED, 403 FORBIDDEN, 404 NOT_FOUND, 422 VALIDATION_ERROR, and 500 INTERNAL_ERROR. Error bodies must not reveal protected fields.

## Audit and AI boundaries

Audit successful and rejected state-change attempts: timestamp, actor ID and role, case ID, action, prior/new status when permitted, outcome, rejection code, and version. Do not include raw handoff notes, full prompts, or unnecessary PHI.

The analyze endpoint is read-only. It returns generation_mode as llm or deterministic_fallback, current blockers, permitted next actions, and an optional escalation draft. Label deterministic fallback honestly. Every proposed action must be checked by backend permissions and DAG rules; Pharmacy and Insurance exceptions still require human resolution.

## Frontend mock data

JSON fixtures under `shared/sample_responses/` use the contract's field names and enums. The frontend can build against these fixtures, then switch only its API adapter when the backend is ready.

## Backend tool contract

- `get_discharge_dag_state(patient_id)` returns current milestone statuses, dependencies, locked actions, case version, and role-filtered blockers. It is read-only.
- `check_pharmacy_and_insurance(patient_id)` queries the two synthetic department services independently, records their separate outcomes, and opens targeted pharmacist or billing exceptions when blocked. One service failure must not erase the other result.
- `update_department_milestone(patient_id, milestone_id, target_status, actor, expected_version, idempotency_key)` is the only state-changing path. It validates the actor, DAG prerequisites, version, and idempotency key, then commits the milestone, audit record, and version atomically.

The LLM may request these tools only through the backend orchestrator. It receives no direct database access; the two read tools are safe to parallelize, while writes are serialized by transaction and optimistic version checks.
