import {
  AlertOctagon,
  ArrowRight,
  BedDouble,
  CheckCircle2,
  ClipboardList,
  ShieldAlert,
} from "lucide-react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
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
import { formatUtcTimestamp } from "@/lib/utils";
import type {
  DashboardSummary,
  ExceptionItem,
  PatientSummary,
  WorkflowResponse,
} from "@/types/api";

interface OperationsOverviewProps {
  summary: DashboardSummary | null;
  patients: PatientSummary[];
  exceptions: ExceptionItem[];
  workflow: WorkflowResponse | null;
  patientId: string;
  onOpenPatient: (patientId: string) => void;
  onOpenExceptions: () => void;
}

export function OperationsOverview({
  summary,
  patients,
  exceptions,
  workflow,
  patientId,
  onOpenPatient,
  onOpenExceptions,
}: OperationsOverviewProps) {
  const openExceptions = exceptions.filter((exception) => exception.status === "open");

  const milestoneProgressData = (workflow?.milestones ?? []).map((m) => ({
    name: m.label.replace(" authorization", "").replace(" dispatched", ""),
    completed: m.status === "completed" ? 1 : 0,
    ready: m.status === "ready" || m.status === "in_progress" ? 1 : 0,
    blocked: m.status === "blocked" ? 1 : 0,
    pending: m.status === "pending" ? 1 : 0,
  }));

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-slate-900">
          Operations Overview
        </h1>
        <p className="mt-1 text-sm text-slate-500">
          Live synthetic hospital discharge queue and deterministic readiness telemetry.
        </p>
      </div>

      {/* KPI Strip */}
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <p className="text-xs font-medium text-slate-500">Active Discharges</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-slate-900">
                {summary?.active_cases ?? "—"}
              </p>
            </div>
            <div className="rounded-md bg-slate-100 p-2.5 text-slate-700">
              <ClipboardList className="h-4 w-4" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <p className="text-xs font-medium text-slate-500">Blocked Cases</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-rose-700">
                {summary?.blocked_cases ?? "—"}
              </p>
            </div>
            <div className="rounded-md bg-rose-50 p-2.5 text-rose-700">
              <AlertOctagon className="h-4 w-4" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <p className="text-xs font-medium text-slate-500">Open Reviews (All Departments)</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-amber-700">
                {summary?.pending_specialist_reviews ?? "—"}
              </p>
            </div>
            <div className="rounded-md bg-amber-50 p-2.5 text-amber-700">
              <ShieldAlert className="h-4 w-4" />
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardContent className="flex items-center justify-between p-4">
            <div>
              <p className="text-xs font-medium text-slate-500">Beds Available</p>
              <p className="mt-1 text-2xl font-semibold tabular-nums text-emerald-700">
                {summary?.available_beds ?? "—"}
              </p>
            </div>
            <div className="rounded-md bg-emerald-50 p-2.5 text-emerald-700">
              <BedDouble className="h-4 w-4" />
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-6">
        {/* Discharge Queue */}
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>Synthetic Discharge Queue</CardTitle>
              <CardDescription>
                Role-filtered case summaries returned by GET /api/patients
              </CardDescription>
            </div>
            <Badge variant="navy">{patients.length} case(s)</Badge>
          </CardHeader>
          <CardContent className="p-0">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[860px] table-fixed text-left text-sm">
                <thead className="border-b border-slate-100 bg-slate-50/70 text-[11px] font-semibold uppercase tracking-wider text-slate-500">
                  <tr>
                    <th className="w-[17%] px-5 py-3">Case</th>
                    <th className="w-[15%] px-4 py-3">Status</th>
                    <th className="w-[30%] px-4 py-3">Active Blockers</th>
                    <th className="w-[9%] px-4 py-3">Version</th>
                    <th className="w-[17%] px-4 py-3">Clearance</th>
                    <th className="w-[12%] px-5 py-3 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {patients.map((p) => (
                    <tr key={p.patient_id} className="hover:bg-slate-50/60">
                      <td className="px-5 py-3.5">
                        <div className="whitespace-nowrap font-semibold text-slate-900">
                          Patient #{p.patient_id}
                        </div>
                        <div className="text-xs text-slate-500">
                          {p.display_name ?? "Name not included in synthetic data"}
                        </div>
                      </td>
                      <td className="px-4 py-3.5">
                        <StatusBadge status={p.workflow_status} />
                      </td>
                      <td className="px-4 py-3.5">
                        {p.blockers.length > 0 ? (
                          <div className="flex flex-wrap gap-1">
                            {p.blockers.map((b) => (
                              <Badge key={b} variant="danger">
                                {b}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="inline-flex items-center gap-1 text-xs text-emerald-700">
                            <CheckCircle2 className="h-3.5 w-3.5" />
                            None
                          </span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3.5 font-mono text-xs text-slate-600">
                        v{p.version}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3.5 text-xs text-slate-500">
                        {formatUtcTimestamp(p.clinical_clearance_at)}
                      </td>
                      <td className="whitespace-nowrap px-5 py-3.5 text-right">
                        <Button
                          size="sm"
                          variant="outline"
                          onClick={() => onOpenPatient(p.patient_id)}
                        >
                          Inspect
                          <ArrowRight className="h-3.5 w-3.5" />
                        </Button>
                      </td>
                    </tr>
                  ))}
                  {patients.length === 0 && (
                    <tr>
                      <td
                        colSpan={6}
                        className="px-5 py-8 text-center text-xs text-slate-500"
                      >
                        No cases loaded. Confirm FastAPI is running on port 8000.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </CardContent>
        </Card>

        {/* Active Alerts & Specialist Queue Preview */}
        <Card>
          <CardHeader className="flex-row items-center justify-between">
            <div>
              <CardTitle>Escalation Alerts</CardTitle>
              <CardDescription>Visible to current authenticated role</CardDescription>
            </div>
            <Button size="sm" variant="ghost" onClick={onOpenExceptions}>
              Inbox
            </Button>
          </CardHeader>
          <CardContent className="space-y-3">
            {openExceptions.length === 0 ? (
              <div className="rounded-md border border-slate-200 bg-slate-50 px-3.5 py-4 text-xs text-slate-600">
                No open specialist exceptions visible for your current role.
              </div>
            ) : (
              openExceptions.map((exc) => (
                <div
                  key={exc.exception_id}
                  className="rounded-md border border-rose-200 bg-rose-50/50 p-3 text-xs"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono font-semibold text-rose-900">
                      {exc.code}
                    </span>
                    <Badge variant="danger">{exc.department}</Badge>
                  </div>
                  <p className="mt-1.5 text-slate-700 leading-relaxed">
                    {exc.summary ?? "Specialist review required."}
                  </p>
                  <div className="mt-2 flex items-center justify-between text-[11px] text-slate-500">
                    <span>Patient #{exc.patient_id}</span>
                    <span className="font-mono">v{exc.version}</span>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      {/* Recharts Pipeline Stage Breakdown */}
      <Card>
        <CardHeader>
          <CardTitle>Patient #{patientId} Stage Readiness Breakdown</CardTitle>
          <CardDescription>
            Live milestone state distribution synchronized from GET{" "}
            <span className="font-mono">/api/patients/{patientId}/workflow</span>
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="h-[220px] w-full">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={milestoneProgressData}
                margin={{ top: 8, right: 16, left: -24, bottom: 4 }}
              >
                <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" vertical={false} />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 11, fill: "#475569" }}
                  axisLine={{ stroke: "#cbd5e1" }}
                  tickLine={false}
                />
                <YAxis
                  allowDecimals={false}
                  domain={[0, 1]}
                  ticks={[0, 1]}
                  tick={{ fontSize: 11, fill: "#64748b" }}
                  axisLine={false}
                  tickLine={false}
                />
                <Tooltip
                  contentStyle={{
                    borderRadius: "8px",
                    border: "1px solid #e2e8f0",
                    fontSize: "12px",
                  }}
                />
                <Bar dataKey="completed" stackId="a" fill="#059669" name="Completed" radius={[4, 4, 0, 0]} />
                <Bar dataKey="ready" stackId="a" fill="#0284c7" name="Ready / Active" radius={[4, 4, 0, 0]} />
                <Bar dataKey="blocked" stackId="a" fill="#e11d48" name="Blocked" radius={[4, 4, 0, 0]} />
                <Bar dataKey="pending" stackId="a" fill="#94a3b8" name="Pending (Locked)" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
