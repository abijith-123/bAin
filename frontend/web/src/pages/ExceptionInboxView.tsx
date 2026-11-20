import { useState } from "react";
import { CheckCircle2, ShieldAlert } from "lucide-react";
import { ApiErrorBanner } from "@/components/shared/ApiErrorBanner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
} from "@/components/ui/card";
import { ApiError, clearTurnApi } from "@/lib/api";
import { getRoleProfile } from "@/lib/roles";
import { generateIdempotencyKey } from "@/lib/utils";
import type { ExceptionItem, Role } from "@/types/api";

interface ExceptionInboxViewProps {
  activeRole: Role;
  token: string;
  exceptions: ExceptionItem[];
  onRefresh: () => Promise<void>;
}

export function ExceptionInboxView({
  activeRole,
  token,
  exceptions,
  onRefresh,
}: ExceptionInboxViewProps) {
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [submittingId, setSubmittingId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<ApiError | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);

  const roleProfile = getRoleProfile(activeRole);

  async function handleResolve(exc: ExceptionItem) {
    const text = (resolutions[exc.exception_id] ?? "").trim();
    if (text.length < 3) return;

    setSubmittingId(exc.exception_id);
    setActionError(null);
    setSuccessMsg(null);

    try {
      const idemKey = generateIdempotencyKey(`exc-${exc.exception_id}`);
      const updated = await clearTurnApi.resolveException(
        token,
        exc.exception_id,
        {
          resolution: text,
          expected_version: exc.version,
        },
        idemKey,
      );
      setSuccessMsg(
        `Resolved ${updated.exception_id} (v${updated.version}). Linked milestone moved from blocked → ready; separate completion confirmation is still required.`,
      );
      setResolutions((prev) => ({ ...prev, [exc.exception_id]: "" }));
      await onRefresh();
    } catch (err) {
      if (err instanceof ApiError) {
        setActionError(err);
      } else {
        setActionError(
          new ApiError(0, "INTERNAL_ERROR", "Unexpected error resolving exception."),
        );
      }
    } finally {
      setSubmittingId(null);
    }
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-900">
          Specialist Exception Inbox
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Human-in-the-loop resolution queue for{" "}
          <span className="font-medium text-slate-700">{roleProfile.label}</span>.
          Resolving an exception unblocks its milestone to <span className="font-mono">ready</span>{" "}
          without auto-completing it.
        </p>
      </div>

      {actionError && (
        <ApiErrorBanner
          error={actionError}
          onRefresh={async () => {
            setActionError(null);
            await onRefresh();
          }}
          onDismiss={() => setActionError(null)}
        />
      )}

      {successMsg && (
        <div className="flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-xs text-emerald-900">
          <CheckCircle2 className="h-4 w-4 shrink-0 text-emerald-600" />
          <span>{successMsg}</span>
        </div>
      )}

      {exceptions.length === 0 ? (
        <Card>
          <CardContent className="flex items-center gap-3 py-8 text-sm text-slate-600">
            <ShieldAlert className="h-5 w-5 text-slate-400" />
            <span>
              No open exceptions are assigned to role <span className="font-mono">{activeRole}</span>.
              Pharmacy and insurance reviews are routed to their assigned specialists; administrators
              can inspect all open exceptions in read-only mode.
            </span>
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4">
          {exceptions.map((exc) => {
            const draft = resolutions[exc.exception_id] ?? "";
            const isOpen = exc.status === "open";
            const canResolve =
              (activeRole === "pharmacist" && exc.department === "pharmacy") ||
              (activeRole === "billing_specialist" && exc.department === "insurance") ||
              (activeRole === "transport_staff" && exc.department === "transport");
            return (
              <Card key={exc.exception_id}>
                <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
                  <div className="flex items-center gap-2.5">
                    <span className="font-mono text-sm font-semibold text-slate-900">
                      {exc.code}
                    </span>
                    <Badge variant={isOpen ? "danger" : "success"}>{exc.status}</Badge>
                    <Badge variant="navy">{exc.department}</Badge>
                  </div>
                  <div className="flex items-center gap-3 font-mono text-xs text-slate-500">
                    <span>{exc.exception_id}</span>
                    <span>Patient #{exc.patient_id}</span>
                    <span>v{exc.version}</span>
                  </div>
                </CardHeader>
                <CardContent className="space-y-4">
                  <p className="text-sm text-slate-700">
                    {exc.summary ?? "Specialist review required."}
                  </p>

                  {isOpen && canResolve ? (
                    <div className="flex flex-col gap-2.5 sm:flex-row">
                      <input
                        type="text"
                        value={draft}
                        onChange={(e) =>
                          setResolutions((prev) => ({
                            ...prev,
                            [exc.exception_id]: e.target.value,
                          }))
                        }
                        placeholder={`Enter specialist resolution for ${exc.code} (min 3 chars)...`}
                        className="h-9 flex-1 rounded-md border border-slate-300 bg-white px-3 text-xs text-slate-900 placeholder:text-slate-400 focus:border-[#0f1d36] focus:outline-none"
                      />
                      <Button
                        size="default"
                        disabled={submittingId === exc.exception_id || draft.trim().length < 3}
                        onClick={() => handleResolve(exc)}
                      >
                        {submittingId === exc.exception_id
                          ? "Resolving…"
                          : "Resolve Exception"}
                      </Button>
                    </div>
                  ) : isOpen ? (
                    <CardDescription>
                      Read-only review. Only the assigned {exc.department} specialist can resolve
                      this exception.
                    </CardDescription>
                  ) : (
                    <CardDescription>
                      Exception resolved and audited. Confirm the linked milestone in Patient #{exc.patient_id}
                      DAG to advance the discharge pipeline.
                    </CardDescription>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}
