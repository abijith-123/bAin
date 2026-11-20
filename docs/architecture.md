# bAin architecture

## Boundary

- **React + Vite (`frontend/web/`)** renders the operations dashboard and calls FastAPI. UI role checks are presentation only; backend authorization remains authoritative.
- **FastAPI (`backend/`)** validates local synthetic demo tokens, enforces roles and workflow dependencies, and owns state changes.
- **Workflow rules** are deterministic Python code. AI analysis is read-only and currently uses the labeled deterministic fallback; it cannot mutate state or make clinical or insurance decisions.
- **SQLite** stores synthetic cases, independent Pharmacy/Insurance mock-service state, exceptions, idempotency records, and audit events. State changes use atomic transactions and optimistic case versions.
- **Synthetic fixtures** are the only patient-like data allowed in this repo. Demo tokens are local configuration examples and are not production credentials.

## Draft discharge dependency graph

```text
Clinical clearance -> Pharmacy ----+
                    -> Insurance --+-> Patient departed -> Housekeeping -> Bed ready
                    -> Transport --+
```

Pharmacy, Insurance, and any required Transport preparation can proceed in parallel after clinical clearance. Patient departure requires Pharmacy and Insurance completion; cases with `transport_required` also require Transport completion. Housekeeping is locked until departure is recorded.

## API boundary

`shared/api_contract.yaml` is the shared draft. Both developers should use it as the sole field/status vocabulary while implementing, then update it together if review of the official document requires changes. The UI must not assume a successful state change until the API confirms it.
