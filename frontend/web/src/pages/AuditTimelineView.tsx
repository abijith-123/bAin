import { CheckCircle2, XCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { formatUtcTimestamp } from "@/lib/utils";
import type { AuditEventItem, Role } from "@/types/api";

interface AuditTimelineViewProps {
  activeRole: Role;
  events: AuditEventItem[];
}

export function AuditTimelineView({ activeRole, events }: AuditTimelineViewProps) {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-900">
          Audit Timeline
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Durable chronological ledger of accepted and rejected operations for Patient #402
          (permission-filtered for <span className="font-mono">{activeRole}</span>).
        </p>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <div>
            <CardTitle>GET /api/patients/402/audit</CardTitle>
            <CardDescription>
              Every successful and rejected transition is recorded atomically by FastAPI
            </CardDescription>
          </div>
          <Badge variant="navy">{events.length} event(s)</Badge>
        </CardHeader>
        <CardContent className="p-0">
          {events.length === 0 ? (
            <div className="px-5 py-8 text-center text-xs text-slate-500">
              No audit events visible yet for this role. Attempt a milestone transition or
              exception resolution to observe durable audit entries.
            </div>
          ) : (
            <div className="divide-y divide-slate-100">
              {events.map((ev) => {
                const isSuccess = ev.outcome === "success";
                return (
                  <div
                    key={ev.event_id}
                    className="flex flex-wrap items-start justify-between gap-4 px-5 py-3.5 text-xs hover:bg-slate-50/60"
                  >
                    <div className="flex items-start gap-3">
                      {isSuccess ? (
                        <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                      ) : (
                        <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-rose-600" />
                      )}
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-mono font-semibold text-slate-900">
                            {ev.action}
                          </span>
                          <Badge variant={isSuccess ? "success" : "danger"}>
                            {ev.outcome}
                          </Badge>
                          <Badge variant="muted">{ev.actor_role}</Badge>
                        </div>
                        {ev.reason && (
                          <p className="text-slate-600 leading-relaxed">{ev.reason}</p>
                        )}
                      </div>
                    </div>

                    <div className="flex flex-col items-end gap-0.5 font-mono text-[11px] text-slate-500">
                      <span>v{ev.version}</span>
                      <span>{formatUtcTimestamp(ev.occurred_at)}</span>
                      <span className="text-slate-400">{ev.event_id}</span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
