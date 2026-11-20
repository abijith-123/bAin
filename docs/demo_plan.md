# bAin live demo plan

All records in this demo are synthetic. Use the local role selector to demonstrate which actions and details are available to each role. A specialist's resolution changes a blocked milestone to `ready`; a separate authorized workflow action is still needed to complete it.

## Prepare a clean run

From the repository root, copy the sample local configuration once:

```bash
cp -n .env.example .env
cp -n frontend/web/.env.example frontend/web/.env.local
python scripts/reset_demo_db.py
```

If the selected database already exists, the script refuses to delete it unless you explicitly pass `--force`. To use a separate database for a presentation, run `python scripts/reset_demo_db.py --database /tmp/bain-stage.sqlite3 --force` and start the API with `BAIN_DB_PATH=/tmp/bain-stage.sqlite3`.

Start the API and dashboard in separate terminals:

```bash
set -a && source .env && set +a
uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

```bash
cd frontend/web
npm install
npm run dev
```

Open `http://127.0.0.1:5173/`. Confirm the API badge says **API Online** and the queue contains synthetic cases #402–#407.

## Scenarios

| Scenario | Case and roles | Expected result |
|---|---|---|
| Normal discharge | #406: Pharmacist completes Pharmacy, billing specialist completes Insurance, transport staff completes Transport and confirms Patient Departed, then housekeeping dispatches cleaning and marks Bed Ready. Refresh the case version after each successful write. | Every transition is accepted in order; the final case status becomes completed. |
| Pharmacy stockout | #403: inspect as the pharmacist, resolve `MEDICATION_STOCKOUT` with a short resolution note, then separately complete Pharmacy. | The exception is audited and Pharmacy becomes ready; the agent does not make a medication decision. |
| Insurance rejection | #404: inspect as billing specialist, resolve `ERR_AUTH_409`, then separately complete Insurance. | The human review is audited; the insurer's decision is not overridden. |
| Parallel department checks | #402: run `check_pharmacy_and_insurance("402")`. | Both independent synthetic status reads return, and any missing blocked-department exceptions are created once in a transaction. |
| One department service unavailable | Run `python -m pytest -q tests/backend/test_transport_workflow.py -k service_failure`. | A simulated Pharmacy timeout is reported as `unavailable`; the blocked Insurance result still opens its billing exception. |
| DAG safety | #405: try to dispatch housekeeping while Patient Departed is pending. | Backend returns `409 INVALID_TRANSITION`; the milestone remains pending and the attempt is audited. |
| RBAC and privacy | Select Housekeeping and inspect #405. Try to resolve a transport exception as Housekeeping. | The API rejects the resolution; clinical notes, clearance time, and patient name remain redacted. |
| Transport specialist review | #405: select Patient Transport, resolve `TRANSPORT_DELAY`, then separately mark Transport completed and confirm departure. | The resolution changes Transport to ready, updates its version, and is audited. Departure remains locked until completion. |
| Concurrent writes | Run `python -m pytest -q tests/backend/test_transport_workflow.py -k parallel`. | Two writes using the same case version serialize; one succeeds and the stale request receives `STALE_VERSION`, with no lost update. |

## Final checks

```bash
python -m pytest -q
cd frontend/web && npm run build
```

Confirm the audit timeline shows the action, actor role, outcome, state change, and case version. AI analysis currently uses the deterministic fallback; this runbook does not represent it as an LLM response.

## Round 2 patient 403 rehearsal

Use a fresh disposable SQLite path for BAIN_DB_PATH and matching local backend/Vite
demo tokens, including SOCIAL_WORKER. Patient 403 is 81 and lives alone.

1. In the Exception Inbox, the pharmacist clears the stockout. In Patient Detail,
   complete Pharmacy. As Transport, complete Transport ready.
2. Select Patient departed. Its remaining prerequisite is Social Work clearance.
   In the Assistant select **Why is this patient waiting?** as the nurse, Transport,
   and Housekeeping. The nurse sees Social Work, Transport sees a pickup hold,
   and Housekeeping sees a room-release hold.
3. As Social Worker, select Social Work clearance, record evidence, start and
   complete the review. Departure now unlocks.
4. As Attending Physician, select Clinical clearance, record a reason and choose
   **Revoke clearance**. Inspect the Audit Timeline for the invalidations at one
   version. Booking and sign-offs must be obtained again.
5. Re-clear as the doctor. Start and complete Pharmacy, Insurance, Transport and
   Social Work as their respective owners. Transport confirms departure, then
   Housekeeping dispatches and completes Bed ready.
