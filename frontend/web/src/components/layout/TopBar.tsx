import { CheckCircle2, RefreshCw, Shield, WifiOff } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { DEMO_ROLES, getRoleProfile } from "@/lib/roles";
import type { HealthResponse, Role } from "@/types/api";

interface TopBarProps {
  health: {
    status: "checking" | "online" | "offline";
    data: HealthResponse | null;
    latencyMs: number | null;
    error: string | null;
  };
  onCheckHealth: () => void;
  activeRole: Role;
  onChangeRole: (role: Role) => void;
  onRefreshData: () => void;
  isRefreshing: boolean;
}

export function TopBar({
  health,
  onCheckHealth,
  activeRole,
  onChangeRole,
  onRefreshData,
  isRefreshing,
}: TopBarProps) {
  const roleProfile = getRoleProfile(activeRole);

  return (
    <header className="flex flex-wrap items-center justify-between gap-4 border-b border-slate-200 bg-white px-6 py-3">
      <div className="flex items-center gap-3">
        <button
          type="button"
          onClick={onCheckHealth}
          title="Re-check GET /api/health"
          className="inline-flex items-center gap-2 rounded-full border border-slate-200 bg-slate-50 px-3 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100 transition-colors cursor-pointer"
        >
          {health.status === "online" ? (
            <>
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-600" />
              <span>API Online</span>
              <span className="font-mono text-[11px] text-slate-400">
                {health.data?.service ?? "bAin-api"}
                {health.latencyMs !== null ? ` · ${health.latencyMs}ms` : ""}
              </span>
            </>
          ) : health.status === "checking" ? (
            <>
              <RefreshCw className="h-3.5 w-3.5 animate-spin text-slate-500" />
              <span>Checking /api/health…</span>
            </>
          ) : (
            <>
              <WifiOff className="h-3.5 w-3.5 text-rose-600" />
              <span className="text-rose-700">API Offline</span>
            </>
          )}
        </button>

        <Badge
          variant={
            roleProfile.phiPolicy === "clinical_phi"
              ? "navy"
              : roleProfile.phiPolicy === "operational_only"
                ? "warning"
                : "info"
          }
        >
          <Shield className="h-3 w-3" />
          <span>{roleProfile.scopeLabel}</span>
        </Badge>
      </div>

      <div className="flex items-center gap-3">
        <div className="flex items-center gap-2">
          <label
            htmlFor="demo-role-select"
            className="text-xs font-medium text-slate-500"
          >
            Demo Role
          </label>
          <select
            id="demo-role-select"
            value={activeRole}
            onChange={(e) => onChangeRole(e.target.value as Role)}
            className="h-8 rounded-md border border-slate-300 bg-white px-2.5 text-xs font-medium text-slate-900 shadow-2xs focus:border-[#0f1d36] focus:outline-none focus:ring-1 focus:ring-[#0f1d36]"
          >
            {DEMO_ROLES.map((item) => (
              <option key={item.role} value={item.role}>
                {item.label} ({item.role})
              </option>
            ))}
          </select>
        </div>

        <Button
          size="sm"
          variant="outline"
          onClick={onRefreshData}
          disabled={isRefreshing}
        >
          <RefreshCw className={`h-3.5 w-3.5 ${isRefreshing ? "animate-spin" : ""}`} />
          <span>Sync</span>
        </Button>
      </div>
    </header>
  );
}
