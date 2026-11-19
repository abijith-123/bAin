# Two-person Git workflow

## Branch ownership

| Branch | Owner | Main paths |
| --- | --- | --- |
| `main` | Both | Stable reviewed integration |
| `feature/dashboard` | Developer 1 | `frontend/`, `tests/frontend/` |
| `feature/backend-agent` | Developer 2 | `backend/`, `tests/backend/` |

Shared files (`shared/`, `docs/`, `requirements.txt`, and integration tests) need a quick agreement before edits. Avoid simultaneous changes to the same file.

## Daily loop

1. Pull the latest `main` and merge it into your feature branch.
2. Work only in your owned paths unless the other developer agrees.
3. Keep API payloads and status values aligned with `shared/api_contract.yaml`.
4. Commit a focused change and push the feature branch.
5. Open a pull request to `main`; review and resolve integration issues before merge.
6. Never force-push shared history or commit `.env`, keys, real patient data, or local database files.

## Initial contract gate

Before parallel feature work, both developers should read the official competition document, finalize the OpenAPI contract, agree on authentication/demo identity behavior, and confirm branch ownership. This starter contract is explicitly marked draft.
