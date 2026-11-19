"""ClearTurn / bAin FastAPI backend — Phase 1: Patient #402 vertical slice."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from backend.auth import (
    Actor,
    AuthError,
    can_access_analyze,
    filter_agent_activity_for_role,
    filter_audit_events_for_role,
    filter_exceptions_for_role,
    filter_patient_detail_for_role,
    filter_patient_summary_for_role,
    filter_workflow_for_role,
    get_current_actor,
)
from backend.db import ClearTurnStore, WorkflowError
from backend.llm_advisory import AdvisoryProvider, AdvisoryService
from backend.models import (
    AgentActivityListResponse,
    AgentAnalyzeRequest,
    AgentAnalyzeResponse,
    AuditListResponse,
    DashboardSummary,
    DepartmentStatusListResponse,
    ErrorCode,
    ErrorResponse,
    ExceptionItem,
    ExceptionListResponse,
    ExceptionResolveRequest,
    ExceptionStatus,
    MilestoneUpdateRequest,
    MilestoneUpdateResponse,
    PatientDetail,
    PatientListResponse,
    WorkflowResponse,
    WorkflowStatus,
)

PATIENT_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


def _error_json(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    reason_phrase = HTTPStatus(status_code).phrase
    body = ErrorResponse(
        error=reason_phrase,
        code=code,
        message=message,
        details=details or None,
    )
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
    )


def create_app(
    db_path: str | None = None,
    advisory_provider: AdvisoryProvider | None = None,
) -> FastAPI:
    store = ClearTurnStore(db_path=db_path)
    store.init_and_seed()
    advisory = AdvisoryService(store, provider=advisory_provider)

    application = FastAPI(title="bAin API", version="1.0.0")
    application.state.store = store
    application.state.advisory = advisory

    @application.exception_handler(AuthError)
    async def handle_auth_error(_request: Request, exc: AuthError) -> JSONResponse:
        return _error_json(exc.status_code, exc.code, exc.message, exc.details)

    @application.exception_handler(WorkflowError)
    async def handle_workflow_error(_request: Request, exc: WorkflowError) -> JSONResponse:
        return _error_json(exc.status_code, exc.code, exc.message, exc.details)

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _error_json(
            422,
            ErrorCode.VALIDATION_ERROR,
            "Request failed validation.",
            {"errors": jsonable_encoder(exc.errors())},
        )

    @application.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, _exc: Exception) -> JSONResponse:
        return _error_json(
            500,
            ErrorCode.INTERNAL_ERROR,
            "An internal error occurred.",
        )

    @application.get("/api/health", tags=["system"])
    def health() -> dict[str, str]:
        """Report that the API process is responding; this is not a workflow readiness check."""
        return {"status": "ok", "service": "bAin-api"}

    @application.get(
        "/api/dashboard/summary",
        response_model=DashboardSummary,
        tags=["dashboard"],
    )
    def get_dashboard_summary(
        actor: Actor = Depends(get_current_actor),
    ) -> DashboardSummary:
        return store.get_dashboard_summary(actor.role)

    @application.get(
        "/api/departments/status",
        response_model=DepartmentStatusListResponse,
        tags=["departments"],
    )
    def get_departments_status(
        actor: Actor = Depends(get_current_actor),
    ) -> DepartmentStatusListResponse:
        return DepartmentStatusListResponse(items=store.get_departments_status(actor.role))

    @application.get("/api/patients", response_model=PatientListResponse, tags=["patients"])
    def list_patients(
        actor: Actor = Depends(get_current_actor),
        status: WorkflowStatus | None = Query(default=None),
    ) -> PatientListResponse:
        rows = store.list_patients(status_filter=status)
        items = [
            filter_patient_summary_for_role(actor.role, summary, statuses)
            for summary, statuses in rows
        ]
        return PatientListResponse(items=items)

    @application.get(
        "/api/patients/{patient_id}",
        response_model=PatientDetail,
        tags=["patients"],
    )
    def get_patient(
        patient_id: Annotated[str, Path(pattern=PATIENT_ID_PATTERN)],
        actor: Actor = Depends(get_current_actor),
    ) -> PatientDetail:
        detail, statuses = store.get_patient_detail(patient_id)
        return filter_patient_detail_for_role(actor.role, detail, statuses)

    @application.get(
        "/api/patients/{patient_id}/workflow",
        response_model=WorkflowResponse,
        tags=["workflow"],
    )
    def get_patient_workflow(
        patient_id: Annotated[str, Path(pattern=PATIENT_ID_PATTERN)],
        actor: Actor = Depends(get_current_actor),
    ) -> WorkflowResponse:
        workflow, statuses = store.get_workflow(patient_id)
        return filter_workflow_for_role(actor.role, workflow, statuses)

    @application.post(
        "/api/patients/{patient_id}/milestones",
        response_model=MilestoneUpdateResponse,
        tags=["workflow"],
    )
    def update_patient_milestone(
        patient_id: Annotated[str, Path(pattern=PATIENT_ID_PATTERN)],
        body: MilestoneUpdateRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
        actor: Actor = Depends(get_current_actor),
    ) -> MilestoneUpdateResponse:
        return store.update_milestone(
            patient_id=patient_id,
            milestone_id=body.milestone_id,
            target_status=body.target_status,
            expected_version=body.expected_version,
            reason=body.reason,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    @application.get(
        "/api/patients/{patient_id}/audit",
        response_model=AuditListResponse,
        tags=["workflow"],
    )
    def get_patient_audit(
        patient_id: Annotated[str, Path(pattern=PATIENT_ID_PATTERN)],
        actor: Actor = Depends(get_current_actor),
    ) -> AuditListResponse:
        events = store.get_audit_events(patient_id)
        return AuditListResponse(items=filter_audit_events_for_role(actor.role, events))

    @application.get("/api/exceptions", response_model=ExceptionListResponse, tags=["exceptions"])
    def list_exceptions(
        actor: Actor = Depends(get_current_actor),
        status: ExceptionStatus | None = Query(default=None),
    ) -> ExceptionListResponse:
        items = store.list_exceptions(status_filter=status)
        return ExceptionListResponse(items=filter_exceptions_for_role(actor.role, items))

    @application.post(
        "/api/exceptions/{exception_id}/resolve",
        response_model=ExceptionItem,
        tags=["exceptions"],
    )
    def resolve_exception(
        exception_id: str,
        body: ExceptionResolveRequest,
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
        actor: Actor = Depends(get_current_actor),
    ) -> ExceptionItem:
        return store.resolve_exception(
            exception_id=exception_id,
            resolution=body.resolution,
            expected_version=body.expected_version,
            actor=actor,
            idempotency_key=idempotency_key,
        )

    @application.get(
        "/api/agent/activity",
        response_model=AgentActivityListResponse,
        tags=["agent"],
    )
    def get_agent_activity(
        actor: Actor = Depends(get_current_actor),
    ) -> AgentActivityListResponse:
        rows = store.list_agent_activity()
        return AgentActivityListResponse(
            items=filter_agent_activity_for_role(actor.role, rows)
        )

    @application.post(
        "/api/agent/analyze",
        response_model=AgentAnalyzeResponse,
        tags=["agent"],
    )
    def analyze_case(
        body: AgentAnalyzeRequest,
        actor: Actor = Depends(get_current_actor),
    ) -> AgentAnalyzeResponse:
        if not can_access_analyze(actor.role):
            raise AuthError(
                status_code=403,
                code=ErrorCode.FORBIDDEN,
                message=f"Role '{actor.role.value}' is not authorized to access advisory analysis.",
                details={"actor_role": actor.role.value},
            )
        return advisory.analyze_case(
            patient_id=body.patient_id,
            actor=actor,
            question=body.question,
        )

    return application


app = create_app()
