import { useEffect, useState } from "react";
import {
  CheckCircle2,
  Lock,
  Send,
  ShieldOff,
  Truck,
} from "lucide-react";
import { DischargeDag } from "@/components/dag/DischargeDag";
import { ApiErrorBanner } from "@/components/shared/ApiErrorBanner";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, clearTurnApi } from "@/lib/api";
import { getRoleProfile } from "@/lib/roles";
import { formatUtcTimestamp, generateIdempotencyKey } from "@/lib/utils";
import type {
  PatientDetail,
  Role,
  WorkflowResponse,
  WorkflowStatus,
} from "@/types/api";

interface PatientDetailViewProps {
  activeRole: Role;
  token: string;
  detail: PatientDetail | null;
  workflow: WorkflowResponse | null;
  onRefresh: () => Promise<void>;
}

const TARGET_STATUS_OPTIONS: WorkflowStatus[] = [
  "in_progress",
  "completed",
];

export function PatientDetailView({
  activeRole,
  token,
  detail,
  workflow,
  onRefresh,
}: PatientDetailViewProps) {
  const [selectedMilestoneId, setSelectedMilestoneId] = useState<string>("pharmacy");
  const [targetStatus, setTargetStatus] = useState<WorkflowStatus>("completed");
  const [reason, setReason] = useState<string>("");
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [confirmationMsg, setConfirmationMsg] = useState<string | null>(null);

  const roleProfile = getRoleProfile(activeRole);
  const selectedMilestone = workflow?.milestones.find(
    (m) => m.milestone_id === selectedMilestoneId,
  );

  useEffect(() => {
    if (!selectedMilestone) return;
    setTargetStatus(
      selectedMilestone.status === "ready" || selectedMilestone.status === "in_progress"
        ? "completed"
        : "in_progress",
    );
  }, [selectedMilestoneId, selectedMilestone?.status]);

  async function handleSubmitTransition(e: React.FormEvent) {
    e.preventDefault();
    if (!workflow || !selectedMilestone) return;

    setSubmitting(true);
    setActionError(null);
    setConfirmationMsg(null);

    try {
      const idemKey = generateIdempotencyKey(`ms-${selectedMilestone.milestone_id}`);
      const res = await clearTurnApi.updateMilestone(
        token,
        workflow.patient_id,
        {
          milestone_id: selectedMilestone.milestone_id,
          target_status: targetStatus,
          expected_version: workflow.version,
          reason: reason.trim(),
        },
        idemKey,
      );
      setConfirmationMsg(
        `Server confirmed ${res.milestone_id} → ${res.status} (v${res.version}, audit ${res.audit_event_id}).`,
      );
      setReason("");
      if (targetStatus === "in_progress") setTargetStatus("completed");
      await onRefresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setActionError(err);
      } else {
        setActionError(
          new ApiError(0, "INTERNAL_ERROR", "Unexpected error submitting transition."),
        );
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-3">
            <h1 className="text-xl font-semibold tracking-tight text-slate-900">
              Patient #{detail?.patient_id ?? "402"}
            </h1>
            {detail && <StatusBadge status={detail.workflow_status} />}
            {workflow && (
              <span className="rounded bg-slate-200/70 px-2 py-0.5 font-mono text-xs text-slate-700">
                v{workflow.version}
              </span>
            )}
          </div>
          <p className="mt-1 text-sm text-slate-500">
            {detail?.display_name ??
              (activeRole === "housekeeping" || activeRole === "transport_staff"
                ? "Patient name redacted for current role"
                : "Name not included in synthetic data")} ·
            Clinical clearance: {formatUtcTimestamp(detail?.clinical_clearance_at)}
          </p>
        </div>

        {detail?.transport_required && (
          <Badge variant="navy">
            <Truck className="h-3.5 w-3.5" />
            <span>Hospital Transport Required</span>
          </Badge>
        )}
      </div>

      {/* Role-scoped Clinical Summary + Department Status */}
      <div className="grid gap-6 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Clinical Handoff Context</CardTitle>
            <CardDescription>
              Field-level PHI redaction enforced server-side for role:{" "}
              <span className="font-mono">{activeRole}</span>
            </CardDescription>
          </CardHeader>
          <CardContent>
            {detail?.clinical_summary ? (
              <p className="text-sm leading-relaxed text-slate-800">
                {detail.clinical_summary}
              </p>
            ) : (
              <div className="flex items-start gap-2.5 rounded-md border border-slate-200 bg-slate-50 px-3.5 py-3 text-xs text-slate-600">
                <ShieldOff className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
                <div>
                  <div className="font-semibold text-slate-700">
                    Clinical PHI Redacted by Backend Policy
                  </div>
                  <p className="mt-0.5">
                    Role <span className="font-mono">{roleProfile.role}</span> (
                    {roleProfile.scopeLabel}) is not authorized to view clinical notes or
                    diagnoses.
                  </p>
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Department Readiness</CardTitle>
            <CardDescription>Visible departments for {roleProfile.shortLabel}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2.5">
            {detail?.departments && Object.keys(detail.departments).length > 0 ? (
              Object.entries(detail.departments).map(([dept, st]) => (
                <div
                  key={dept}
                  className="flex items-center justify-between rounded-md border border-slate-100 bg-slate-50/70 px-3 py-2 text-xs"
                >
                  <span className="font-medium capitalize text-slate-700">{dept}</span>
                  <StatusBadge status={st} />
                </div>
              ))
            ) : (
              <p className="text-xs text-slate-500">No department data loaded.</p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Interactive DAG */}
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <div>
            <CardTitle>Deterministic Discharge Dependency Graph</CardTitle>
            <CardDescription>
              Click any milestone node to inspect prerequisites or submit a backend-validated transition
            </CardDescription>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-xs">
            {(workflow?.blockers ?? []).map((b) => (
              <Badge key={b} variant="danger">
                {b}
              </Badge>
            ))}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {workflow ? (
            <DischargeDag
              milestones={workflow.milestones}
              selectedMilestoneId={selectedMilestoneId}
              onSelectMilestone={(id) => {
                setSelectedMilestoneId(id);
                const nextMilestone = workflow?.milestones.find((item) => item.milestone_id === id);
                setTargetStatus(
                  nextMilestone?.status === "ready" || nextMilestone?.status === "in_progress"
                    ? "completed"
                    : "in_progress",
                );
                setReason("");
                setActionError(null);
                setConfirmationMsg(null);
              }}
            />
          ) : (
            <div className="flex h-[240px] items-center justify-center rounded-lg border border-dashed border-slate-300 text-xs text-slate-500">
              Loading workflow DAG from FastAPI…
            </div>
          )}

          {/* Action Panel */}
          {selectedMilestone && workflow && (
            <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-200/80 pb-3">
                <div className="flex items-center gap-2.5">
                  <span className="text-sm font-semibold text-slate-900">
                    {selectedMilestone.label}
                  </span>
                  <StatusBadge status={selectedMilestone.status} />
                  {selectedMilestone.locked && (
                    <Badge variant="muted">
                      <Lock className="h-3 w-3" />
                      <span>Locked</span>
                    </Badge>
                  )}
                </div>
                <span className="font-mono text-xs text-slate-500">
                  expected_version: {workflow.version}
                </span>
              </div>

              {actionError && (
                <div className="mt-3">
                  <ApiErrorBanner
                    error={actionError}
                    onRefresh={async () => {
                      setActionError(null);
                      await onRefresh();
                    }}
                    onDismiss={() => setActionError(null)}
                  />
                </div>
              )}

              {confirmationMsg && (
                <div className="mt-3 flex items-center gap-2 rounded-md border border-emerald-200 bg-emerald-50 px-3.5 py-2.5 text-xs text-emerald-900">
                  <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
                  <span>{confirmationMsg}</span>
                </div>
              )}

              <form
                onSubmit={handleSubmitTransition}
                className="mt-4 grid gap-3 md:grid-cols-12"
              >
                <div className="md:col-span-3">
                  <label className="block text-[11px] font-medium text-slate-600">
                    Target Status
                  </label>
                  <select
                    value={targetStatus}
                    onChange={(e) => setTargetStatus(e.target.value as WorkflowStatus)}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-900 focus:border-[#0f1d36] focus:outline-none"
                  >
                    {TARGET_STATUS_OPTIONS.map((st) => (
                      <option key={st} value={st}>
                        {st}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="md:col-span-6">
                  <label className="block text-[11px] font-medium text-slate-600">
                    Recorded Reason / Evidence (min 3 chars)
                  </label>
                  <input
                    type="text"
                    required
                    minLength={3}
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                    placeholder={`Evidence for ${selectedMilestone.milestone_id} transition...`}
                    className="mt-1 h-9 w-full rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-900 placeholder:text-slate-400 focus:border-[#0f1d36] focus:outline-none"
                  />
                </div>

                <div className="flex items-end md:col-span-3">
                  <Button
                    type="submit"
                    className="w-full"
                    disabled={
                      submitting || reason.trim().length < 3 || selectedMilestone.locked ||
                      selectedMilestone.status === "blocked" ||
                      selectedMilestone.status === "completed"
                    }
                  >
                    <Send className="h-3.5 w-3.5" />
                    <span>{submitting ? "Submitting…" : "Submit to Backend"}</span>
                  </Button>
                </div>
              </form>

              {selectedMilestone.locked && (
                <p className="mt-2.5 text-[11px] text-amber-700">
                  This milestone is locked until its required dependencies are complete.
                </p>
              )}
              {selectedMilestone.status === "blocked" && (
                <p className="mt-2.5 text-[11px] text-rose-700">
                  Resolve the assigned specialist exception before advancing this milestone.
                </p>
              )}
              {selectedMilestone.status === "completed" && (
                <p className="mt-2.5 text-[11px] text-emerald-700">
                  This milestone is already completed.
                </p>
              )}

              <p className="mt-2.5 text-[11px] text-slate-500">
                Transitions are never marked complete client-side. FastAPI validates role permissions,
                DAG prerequisites, optimistic version, and idempotency before committing.
              </p>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
