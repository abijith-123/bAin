import { useState } from "react";
import { Bot, Info, ShieldCheck, Sparkles } from "lucide-react";
import { ApiErrorBanner } from "@/components/shared/ApiErrorBanner";
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
import type {
  AgentAnalyzeResponse,
  Role,
  WorkflowResponse,
} from "@/types/api";

interface AiAssistantViewProps {
  activeRole: Role;
  token: string;
  workflow: WorkflowResponse | null;
}

export function AiAssistantView({
  activeRole,
  token,
  workflow,
}: AiAssistantViewProps) {
  const [wait, setWait] = useState<{answer: string; version: number} | null>(null);
  const [note, setNote] = useState("");
  const [question, setQuestion] = useState(
    "Summarize current blockers and draft targeted specialist escalations.",
  );
  const [loading, setLoading] = useState(false);
  const [analysis, setAnalysis] = useState<AgentAnalyzeResponse | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  const roleProfile = getRoleProfile(activeRole);
  const isOperationalOnly = roleProfile.phiPolicy === "operational_only";

  async function handleAnalyze(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError(null);
    try {
      const res = await clearTurnApi.analyzeCase(token, {
        patient_id: workflow?.patient_id ?? "402",
        handoff_note: note || undefined,
        question: question.trim() || undefined,
      });
      setAnalysis(res);
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err);
      } else {
        setError(
          new ApiError(0, "INTERNAL_ERROR", "Unable to request advisory analysis."),
        );
      }
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight text-slate-900">
            Advisory AI Assistant
          </h1>
          <p className="mt-1 text-sm text-slate-500">
            Read-only bottleneck explanation and escalation drafting. AI output cannot mutate
            workflow state or override clinical/insurance gates.
          </p>
        </div>
        <Badge variant="navy">
          <ShieldCheck className="h-3.5 w-3.5" />
          <span>Advisory Only · Zero Write Access</span>
        </Badge>
      </div>

      {["nurse", "housekeeping", "transport_staff"].includes(activeRole) && workflow && (
        <Card><CardContent className="space-y-3 py-4">
          <Button onClick={async () => {
            try {
              setWait(await clearTurnApi.waitReason(token, workflow.patient_id));
            } catch {
              setWait({answer: "Unable to retrieve the current wait reason. Try again.", version: workflow.version});
            }
          }}>Why is this patient waiting?</Button>
          {wait && wait.version >= workflow.version && <p aria-live="polite">{wait.answer} · v{wait.version}</p>}
        </CardContent></Card>
      )}
      {isOperationalOnly ? (
        <Card>
          <CardContent className="flex items-start gap-3 py-6 text-xs text-slate-600">
            <Info className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
            <div>
              <div className="font-semibold text-slate-800">
                Clinical Advisory Restricted for {roleProfile.label}
              </div>
              <p className="mt-1 leading-relaxed">
                Operational roles ({activeRole}) only receive room/departure readiness telemetry and
                do not access clinical summaries or specialist escalation drafts.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Request Case Analysis (POST /api/agent/analyze)</CardTitle>
              <CardDescription>
                Gracefully remains usable when the LLM service is offline or in Phase 2 rollout
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
              <form onSubmit={handleAnalyze} className="flex flex-col gap-3 sm:flex-row">
                <input
                  type="text"
                  value={question}
                  onChange={(e) => setQuestion(e.target.value)}
                  placeholder="Ask for a role-filtered blocker summary or escalation draft..."
                  className="h-9 flex-1 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-900 focus:border-[#0f1d36] focus:outline-none"
                />
                {(activeRole === "doctor" || activeRole === "nurse") &&
                  <label>Synthetic handoff note
                    <textarea value={note} maxLength={2000} onChange={e => setNote(e.target.value)} placeholder="Quoted source material for a read-only summary"/>
                  </label>}
                <Button type="submit" disabled={loading}>
                  <Sparkles className="h-3.5 w-3.5" />
                  <span>{loading ? "Analyzing…" : "Run Advisory Analysis"}</span>
                </Button>
              </form>

              {error && (
                <ApiErrorBanner error={error} onDismiss={() => setError(null)} />
              )}

              {analysis ? (
                <div className="space-y-4 rounded-lg border border-slate-200 bg-slate-50/70 p-4 text-xs">
                  <div className="flex items-center justify-between">
                    <span className="font-semibold text-slate-900">Advisory Summary</span>
                    <Badge
                      variant={
                        analysis.generation_mode === "llm" ? "info" : "warning"
                      }
                    >
                      {analysis.generation_mode}
                    </Badge>
                  </div>
                  <p className="leading-relaxed text-slate-700">{analysis.summary}</p>
                  {analysis.note_status !== "absent" && <p>Handoff note: {analysis.note_status}</p>}
                  {analysis.escalation_draft && (
                    <div className="rounded-md border border-slate-200 bg-white p-3">
                      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                        Escalation Draft
                      </div>
                      <p className="whitespace-pre-wrap text-slate-700">
                        {analysis.escalation_draft}
                      </p>
                    </div>
                  )}
                </div>
              ) : (
                <div className="rounded-lg border border-slate-200 bg-slate-50/70 p-4 text-xs text-slate-600">
                  <div className="flex items-center gap-2 font-semibold text-slate-800">
                    <Bot className="h-4 w-4 text-slate-500" />
                    <span>Deterministic Backend Telemetry Snapshot</span>
                  </div>
                  <p className="mt-1.5 leading-relaxed">
                    Patient #{workflow?.patient_id ?? "402"} (v{workflow?.version ?? 1}) currently
                    reports{" "}
                    <span className="font-semibold text-slate-900">
                      {workflow?.blockers.length ?? 0} active blocker(s)
                    </span>
                    {workflow?.blockers.length
                      ? `: ${workflow.blockers.join("; ")}.`
                      : "."}
                  </p>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
