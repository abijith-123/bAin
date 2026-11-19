"""Phase 3D read-only advisory LLM service and provider adapters.

Safety invariants:
- Strictly read-only: never receives database write handles or mutates workflow state.
- Role-based access control and information minimization are applied BEFORE calling any LLM.
- Provider output is validated; on timeout, misconfiguration, or invalid output,
  a deterministic fallback response is returned with generation_mode='deterministic_fallback'.
- Never hardcodes API keys, never logs raw clinical notes/keys/prompts, and never downloads weights.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from backend.auth import (
    CLINICAL_PHI_ROLES,
    Actor,
    filter_exceptions_for_role,
    filter_patient_detail_for_role,
    filter_workflow_for_role,
)
from backend.db import ClearTurnStore
from backend.models import (
    AgentActivityType,
    AgentAnalyzeResponse,
    ExceptionStatus,
    GenerationMode,
    Role,
    WorkflowStatus,
)


@dataclass(frozen=True)
class MinimizedAdvisoryContext:
    """Minimum authorized synthetic context safe to send to an advisory provider."""

    patient_id: str
    caller_role: str
    workflow_status: str
    version: int
    transport_required: bool
    blockers: list[str]
    milestones: list[dict[str, Any]]
    open_exceptions: list[dict[str, Any]]
    clinical_summary: str | None
    question: str | None


class AdvisoryProvider(Protocol):
    name: str

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        """Return a structured dict matching AgentAnalyzeResponse fields (excluding generation_mode)."""
        ...


def build_minimized_context(
    store: ClearTurnStore,
    *,
    patient_id: str,
    actor: Actor,
    question: str | None,
) -> MinimizedAdvisoryContext:
    """Apply server-side RBAC and information minimization BEFORE provider invocation."""
    detail, statuses = store.get_patient_detail(patient_id)
    workflow, _ = store.get_workflow(patient_id)

    filtered_detail = filter_patient_detail_for_role(actor.role, detail, statuses)
    filtered_wf = filter_workflow_for_role(actor.role, workflow, statuses)

    open_excs = [
        e
        for e in store.list_exceptions(status_filter=ExceptionStatus.open)
        if e.patient_id == patient_id
    ]
    visible_excs = filter_exceptions_for_role(actor.role, open_excs)

    # Information minimization: never send patient display_name to external/local LLMs;
    # only include synthetic clinical_summary for explicitly authorized clinical roles.
    minimized_clinical_summary = (
        filtered_detail.clinical_summary if actor.role in CLINICAL_PHI_ROLES else None
    )

    return MinimizedAdvisoryContext(
        patient_id=filtered_wf.patient_id,
        caller_role=actor.role.value,
        workflow_status=filtered_detail.workflow_status.value,
        version=filtered_wf.version,
        transport_required=filtered_wf.transport_required,
        blockers=list(filtered_wf.blockers),
        milestones=[
            {
                "milestone_id": m.milestone_id,
                "status": m.status.value,
                "locked": m.locked,
            }
            for m in filtered_wf.milestones
        ],
        open_exceptions=[
            {
                "exception_id": e.exception_id,
                "department": e.department.value,
                "code": e.code,
                "status": e.status.value,
            }
            for e in visible_excs
        ],
        clinical_summary=minimized_clinical_summary,
        question=question.strip() if question and question.strip() else None,
    )


def build_deterministic_advisory(context: MinimizedAdvisoryContext) -> AgentAnalyzeResponse:
    """Always-available offline deterministic advisory explanation and escalation draft."""
    ms_by_id = {m["milestone_id"]: m for m in context.milestones}
    suggested_actions: list[str] = []
    escalation_lines: list[str] = []

    for exc in context.open_exceptions:
        dept = exc["department"]
        code = exc["code"]
        exc_id = exc["exception_id"]
        if dept == "pharmacy":
            suggested_actions.append(
                f"Pharmacist review required to resolve {code} ({exc_id}) and confirm Pharmacy completion."
            )
            escalation_lines.append(
                f"[Pharmacy Escalation] Patient #{context.patient_id}: {code} ({exc_id}) is blocking discharge. "
                "Please verify stock/substitution and record human resolution."
            )
        elif dept == "insurance":
            suggested_actions.append(
                f"Billing Specialist review required to resolve {code} ({exc_id}) and confirm Insurance clearance."
            )
            escalation_lines.append(
                f"[Billing Escalation] Patient #{context.patient_id}: {code} ({exc_id}) requires specialist review "
                "and payer resubmission; automated override is disabled."
            )
        else:
            suggested_actions.append(
                f"Authorized {dept} specialist review required for {code} ({exc_id})."
            )

    if ms_by_id.get("pharmacy", {}).get("status") == WorkflowStatus.ready.value:
        suggested_actions.append("Pharmacist can confirm Pharmacy milestone completion.")
    if ms_by_id.get("insurance", {}).get("status") == WorkflowStatus.ready.value:
        suggested_actions.append("Billing Specialist can confirm Insurance milestone completion.")
    if ms_by_id.get("patient_departed", {}).get("status") == WorkflowStatus.ready.value:
        suggested_actions.append(
            "Authorized Nurse or Transport Staff must confirm physical patient departure before Housekeeping dispatch."
        )
    if ms_by_id.get("housekeeping_dispatched", {}).get("status") == WorkflowStatus.ready.value:
        suggested_actions.append("Housekeeping can dispatch room turnover cleaning.")
    if ms_by_id.get("bed_ready", {}).get("status") == WorkflowStatus.ready.value:
        suggested_actions.append("Housekeeping can mark the bed ready once turnover completes.")

    if not suggested_actions:
        if context.workflow_status == WorkflowStatus.completed.value:
            suggested_actions.append("All discharge and bed-turnover milestones are completed.")
        else:
            suggested_actions.append("Monitor active milestones; no immediate manual escalation required.")

    if context.blockers:
        summary = (
            f"Patient #{context.patient_id} (v{context.version}) is currently '{context.workflow_status}' "
            f"with {len(context.blockers)} visible blocker(s): {', '.join(context.blockers)}. "
            "Advisory only — specialist human approval and backend DAG validation govern all transitions."
        )
    else:
        summary = (
            f"Patient #{context.patient_id} (v{context.version}) is currently '{context.workflow_status}' "
            "with no visible blockers for your role. "
            "Advisory only — backend permissions and DAG prerequisites govern all transitions."
        )

    escalation_draft = "\n".join(escalation_lines) if escalation_lines else None

    return AgentAnalyzeResponse(
        summary=summary,
        blockers=list(context.blockers),
        suggested_actions=suggested_actions,
        escalation_draft=escalation_draft,
        generation_mode=GenerationMode.deterministic_fallback,
    )


def _build_advisory_prompt(context: MinimizedAdvisoryContext) -> tuple[str, str]:
    system_prompt = (
        "You are ClearTurn's read-only hospital discharge advisory assistant. "
        "You CANNOT mutate state, override clinical orders, approve insurance denials, "
        "confirm patient departure, or dispatch housekeeping. "
        "Return STRICT JSON with keys: summary (string), blockers (array of strings), "
        "suggested_actions (array of strings), escalation_draft (string or null). "
        "Use ONLY the provided synthetic JSON context."
    )
    user_payload = json.dumps(asdict(context), separators=(",", ":"))
    return system_prompt, user_payload


def _extract_json_object(raw_text: str) -> dict[str, Any]:
    text = raw_text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("Provider output must be a JSON object.")
    return parsed


class OllamaProvider:
    """Optional local Ollama adapter; only runs when configured and reachable. Never downloads weights."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.base_url = (base_url or os.getenv("BAIN_OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = (model or os.getenv("BAIN_OLLAMA_MODEL", "")).strip()
        self.timeout_seconds = timeout_seconds

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        if not self.model:
            raise RuntimeError("BAIN_OLLAMA_MODEL is not configured.")
        system_prompt, user_prompt = _build_advisory_prompt(context)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(
                f"{self.base_url}/api/chat",
                json={
                    "model": self.model,
                    "stream": False,
                    "format": "json",
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data.get("message", {}).get("content", "")
            return _extract_json_object(content)


class GroqProvider:
    """Optional Groq cloud adapter; enabled only when GROQ_API_KEY is set."""

    name = "groq"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("GROQ_API_KEY", "")).strip()
        self.model = (model or os.getenv("BAIN_GROQ_MODEL", "llama-3.1-8b-instant")).strip()
        self.timeout_seconds = timeout_seconds

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY is not configured.")
        system_prompt, user_prompt = _build_advisory_prompt(context)
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return _extract_json_object(content)


class GeminiProvider:
    """Optional Gemini API adapter; enabled only when GEMINI_API_KEY is set."""

    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self.api_key = (api_key or os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = (model or os.getenv("BAIN_GEMINI_MODEL", "gemini-2.0-flash")).strip()
        self.timeout_seconds = timeout_seconds

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is not configured.")
        system_prompt, user_prompt = _build_advisory_prompt(context)
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent"
        )
        with httpx.Client(timeout=self.timeout_seconds) as client:
            resp = client.post(
                url,
                headers={"x-goog-api-key": self.api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system_prompt}]},
                    "contents": [{"parts": [{"text": user_prompt}]}],
                    "generationConfig": {
                        "temperature": 0.1,
                        "responseMimeType": "application/json",
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["candidates"][0]["content"]["parts"][0]["text"]
            return _extract_json_object(content)


def build_provider_from_env() -> AdvisoryProvider | None:
    """Select an optional LLM provider from environment variables; default is disabled (offline)."""
    selected = os.getenv("BAIN_LLM_PROVIDER", "disabled").strip().lower()
    if selected in {"", "disabled", "none", "fallback"}:
        return None
    if selected == "ollama":
        return OllamaProvider()
    if selected == "groq":
        return GroqProvider()
    if selected == "gemini":
        return GeminiProvider()
    return None


def _validate_provider_output(
    raw: dict[str, Any],
    context: MinimizedAdvisoryContext,
) -> AgentAnalyzeResponse:
    """Strictly validate LLM output and force generation_mode='llm'."""
    summary = raw.get("summary")
    blockers = raw.get("blockers")
    suggested_actions = raw.get("suggested_actions")
    escalation_draft = raw.get("escalation_draft")

    if not isinstance(summary, str) or len(summary.strip()) < 5:
        raise ValueError("Invalid summary in provider output.")
    if not isinstance(blockers, list) or not all(isinstance(b, str) for b in blockers):
        raise ValueError("Invalid blockers list in provider output.")
    if not isinstance(suggested_actions, list) or not all(
        isinstance(a, str) for a in suggested_actions
    ):
        raise ValueError("Invalid suggested_actions list in provider output.")
    if escalation_draft is not None and not isinstance(escalation_draft, str):
        raise ValueError("Invalid escalation_draft in provider output.")

    # Always anchor blockers to the server-side role-filtered ground truth so an LLM
    # can never hallucinate or leak unauthorized department blockers across roles.
    effective_blockers = list(context.blockers)

    return AgentAnalyzeResponse(
        summary=summary.strip(),
        blockers=effective_blockers,
        suggested_actions=[a.strip() for a in suggested_actions if a.strip()],
        escalation_draft=escalation_draft.strip() if isinstance(escalation_draft, str) and escalation_draft.strip() else None,
        generation_mode=GenerationMode.llm,
    )


class AdvisoryService:
    """Read-only advisory facade enforcing pre-LLM RBAC minimization and safe deterministic fallback."""

    def __init__(
        self,
        store: ClearTurnStore,
        *,
        provider: AdvisoryProvider | None = None,
    ) -> None:
        self.store = store
        self._explicit_provider = provider

    def _resolve_provider(self) -> AdvisoryProvider | None:
        if self._explicit_provider is not None:
            return self._explicit_provider
        return build_provider_from_env()

    def analyze_case(
        self,
        *,
        patient_id: str,
        actor: Actor,
        question: str | None = None,
    ) -> AgentAnalyzeResponse:
        """Generate a read-only advisory explanation and escalation draft."""
        context = build_minimized_context(
            self.store,
            patient_id=patient_id,
            actor=actor,
            question=question,
        )
        scope = _activity_scope_for_role(actor.role)
        provider = self._resolve_provider()

        if provider is None:
            fallback = build_deterministic_advisory(context)
            self.store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.summary,
                message=(
                    f"Generated read-only advisory analysis for Patient #{patient_id} "
                    "using deterministic fallback (LLM provider disabled)."
                ),
                generation_mode=GenerationMode.deterministic_fallback,
                visibility_scope=scope,
            )
            return fallback

        try:
            raw_output = provider.generate(context)
            validated = _validate_provider_output(raw_output, context)
            self.store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.summary,
                message=(
                    f"Generated read-only advisory analysis for Patient #{patient_id} "
                    f"via provider '{provider.name}'."
                ),
                generation_mode=GenerationMode.llm,
                visibility_scope=scope,
            )
            return validated
        except Exception:
            fallback = build_deterministic_advisory(context)
            self.store.record_agent_activity(
                patient_id=patient_id,
                activity_type=AgentActivityType.recovery,
                message=(
                    f"Advisory provider '{provider.name}' unavailable or returned invalid output "
                    f"for Patient #{patient_id}; safely fell back to deterministic advisory."
                ),
                generation_mode=GenerationMode.deterministic_fallback,
                visibility_scope=scope,
            )
            return fallback


def _activity_scope_for_role(role: Role) -> str:
    if role == Role.pharmacist:
        return "pharmacy"
    if role == Role.billing_specialist:
        return "insurance"
    return "clinical"
