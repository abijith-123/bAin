"""Read-only real LLM advisory service and provider adapters for bAin.

Safety invariants:
- Strictly read-only on workflow state: never mutates cases, milestones, exceptions, or audit logs.
- Server-side RBAC and information minimization are applied BEFORE calling any provider.
- Provider output is validated against the /api/agent/analyze contract, blockers are anchored
  to actual database state, and unsafe autonomous recommendations trigger deterministic fallback.
- On disabled provider, missing credentials, timeout, network error, or invalid output,
  an always-working deterministic fallback response is returned with
  generation_mode='deterministic_fallback'.
- Never hardcodes API keys, never logs credentials or raw prompts, and never downloads weights.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx


CLINICAL_SUMMARY_ROLES = {"doctor", "nurse"}
ADVISORY_ALLOWED_ROLES = {"admin", "doctor", "nurse", "pharmacist", "billing_specialist"}

DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"

# Direct negation markers that negate only the immediately following verb phrase.
_DIRECT_NEGATION_RE = re.compile(
    r"(?:^|[\s,;:(])"
    r"(?:do\s+not|don't|does\s+not|doesn't|did\s+not|didn't|"
    r"never|cannot|can't|can\s+not|could\s+not|couldn't|"
    r"must\s+not|mustn't|should\s+not|shouldn't|will\s+not|won't|"
    r"may\s+not|not\s+permitted\s+to|not\s+allowed\s+to|prohibited\s+from|"
    r"unable\s+to|refuse\s+to|decline\s+to|avoid)\s+"
    r"(?:ever\s+|simply\s+|just\s+|directly\s+|manually\s+|automatically\s+|"
    r"(?:be|been)\s+|(?:attempt|try|seek|proceed)\s+to\s+){0,2}$",
    re.IGNORECASE,
)

# Disjunctive list continuation allowed ONLY after an already-negated unsafe directive in the same sentence.
_NEGATED_LIST_CONTINUATION_RE = re.compile(
    r"^(?:[^.!?\n;]{0,35}?)(?:,\s*(?:or|nor)\s+|\s+(?:or|nor)\s+|,\s+)"
    r"(?:ever\s+|simply\s+|just\s+|directly\s+|manually\s+|automatically\s+|to\s+|be\s+)?$",
    re.IGNORECASE,
)
_CLAUSE_BREAK_WORD_RE = re.compile(
    r"\b(?:and|but|then|yet|while|whereas|however|instead|rather|otherwise|if|unless|when|because)\b",
    re.IGNORECASE,
)

_UNSAFE_DIRECTIVE_PATTERNS = (
    re.compile(
        r"\bauto[- ]?approv(?:e|ing|al|ed)\b|\bautomatically\s+approv(?:e|ing|ed)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:overrid(?:e|ing|den)|ignor(?:e|ing)|disregard(?:ing)?|waiv(?:e|ing))\s+"
        r"(?:the\s+)?(?:insurance|payer|insurer|denial|err_auth_409|clinical\s+order|pharmacist|billing|exception)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:insurance|payer|insurer|denial|err_auth_409|clinical\s+order)\b"
        r"[^.!?\n;]{0,25}\b(?:be\s+)?overridden\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:bypass(?:ing)?|skip(?:ping)?|circumvent(?:ing)?)\s+"
        r"(?:the\s+)?(?:pharmacist|billing|specialist|human|departure|dag|review|approval|gate)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:alter(?:ing)?|modif(?:y|ying)|chang(?:e|ing)|cancel(?:ing|ling)?|discontinu(?:e|ing))\s+"
        r"(?:the\s+)?(?:clinical|medication|prescription)\s+order\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:auto[- ]?|automatically\s+)(?:confirm|complete|mark|record)(?:ing|ed)?\s+"
        r"(?:the\s+)?(?:patient\s+)?departure\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:auto[- ]?|automatically\s+)resolv(?:e|ing|ed)\s+"
        r"(?:the\s+)?(?:exception|stockout|err_auth_409|insurance|pharmacy|blocker)\b",
        re.IGNORECASE,
    ),
)

_HOUSEKEEPING_TARGET = (
    r"(?:housekeeping|evs|environmental\s+services|room\s+(?:cleaning|turnover)|"
    r"bed\s+(?:turnover|cleaning|prep|preparation)|(?:the\s+)?bed\s+ready|bed_ready)"
)
_HOUSEKEEPING_ACTION_PATTERNS = (
    re.compile(
        r"\b(?:dispatch(?:ing|ed)?|send(?:ing)?|assign(?:ing)?|schedul(?:e|ing)|start(?:ing)?|"
        r"begin(?:ning)?|initiat(?:e|ing)|trigger(?:ing)?|deploy(?:ing)?|mobiliz(?:e|ing)|"
        r"notify(?:ing)?|alert(?:ing)?|proceed(?:ing)?\s+with|perform(?:ing)?|execut(?:e|ing)|"
        r"complet(?:e|ing)|mark(?:ing)?|prepar(?:e|ing)|turn(?:ing)?\s+over)\b"
        r"[^.!?\n;]{0,40}\b" + _HOUSEKEEPING_TARGET + r"\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:housekeeping|evs)\b\s+(?:should|must|can|may|needs\s+to|to)\s+"
        r"(?:be\s+)?(?:dispatch(?:ed)?|start|begin|clean|turn\s+over|proceed|prepare|ready)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:clean(?:ing)?|turn(?:ing)?\s+over)\s+(?:the\s+)?(?:patient(?:'s)?\s+)?(?:room|bed)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bmark(?:ing)?\s+(?:the\s+)?bed\s+(?:as\s+)?ready\b", re.IGNORECASE),
)
_PRE_DEPARTURE_TIMING_RE = re.compile(
    r"\b(?:(?:before|prior\s+to|ahead\s+of)\s+(?:the\s+)?(?:confirmed\s+)?(?:patient\s+)?depart(?:s|ed|ure|ing)?|"
    r"until\s+after\s+nothing)\b",
    re.IGNORECASE,
)
_EARLY_DISPATCH_MODIFIER_RE = re.compile(
    r"\b(?:before|prior\s+to|ahead\s+of)\s+(?:the\s+)?(?:confirmed\s+)?(?:patient\s+)?depart|"
    r"\bimmediately\b|\bright\s+away\b|\bnow\b",
    re.IGNORECASE,
)
_DEPARTURE_GATE_RE = re.compile(
    r"\b(?:"
    r"(?:after|once|when|following|upon|until)\s+(?:the\s+)?(?:confirmed\s+)?(?:patient\s+)?depart(?:s|ed|ure|ing)?\b|"
    r"(?:after|once|when|following|upon|until)\s+(?:physical\s+)?(?:patient\s+)?departure\s+"
    r"(?:is|has\s+been)\s+(?:confirmed|completed|recorded)\b|"
    r"(?:once|when|after|until)\s+(?:the\s+)?(?:patient\s+(?:has\s+)?departed|confirmed\s+departure)\b|"
    r"(?:wait(?:ing)?|locked|blocked|gated|hold)\s+until\s+[^.!?\n;]{0,40}\bdepart(?:s|ed|ure|ing)?\b|"
    r"only\s+after\s+[^.!?\n;]{0,40}\bdepart(?:s|ed|ure|ing)?\b"
    r")",
    re.IGNORECASE,
)


def _configured_timeout_seconds() -> float:
    raw = os.getenv("BAIN_LLM_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return max(1.0, min(value, 30.0))


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
    model: str

    def generate(self, context: MinimizedAdvisoryContext) -> dict[str, Any]:
        """Return a structured dict matching /api/agent/analyze fields (excluding generation_mode)."""
        ...


def _visible_exceptions_for_role(open_exceptions: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    if role in {"doctor", "nurse", "admin"}:
        return list(open_exceptions)
    if role == "pharmacist":
        return [e for e in open_exceptions if e["department"] == "pharmacy"]
    if role == "billing_specialist":
        return [e for e in open_exceptions if e["department"] == "insurance"]
    return []


def _role_filtered_blockers(
    db: Any,
    case: dict[str, Any],
    visible_exceptions: list[dict[str, Any]],
    role: str,
) -> list[str]:
    blockers = [e["code"] for e in visible_exceptions]
    if role in {"billing_specialist", "doctor", "nurse", "admin"}:
        upheld = db.execute(
            """SELECT code FROM exceptions
               WHERE patient_id = ? AND status = 'resolved' AND resolution_outcome = 'denial_upheld'
               ORDER BY exception_id""",
            (case["patient_id"],),
        ).fetchall()
        for row in upheld:
            blockers.append(f"{row['code']}: denial upheld; follow-up required")
    if case["milestones"].get("transport") == "blocked" and role in {"doctor", "nurse", "admin"}:
        blockers.append("Transport availability pending")
    return list(dict.fromkeys(blockers))


def build_minimized_context(
    *,
    patient_id: str,
    role: str,
    question: str | None,
) -> MinimizedAdvisoryContext:
    """Apply server-side RBAC and information minimization BEFORE provider invocation."""
    from backend import main as api

    db = api.connect()
    try:
        case, version = api.load_case(db, patient_id)
        wf = api.workflow_view(case, version, role, db)
        wf_status = api.aggregate_status(db, case)
        open_excs = api.case_exceptions(db, patient_id)
        visible_excs = _visible_exceptions_for_role(open_excs, role)
        blockers = _role_filtered_blockers(db, case, visible_excs, role)

        # Information minimization: never send display_name to external/local LLMs;
        # only include synthetic clinical_summary for explicitly authorized clinical roles.
        minimized_clinical_summary = (
            api.require_patient(case, role)["clinical_summary"]
            if role in CLINICAL_SUMMARY_ROLES
            else None
        )

        return MinimizedAdvisoryContext(
            patient_id=patient_id,
            caller_role=role,
            workflow_status=wf_status,
            version=version,
            transport_required=bool(case["transport_required"]),
            blockers=blockers,
            milestones=[
                {
                    "milestone_id": m["milestone_id"],
                    "status": m["status"],
                    "locked": m["locked"],
                }
                for m in wf["milestones"]
            ],
            open_exceptions=[
                {
                    "exception_id": e["exception_id"],
                    "department": e["department"],
                    "code": e["code"],
                    "status": e["status"],
                }
                for e in visible_excs
            ],
            clinical_summary=minimized_clinical_summary,
            question=question.strip()[:1000] if question and question.strip() else None,
        )
    finally:
        db.close()


def build_deterministic_advisory(context: MinimizedAdvisoryContext) -> dict[str, Any]:
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
                f"Route {code} ({exc_id}) to the pharmacist for human review and milestone confirmation."
            )
            escalation_lines.append(
                f"[Pharmacy Escalation] Synthetic case {context.patient_id}: {code} ({exc_id}) is blocking discharge. "
                "Please verify stock/substitution and record your decision in the specialist workflow."
            )
        elif dept == "insurance":
            suggested_actions.append(
                f"Route {code} ({exc_id}) to the billing specialist for human review; automated approval is disabled."
            )
            escalation_lines.append(
                f"[Billing Escalation] Synthetic case {context.patient_id}: {code} ({exc_id}) requires billing specialist review "
                "and payer resubmission; record your decision in the specialist workflow."
            )
        elif dept == "transport":
            suggested_actions.append(
                f"Route {code} ({exc_id}) to transport staff for human confirmation of pickup availability."
            )
            escalation_lines.append(
                f"[Transport Escalation] Synthetic case {context.patient_id}: {code} ({exc_id}) requires transport confirmation."
            )
        else:
            suggested_actions.append(
                f"Authorized {dept} specialist review required for {code} ({exc_id})."
            )

    if any("denial upheld" in b.lower() for b in context.blockers):
        suggested_actions.append(
            "Billing Specialist follow-up or payer appeal is required for the upheld insurance denial; discharge remains blocked."
        )
        escalation_lines.append(
            f"[Billing Follow-Up] Synthetic case {context.patient_id}: insurance denial was upheld. "
            "Coordinate payer appeal or alternative financial clearance before discharge can proceed."
        )

    if (
        ms_by_id.get("transport", {}).get("status") == "blocked"
        and not any(e["department"] == "transport" for e in context.open_exceptions)
        and context.caller_role in {"doctor", "nurse", "admin"}
    ):
        suggested_actions.append("Contact patient transport to confirm availability.")

    if ms_by_id.get("pharmacy", {}).get("status") == "ready" and context.caller_role in {"admin", "doctor", "nurse", "pharmacist"}:
        suggested_actions.append("Pharmacist can confirm Pharmacy milestone completion.")
    if ms_by_id.get("insurance", {}).get("status") == "ready" and context.caller_role in {"admin", "doctor", "nurse", "billing_specialist"}:
        suggested_actions.append("Billing Specialist can confirm Insurance milestone completion.")
    if ms_by_id.get("patient_departed", {}).get("status") in {"ready", "in_progress"}:
        suggested_actions.append(
            "Authorized Transport Staff must confirm physical patient departure before Housekeeping dispatch."
        )
    if ms_by_id.get("housekeeping_dispatched", {}).get("status") in {"ready", "in_progress"}:
        suggested_actions.append("Housekeeping can dispatch room turnover cleaning after confirmed departure.")
    if ms_by_id.get("bed_ready", {}).get("status") in {"ready", "in_progress"}:
        suggested_actions.append("Housekeeping can mark the bed ready once turnover completes.")

    if not suggested_actions:
        if context.workflow_status == "completed":
            suggested_actions.append("All discharge and bed-turnover milestones are completed.")
        else:
            suggested_actions.append(
                "Review the current workflow milestones and proceed only with role-authorized steps."
            )

    if context.blockers:
        summary = (
            f"Synthetic case {context.patient_id} (v{context.version}) is currently '{context.workflow_status}' "
            f"with {len(context.blockers)} visible blocker(s): {', '.join(context.blockers)}. "
            "Discharge cannot advance until required human reviews and milestones are complete."
        )
    else:
        summary = (
            f"Synthetic case {context.patient_id} (v{context.version}) is currently '{context.workflow_status}' "
            "with no visible blockers for your role. Continue with the next authorized workflow step."
        )

    escalation_draft = "\n".join(escalation_lines) if escalation_lines else None
    return {
        "summary": summary,
        "blockers": list(context.blockers),
        "suggested_actions": suggested_actions,
        "escalation_draft": escalation_draft,
        "generation_mode": "deterministic_fallback",
    }


def _build_advisory_prompt(context: MinimizedAdvisoryContext) -> tuple[str, str]:
    system_prompt = (
        "You are ClearTurn's read-only hospital discharge advisory assistant. "
        "Safety rules: you CANNOT mutate workflow state, auto-approve insurance denials, "
        "alter clinical orders, auto-resolve specialist exceptions, confirm patient departure, "
        "or dispatch housekeeping before confirmed departure. "
        "Never recommend dispatching, scheduling, notifying, or starting housekeeping unless "
        "patient_departed is 'completed' and housekeeping_dispatched is unlocked; if patient departure "
        "is not completed, state that housekeeping remains locked until after patient departure is confirmed. "
        "Provide a concise discharge summary, explain only the verified operational blockers "
        "present in the provided synthetic JSON context, and draft targeted human escalations "
        "for pharmacist/billing/transport specialists as applicable. "
        "Return STRICT JSON with exact keys: summary (string), blockers (array of strings), "
        "suggested_actions (array of strings), escalation_draft (string or null)."
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


def _is_match_negated(
    sentence: str,
    match_start: int,
    prior_negated_ends: list[int],
) -> bool:
    prefix = sentence[:match_start]
    if _DIRECT_NEGATION_RE.search(prefix):
        return True
    for prev_end in reversed(prior_negated_ends):
        between = sentence[prev_end:match_start]
        if _CLAUSE_BREAK_WORD_RE.search(between):
            return False
        if _NEGATED_LIST_CONTINUATION_RE.fullmatch(between):
            return True
    return False


def _is_housekeeping_unlocked(context: MinimizedAdvisoryContext) -> bool:
    ms_by_id = {m["milestone_id"]: m for m in context.milestones}
    departed_completed = ms_by_id.get("patient_departed", {}).get("status") == "completed"
    hk_ms = ms_by_id.get("housekeeping_dispatched")
    if hk_ms is None:
        return False
    return departed_completed and not bool(hk_ms.get("locked", True))


def _contains_unsafe_housekeeping_recommendation(
    sentence: str,
    *,
    hk_unlocked: bool,
) -> bool:
    matches: list[re.Match[str]] = []
    for pattern in _HOUSEKEEPING_ACTION_PATTERNS:
        matches.extend(pattern.finditer(sentence))
    if not matches:
        return False
    matches.sort(key=lambda m: m.start())

    has_pre_departure_timing = bool(_PRE_DEPARTURE_TIMING_RE.search(sentence))
    has_early_modifier = bool(_EARLY_DISPATCH_MODIFIER_RE.search(sentence))
    has_departure_gate = bool(_DEPARTURE_GATE_RE.search(sentence))

    negated_ends: list[int] = []
    for match in matches:
        if _is_match_negated(sentence, match.start(), negated_ends):
            negated_ends.append(match.end())
            continue
        # Even if DAG is unlocked, recommending housekeeping *before* departure is unsafe.
        if has_pre_departure_timing:
            return True
        if not hk_unlocked:
            if has_early_modifier or not has_departure_gate:
                return True
    return False


def _contains_unsafe_directive(text: str, context: MinimizedAdvisoryContext | None = None) -> bool:
    hk_unlocked = _is_housekeeping_unlocked(context) if context is not None else False
    for sentence in re.split(r"[.!?\n;]+", text):
        if not sentence.strip():
            continue
        matches: list[re.Match[str]] = []
        for pattern in _UNSAFE_DIRECTIVE_PATTERNS:
            matches.extend(pattern.finditer(sentence))
        matches.sort(key=lambda m: m.start())

        negated_ends: list[int] = []
        for match in matches:
            if _is_match_negated(sentence, match.start(), negated_ends):
                negated_ends.append(match.end())
            else:
                return True

        if _contains_unsafe_housekeeping_recommendation(sentence, hk_unlocked=hk_unlocked):
            return True
    return False


class GroqProvider:
    """Prioritized Groq OpenAI-compatible adapter; enabled only when GROQ_API_KEY is set."""

    name = "groq"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")).strip()
        self.model = (model or os.getenv("BAIN_GROQ_MODEL", DEFAULT_GROQ_MODEL)).strip() or DEFAULT_GROQ_MODEL
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _configured_timeout_seconds()

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
        timeout_seconds: float | None = None,
    ) -> None:
        self.api_key = (api_key if api_key is not None else os.getenv("GEMINI_API_KEY", "")).strip()
        self.model = (model or os.getenv("BAIN_GEMINI_MODEL", DEFAULT_GEMINI_MODEL)).strip() or DEFAULT_GEMINI_MODEL
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _configured_timeout_seconds()

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


class OllamaProvider:
    """Optional local Ollama adapter; only runs when configured and reachable. Never downloads weights."""

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("BAIN_OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.model = (model or os.getenv("BAIN_OLLAMA_MODEL", "")).strip()
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else _configured_timeout_seconds()

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


def build_provider_from_env() -> AdvisoryProvider | None:
    """Select an optional LLM provider from environment variables; default is disabled (offline)."""
    selected = os.getenv("BAIN_LLM_PROVIDER", "disabled").strip().lower()
    if selected in {"", "disabled", "none", "fallback"}:
        return None
    if selected == "groq":
        return GroqProvider()
    if selected == "gemini":
        return GeminiProvider()
    if selected == "ollama":
        return OllamaProvider()
    return None


def _validate_provider_output(
    raw: dict[str, Any],
    context: MinimizedAdvisoryContext,
) -> dict[str, Any]:
    """Strictly validate LLM output, anchor blockers to DB state, and block unsafe directives."""
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

    cleaned_summary = summary.strip()[:1500]
    cleaned_actions = [a.strip()[:500] for a in suggested_actions if a.strip()]
    if not cleaned_actions:
        raise ValueError("Provider output must include at least one suggested action.")
    cleaned_draft = (
        escalation_draft.strip()[:1500]
        if isinstance(escalation_draft, str) and escalation_draft.strip()
        else None
    )

    # Reject affirmative unsafe autonomous/bypass recommendations so fallback handles them safely.
    for text_block in (cleaned_summary, *cleaned_actions, cleaned_draft or ""):
        if text_block and _contains_unsafe_directive(text_block, context):
            raise ValueError("Provider output contained an unsafe autonomous recommendation.")

    # Always anchor blockers to the actual server-side role-filtered database state so an LLM
    # can never hallucinate fake blockers or leak unauthorized blockers across roles.
    effective_blockers = list(context.blockers)

    return {
        "summary": cleaned_summary,
        "blockers": effective_blockers,
        "suggested_actions": cleaned_actions,
        "escalation_draft": cleaned_draft,
        "generation_mode": "llm",
    }


class AdvisoryService:
    """Read-only advisory facade enforcing pre-LLM RBAC minimization and safe deterministic fallback."""

    def __init__(self, *, provider: AdvisoryProvider | None = None) -> None:
        self._explicit_provider = provider

    def _resolve_provider(self) -> AdvisoryProvider | None:
        if self._explicit_provider is not None:
            return self._explicit_provider
        return build_provider_from_env()

    def analyze_case(
        self,
        *,
        patient_id: str,
        role: str,
        question: str | None = None,
    ) -> dict[str, Any]:
        """Generate a read-only advisory explanation and escalation draft."""
        from backend import main as api

        if role not in ADVISORY_ALLOWED_ROLES:
            raise api.APIError(403, "FORBIDDEN", "This role is not authorized for clinical advisory analysis.")

        context = build_minimized_context(
            patient_id=patient_id,
            role=role,
            question=question,
        )
        provider = self._resolve_provider()

        if provider is None:
            fallback = build_deterministic_advisory(context)
            api.record_activity(
                patient_id=patient_id,
                activity_type="summary",
                message=(
                    f"Generated read-only advisory analysis for synthetic case {patient_id} "
                    "using deterministic fallback (LLM provider disabled)."
                ),
                generation_mode="deterministic_fallback",
            )
            return fallback

        try:
            raw_output = provider.generate(context)
            validated = _validate_provider_output(raw_output, context)
            api.record_activity(
                patient_id=patient_id,
                activity_type="summary",
                message=(
                    f"Generated read-only advisory analysis for synthetic case {patient_id} "
                    f"via provider '{provider.name}'."
                ),
                generation_mode="llm",
            )
            return validated
        except Exception:
            fallback = build_deterministic_advisory(context)
            api.record_activity(
                patient_id=patient_id,
                activity_type="recovery",
                message=(
                    f"Advisory provider '{provider.name}' unavailable or returned invalid output "
                    f"for synthetic case {patient_id}; safely fell back to deterministic advisory."
                ),
                generation_mode="deterministic_fallback",
            )
            return fallback
