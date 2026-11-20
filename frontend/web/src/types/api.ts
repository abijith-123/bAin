/**
 * TypeScript contract types matching shared/api_contract.yaml (v1.0.0).
 * FastAPI is the sole authority for permissions, workflow state, and redaction.
 */

export type WorkflowStatus =
  | "pending"
  | "ready"
  | "in_progress"
  | "blocked"
  | "completed";

export type Role =
  | "admin"
  | "doctor"
  | "nurse"
  | "pharmacist"
  | "billing_specialist"
  | "transport_staff"
  | "housekeeping";

export type Department =
  | "pharmacy"
  | "insurance"
  | "transport"
  | "housekeeping";

export type ExceptionStatus = "open" | "resolved";

export type AuditOutcome = "success" | "rejected";

export type GenerationMode = "llm" | "deterministic_fallback";

export type ErrorCode =
  | "UNAUTHENTICATED"
  | "FORBIDDEN"
  | "NOT_FOUND"
  | "STALE_VERSION"
  | "INVALID_TRANSITION"
  | "IDEMPOTENCY_CONFLICT"
  | "VALIDATION_ERROR"
  | "INTERNAL_ERROR"
  | "NETWORK_ERROR";

export interface HealthResponse {
  status: "ok";
  service: string;
}

export interface DashboardSummary {
  generated_at: string;
  active_cases: number;
  blocked_cases: number;
  completed_today: number;
  available_beds: number;
  pending_specialist_reviews: number;
}

export interface DepartmentStatus {
  department: Department;
  status: WorkflowStatus;
  open_cases: number;
  blocked_cases: number;
}

export interface PatientSummary {
  patient_id: string;
  display_name: string | null;
  workflow_status: WorkflowStatus;
  version: number;
  blockers: string[];
  clinical_clearance_at: string | null;
}

export interface PatientDetail extends PatientSummary {
  departments?: Record<string, WorkflowStatus>;
  clinical_summary: string | null;
  transport_required?: boolean;
}

export interface MilestoneItem {
  milestone_id: string;
  label: string;
  status: WorkflowStatus;
  dependencies: string[];
  locked?: boolean;
}

export interface WorkflowResponse {
  patient_id: string;
  version: number;
  milestones: MilestoneItem[];
  transport_required?: boolean;
  blockers: string[];
}

export interface MilestoneUpdateRequest {
  milestone_id: string;
  target_status: WorkflowStatus;
  expected_version: number;
  reason: string;
}

export interface MilestoneUpdateResponse {
  patient_id: string;
  milestone_id: string;
  status: WorkflowStatus;
  version: number;
  audit_event_id: string;
}

export interface ExceptionItem {
  exception_id: string;
  patient_id: string;
  department: Department;
  code: string;
  status: ExceptionStatus;
  summary?: string;
  version: number;
}

export interface ExceptionResolveRequest {
  resolution: string;
  expected_version: number;
}

export interface AuditEventItem {
  event_id: string;
  occurred_at: string;
  actor_id?: string;
  actor_role: Role;
  patient_id?: string;
  action: string;
  outcome: AuditOutcome;
  reason: string | null;
  version: number;
  previous_state: string | null;
  new_state: string | null;
}

export interface AgentActivityItem {
  activity_id: string;
  occurred_at: string;
  patient_id: string | null;
  activity_type:
    | "tool_call"
    | "bottleneck"
    | "summary"
    | "escalation"
    | "error"
    | "recovery";
  message: string;
  generation_mode: GenerationMode | null;
}

export interface AgentAnalyzeRequest {
  patient_id: string;
  question?: string;
}

export interface AgentAnalyzeResponse {
  summary: string;
  blockers: string[];
  suggested_actions: string[];
  escalation_draft: string | null;
  generation_mode: GenerationMode;
}

export interface ErrorEnvelope {
  error: string;
  code: ErrorCode;
  message: string;
  details?: Record<string, unknown>;
}
