"""Minimal API scaffold. Workflow behavior will be implemented after the contract is finalized."""

from fastapi import FastAPI

app = FastAPI(title="bAin API", version="0.1.0")


@app.get("/api/health", tags=["system"])
def health() -> dict[str, str]:
    """Report that the API process is responding; this is not a workflow readiness check."""
    return {"status": "ok", "service": "bAin-api"}
