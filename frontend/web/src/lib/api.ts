import type {
  AgentActivityItem,
  AgentAnalyzeRequest,
  AgentAnalyzeResponse,
  AuditEventItem,
  DashboardSummary,
  DepartmentStatus,
  ErrorCode,
  ErrorEnvelope,
  ExceptionItem,
  ExceptionResolveRequest,
  ExceptionResolveResponse,
  ExceptionStatus,
  HealthResponse,
  MilestoneUpdateRequest,
  MilestoneUpdateResponse,
  PatientDetail,
  PatientSummary,
  WorkflowResponse,
  WorkflowStatus,
} from "@/types/api";

/**
 * In Vite dev mode, requests to `/api/...` are proxied to FastAPI via vite.config.ts.
 * In production builds, VITE_API_BASE_URL may point to a local FastAPI host (never secrets).
 */
const API_BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

export class ApiError extends Error {
  readonly status: number;
  readonly code: ErrorCode;
  readonly details?: Record<string, unknown>;

  constructor(
    status: number,
    code: ErrorCode,
    message: string,
    details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function resolveUrl(path: string): string {
  // In Vite dev server, use relative /api path to leverage dev proxy and avoid CORS issues.
  if (import.meta.env.DEV) {
    return path;
  }
  return API_BASE ? `${API_BASE}${path}` : path;
}

async function requestJson<T>(
  path: string,
  options: {
    method?: "GET" | "POST";
    token?: string;
    idempotencyKey?: string;
    body?: unknown;
    signal?: AbortSignal;
  } = {},
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  if (options.token) {
    headers.Authorization = `Bearer ${options.token}`;
  }
  if (options.idempotencyKey) {
    headers["Idempotency-Key"] = options.idempotencyKey;
  }
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  let response: Response;
  try {
    response = await fetch(resolveUrl(path), {
      method: options.method ?? "GET",
      headers,
      body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
      signal: options.signal,
    });
  } catch (err) {
    const message =
      err instanceof Error
        ? `Unable to reach bAin API (${err.message}). Verify FastAPI is running.`
        : "Unable to reach bAin API.";
    throw new ApiError(0, "NETWORK_ERROR", message);
  }

  const text = await response.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!response.ok) {
    const env = parsed as Partial<ErrorEnvelope> | null;
    const code: ErrorCode = env?.code ?? (response.status === 404 ? "NOT_FOUND" : "INTERNAL_ERROR");
    const message =
      env?.message ??
      (typeof (parsed as { detail?: string } | null)?.detail === "string"
        ? (parsed as { detail: string }).detail
        : `Request failed with HTTP ${response.status}.`);
    throw new ApiError(response.status, code, message, env?.details);
  }

  return parsed as T;
}

export const clearTurnApi = {
  getClock(token: string) {
    return requestJson<{clock_version: number}>("/api/demo/clock", {token});
  },
  advanceClock(token: string, version: number, minutes: number, key: string) {
    return requestJson<{reassigned_exceptions: {exception_id:string}[]}>("/api/demo/clock/advance", {
      token, method: "POST", idempotencyKey: key,
      body: {advance_minutes: minutes, expected_clock_version: version},
    });
  },
  getHealth(signal?: AbortSignal): Promise<HealthResponse> {
    return requestJson<HealthResponse>("/api/health", { signal });
  },

  getDashboardSummary(token: string, signal?: AbortSignal): Promise<DashboardSummary> {
    return requestJson<DashboardSummary>("/api/dashboard/summary", { token, signal });
  },

  getDepartmentsStatus(token: string, signal?: AbortSignal): Promise<{ items: DepartmentStatus[] }> {
    return requestJson<{ items: DepartmentStatus[] }>("/api/departments/status", { token, signal });
  },

  listPatients(
    token: string,
    status?: WorkflowStatus,
    signal?: AbortSignal,
  ): Promise<{ items: PatientSummary[] }> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return requestJson<{ items: PatientSummary[] }>(`/api/patients${qs}`, { token, signal });
  },

  getPatientDetail(token: string, patientId: string, signal?: AbortSignal): Promise<PatientDetail> {
    return requestJson<PatientDetail>(`/api/patients/${encodeURIComponent(patientId)}`, {
      token,
      signal,
    });
  },

  getPatientWorkflow(
    token: string,
    patientId: string,
    signal?: AbortSignal,
  ): Promise<WorkflowResponse> {
    return requestJson<WorkflowResponse>(
      `/api/patients/${encodeURIComponent(patientId)}/workflow`,
      { token, signal },
    );
  },

  updateMilestone(
    token: string,
    patientId: string,
    payload: MilestoneUpdateRequest,
    idempotencyKey: string,
  ): Promise<MilestoneUpdateResponse> {
    return requestJson<MilestoneUpdateResponse>(
      `/api/patients/${encodeURIComponent(patientId)}/milestones`,
      {
        method: "POST",
        token,
        idempotencyKey,
        body: payload,
      },
    );
  },

  getPatientAudit(
    token: string,
    patientId: string,
    signal?: AbortSignal,
  ): Promise<{ items: AuditEventItem[] }> {
    return requestJson<{ items: AuditEventItem[] }>(
      `/api/patients/${encodeURIComponent(patientId)}/audit`,
      { token, signal },
    );
  },

  listExceptions(
    token: string,
    status?: ExceptionStatus,
    signal?: AbortSignal,
  ): Promise<{ items: ExceptionItem[] }> {
    const qs = status ? `?status=${encodeURIComponent(status)}` : "";
    return requestJson<{ items: ExceptionItem[] }>(`/api/exceptions${qs}`, { token, signal });
  },

  resolveException(
    token: string,
    exceptionId: string,
    payload: ExceptionResolveRequest,
    idempotencyKey: string,
  ): Promise<ExceptionResolveResponse> {
    return requestJson<ExceptionResolveResponse>(
      `/api/exceptions/${encodeURIComponent(exceptionId)}/resolve`,
      {
        method: "POST",
        token,
        idempotencyKey,
        body: payload,
      },
    );
  },

  getAgentActivity(
    token: string,
    signal?: AbortSignal,
  ): Promise<{ items: AgentActivityItem[] }> {
    return requestJson<{ items: AgentActivityItem[] }>("/api/agent/activity", { token, signal });
  },

  analyzeCase(token: string, payload: AgentAnalyzeRequest): Promise<AgentAnalyzeResponse> {
    return requestJson<AgentAnalyzeResponse>("/api/agent/analyze", {
      method: "POST",
      token,
      body: payload,
    });
  },
};
