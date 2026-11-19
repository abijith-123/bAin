# Initial architecture

## Boundary

- **Streamlit (`frontend/`)** renders the dashboard and calls the API. It is not an authority for permissions or workflow safety.
- **FastAPI (`backend/`)** authenticates demo identities, applies role checks, validates workflow transitions, and owns all state changes.
- **Workflow rules** are deterministic Python code. The LLM may summarize, explain, and draft escalation text, but it cannot mutate state or make clinical/insurance decisions.
- **SQLite** is the planned local simulation store. Updates that change workflow state should use transactions, expected versions, and idempotency keys.
- **Synthetic fixtures** are the only patient-like data allowed in this repo.

## Draft discharge dependency graph

```text
Clinical clearance -> Pharmacy ----+
                    -> Insurance --+-> Patient departed -> Housekeeping -> Bed ready
```

Pharmacy and insurance can proceed in parallel after clinical clearance. Patient departure requires both. Housekeeping is locked until departure is recorded. The authoritative transition rules and role matrix must be checked against the official competition brief before implementation.

## API boundary

`shared/api_contract.yaml` is the shared draft. Both developers should use it as the sole field/status vocabulary while implementing, then update it together if review of the official document requires changes. The UI must not assume a successful state change until the API confirms it.
