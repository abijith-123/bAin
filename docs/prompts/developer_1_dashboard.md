# Codex Master Prompt — Developer 1: Dashboard

You are implementing the bAin Streamlit dashboard in the shared repository for the IEEE TechX 2026 AI Agent Arena, Domain 3: Healthcare Operations & Clinical Workflow. bAin is our project name. The challenge concept is ClearTurn: synthetic hospital discharge orchestration and bed turnover.

## Branch and file ownership

- Work only on `feature/dashboard`.
- Own `frontend/` and `tests/frontend/`.
- Read `shared/api_contract.yaml`, `docs/api_contract.md`, and `shared/sample_responses/` before coding. The OpenAPI contract is the single source of truth for route paths, payloads, status enums, roles, and error codes.
- Do not edit backend-owned code. Do not change the shared API contract without coordinating with Developer 2 and the project lead.

## Start safely

1. Inspect the existing repository, AGENTS.md files, current branch, git status, README, and frontend files before editing.
2. Confirm the current branch is `feature/dashboard`. Do not reset, force-push, overwrite, or discard existing work.
3. If the branch is behind main, preserve any local work and integrate the latest shared contract carefully. Stop and report any conflict instead of guessing.
4. Make a short implementation plan, then implement and verify the dashboard.

## Build requirements

Create a professional hospital operations command center with these views:

1. **Operations dashboard:** active, blocked, completed-today, available-bed, and pending-specialist-review counts; department status; recent agent activity.
2. **Case workflow:** synthetic case identifier, permitted patient details, clinical-clearance status, Pharmacy, Insurance, optional Transport, patient-departure, Housekeeping, bed-ready, blockers, and timeline.
3. **DAG view:** show completed, active, blocked, pending, and locked milestones from the workflow API response. Make it clear that Pharmacy and Insurance progress independently after clearance. Patient departure remains locked until both are resolved; when `transport_required` is true, transport readiness is also required. Housekeeping stays locked until departure is confirmed.
4. **Specialist queue:** show exceptions only to the appropriate specialist. Provide Pharmacy resolution to pharmacists and Insurance resolution to billing specialists. Ask for a reason and display the API result or rejection.
5. **Agent activity:** distinguish tool calls, deterministic workflow decisions, LLM-generated text, fallback text, human actions, failures, and recovery. Never label fallback output as LLM output.
6. **Audit timeline:** show visible audit events in chronological order, respecting the server-filtered response.

## API and mock behavior

- Build a small API adapter in `frontend/services/` for the exact contract endpoints. Read `BAIN_API_BASE_URL` and local demo credentials from environment/configuration; never hardcode tokens.
- Make the dashboard usable before the backend is complete by loading contract-compatible fixtures from `shared/sample_responses/` in an explicitly labeled mock/demo mode.
- Never fabricate live statistics. If the API is unavailable, show a clear connection/error state and label fixture data as synthetic. Do not show success for a mutation unless the API confirms it.
- Treat backend responses as authoritative. UI role checks only hide controls; they are not security enforcement. Do not include clinical notes in transport or housekeeping views.
- For state changes, send `expected_version` and a unique `Idempotency-Key`. On `409 STALE_VERSION` or `409 INVALID_TRANSITION`, refresh the workflow and show the reason. Do not automatically retry a state-changing action.
- Use exact status strings from the contract: `pending`, `ready`, `in_progress`, `blocked`, `completed`; exception statuses `open` and `resolved`.

## UI quality

- Keep the layout readable on a projector and at laptop width. Use consistent status colors with text labels, not color alone.
- Include loading, empty, permission-denied, API-error, success, and conflict states.
- Keep patient-like identifiers visibly marked as synthetic in the demo.
- Do not add decorative controls that do nothing. Every action button must call the mock or API adapter and show its result.

## Tests and completion report

Add focused frontend tests for API response parsing, mock/real adapter behavior, role-filtered rendering, and handling of 409 responses. Run the relevant tests and report the exact commands and results. Do not claim unrun checks passed.

Before finishing, summarize changed files, behavior implemented, tests actually run, any backend endpoints still unavailable, and the commit/PR state. Keep changes within your ownership paths.
