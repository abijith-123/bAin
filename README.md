# bAin

**bAin** is a two-person university project for the IEEE TechX 2026 AI Agent Arena, Domain 3: Healthcare Operations & Clinical Workflow. The intended system coordinates synthetic hospital discharge workflows with deterministic safety rules, human specialist oversight, an AI assistant, and an operations dashboard.

## Project status

This repository is an initial scaffold. The official competition brief has not yet been added, so `shared/api_contract.yaml` is a draft based on the project requirements supplied by the team. Confirm it against the official brief before treating it as final. No production workflow, authorization, persistence, or AI integration is implemented yet.

## Team ownership

- **Developer 1 — dashboard:** `frontend/`, `tests/frontend/`
- **Developer 2 — backend and agent:** `backend/`, `tests/backend/`
- **Shared changes:** `shared/`, `docs/`, `tests/integration/`; coordinate before editing.

## Repository layout

```text
backend/                 FastAPI service and workflow engine (backend owner)
frontend/                Streamlit operations dashboard (dashboard owner)
shared/                  Draft API contract and contract-compatible fixtures
docs/                    Architecture and collaboration notes
tests/                   Frontend, backend, and integration tests
scripts/                 Local development and synthetic-data utilities
```

## Local setup

Use Python 3.11 or newer.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
```

Run the API and dashboard in separate terminals:

```bash
uvicorn backend.main:app --reload
```

```bash
streamlit run frontend/app.py
```

The current scaffold exposes only `GET /api/health`; the dashboard labels this as a scaffold connection check. Configure `BAIN_API_BASE_URL` to point the dashboard at another API host. Do not put API keys or real patient information in this repository.

## Git workflow

- `main` is the stable integration branch.
- Dashboard work goes to `feature/dashboard`.
- Backend and agent work goes to `feature/backend-agent`.
- Open pull requests into `main`; do not force-push or overwrite another developer's work.
- Before implementation begins, review and finalize the shared contract together.

See [docs/team_workflow.md](docs/team_workflow.md) and [docs/architecture.md](docs/architecture.md).
