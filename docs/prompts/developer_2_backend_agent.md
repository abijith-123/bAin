# Codex Master Prompt — Developer 2: Backend and AI Agent

You are implementing the bAin backend and orchestration engine in the shared repository for the IEEE TechX 2026 AI Agent Arena, Domain 3: Healthcare Operations & Clinical Workflow. The challenge concept is ClearTurn: synthetic hospital discharge orchestration and bed turnover.

## Branch and file ownership

- Work only on `feature/backend-agent`.
- Own `backend/`, `tests/backend/`, backend scripts, and backend dependency changes.
- Read `shared/api_contract.yaml`, `docs/api_contract.md`, `docs/architecture.md`, `docs/team_workflow.md`, and `shared/sample_responses/` before coding.
- The OpenAPI contract is the single source of truth. Coordinate with Developer 1 before changing shared contract files. Do not edit dashboard-owned files.

## Start safely

1. Inspect the current branch, git status, existing files, and any AGENTS.md instructions.
2. Confirm you are on `feature/backend-agent`; preserve all existing work. Do not reset, force-push, overwrite, or discard changes.
3. Implement in small steps, keeping the frontend contract stable. Report any contract gap before changing shared schemas.

## Core backend

Implement the FastAPI service, SQLite persistence, synthetic data, and deterministic workflow engine. The three required tools must perform real reads/writes against the simulation:

- `get_discharge_dag_state(patient_id)`: return milestone state, dependencies, locked actions, version, and role-filtered blockers.
- `check_pharmacy_and_insurance(patient_id)`: query the two mock department services independently and safely in parallel. Record each result separately; a timeout or block in one must not erase the other result. Open a targeted Pharmacy or Insurance exception when needed.
- `update_department_milestone(...)`: the only path for state changes. Validate actor/role, DAG dependencies, expected version, and idempotency key; atomically update milestone, case version, exception state if applicable, and audit event.

Enforce the dependency graph in Python, never in the LLM:

Clinical clearance -> Pharmacy ready AND Insurance cleared -> Patient departed -> Housekeeping dispatched -> Bed ready.

Pharmacy and Insurance must progress independently after clearance. Transport preparation can run in parallel; if a synthetic case has `transport_required: true`, transport readiness is an extra departure prerequisite. Housekeeping must never be dispatched before departure. Reject unsafe transitions with contract-compatible 409 errors.

## Concurrency, RBAC, privacy, and audit

- Use SQLite transactions for state changes, monotonic case versions for optimistic locking, and persistent idempotency records for repeated requests. A stale update returns `409 STALE_VERSION`; do not silently overwrite another department's update.
- Authenticate from server-validated bearer tokens mapped to synthetic actors and roles. Do not trust `role` or `actor_id` supplied in request JSON or headers as proof of identity. Keep demo tokens and provider keys out of Git.
- Enforce server-side role permissions on every endpoint and action. Doctor records clinical clearance; pharmacist resolves Pharmacy exceptions; billing specialist records Insurance review outcomes; transport staff owns transport readiness/departure confirmation; housekeeping sees only room/bed readiness and may act only after departure. Admin cannot sign clinical clearance or override specialist decisions.
- Filter fields before returning responses. Transport and Housekeeping must never receive diagnoses or clinical handoff notes. Pharmacy and Billing receive only the minimum relevant fields.
- Audit successful and rejected state-change attempts. Include actor, role, case, action, outcome/rejection, versions, and timestamps, but do not log raw clinical notes, full prompts, or unnecessary PHI.

## API implementation

Implement every endpoint and schema in `shared/api_contract.yaml`: health, dashboard summary, patient list/detail/workflow, milestone update, audit, department status, exceptions and resolution, agent activity, and agent analysis. Return the contract's exact field names and error envelope. The frontend uses these same schemas and fixtures.

## AI agent

- Support a configurable provider suitable for the competition baseline: local Ollama Qwen3 or a configured Groq/Gemini-compatible API. Keep provider calls behind a small service boundary.
- LLM tasks are limited to summarizing authorized synthetic handoff notes, explaining backend-reported blockers, and drafting targeted escalation messages.
- The LLM has no direct database access and cannot mutate state, change clinical orders, approve a denied claim, or resolve a specialist exception. All tool calls and writes pass through deterministic backend functions.
- Provide a deterministic fallback for provider outages. Return `generation_mode: deterministic_fallback` when used, log the provider failure without sensitive payloads, and never pretend the LLM ran.

## Synthetic scenarios

Seed reproducible synthetic cases that cover: Patient 402 with Pharmacy stockout and ERR_AUTH_409; Pharmacy-only block; Insurance-only rejection; Transport delay; successful discharge; parallel department updates; premature Housekeeping request; stale-version conflict; LLM timeout; and human specialist resolution. Never commit real patient information or operational database files.

## Tests and completion report

Use pytest for backend and integration tests covering DAG guards, permitted transitions, parallel department updates, stale versions, idempotency, RBAC, PHI filtering, specialist resolution, audit events, service failures, LLM fallback, and API contract compatibility. Run relevant tests and report exact commands/results; do not claim unrun checks passed.

Before finishing, summarize changed files, behaviors implemented, tests actually run, remaining gaps, and commit/PR state. Do not edit frontend-owned files.
