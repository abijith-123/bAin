# bAin

**bAin** is a two-person university project for the IEEE TechX 2026 AI Agent Arena, Domain 3: Healthcare Operations & Clinical Workflow. The intended system coordinates synthetic hospital discharge workflows with deterministic safety rules, human specialist oversight, an AI assistant, and an operations dashboard.

## Project status

The React dashboard and a local FastAPI simulation are available for development. The API seeds synthetic cases into SQLite, validates demo-role permissions, enforces discharge dependencies, and records versions, idempotency keys, and audit events. AI analysis currently uses a clearly labeled deterministic fallback; no external clinical, pharmacy, payer, or transport systems are connected.

## Team ownership

- **Developer 1 — dashboard:** `frontend/web/`, `tests/frontend/`
- **Developer 2 — backend and agent:** `backend/`, `tests/backend/`
- **Shared changes:** `shared/`, `docs/`, `tests/integration/`; coordinate before editing.

## Repository layout

```text
backend/                 FastAPI service and workflow engine (backend owner)
frontend/web/            React + Vite operations dashboard (dashboard owner)
shared/                  Draft API contract and contract-compatible fixtures
docs/                    Architecture and collaboration notes
tests/                   Frontend, backend, and integration tests
scripts/                 Local development and synthetic-data utilities
```

## Local setup

Use Python 3.11 or newer and Node.js 20 or newer.

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp -n .env.example .env
cp -n frontend/web/.env.example frontend/web/.env.local
```

Run the API and dashboard in separate terminals from the repository root.

Terminal 1 — API:

```bash
source .venv/bin/activate
set -a && source .env && set +a
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Terminal 2 — React dashboard:

```bash
cd frontend/web
npm install
npm run dev
```

The sample role tokens are synthetic local-demo values used only by the role selector. Vite exposes its `VITE_*` values to the browser, so never put real API credentials or LLM keys in the frontend environment file. This demo authentication is not suitable for production or real patient data.

Open the Vite URL shown in the terminal (normally `http://127.0.0.1:5173`). The dashboard proxies `/api` requests to FastAPI on port 8000. The SQLite simulation database is stored in the system temporary directory by default; set `BAIN_DB_PATH` to use another local path. All included cases are synthetic. Do not add real patient information or API keys to this repository.

To reset the synthetic cases for a fresh demo, run `python scripts/reset_demo_db.py`. If the selected database already exists, add `--force` to confirm replacing it. See [docs/demo_plan.md](docs/demo_plan.md) for the live scenario sequence and expected safety checks.

## Verification

```bash
python -m pytest -q
cd frontend/web && npm run build
```

## Git workflow

- `main` is the stable integration branch.
- Dashboard work goes to `feature/dashboard`.
- Backend and agent work goes to `feature/backend-agent`.
- Open pull requests into `main`; do not force-push or overwrite another developer's work.
- Before implementation begins, review and finalize the shared contract together.

See [docs/team_workflow.md](docs/team_workflow.md) and [docs/architecture.md](docs/architecture.md).
