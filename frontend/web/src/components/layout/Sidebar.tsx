import {
  Bot,
  FileClock,
  GitBranch,
  Inbox,
  LayoutDashboard,
  ShieldCheck,
} from "lucide-react";
import { cn } from "@/lib/utils";

export type NavTab =
  | "overview"
  | "patient"
  | "exceptions"
  | "assistant"
  | "audit";

interface SidebarProps {
  activeTab: NavTab;
  onSelectTab: (tab: NavTab) => void;
  openExceptionCount: number;
  patientId: string;
}

const NAV_ITEMS: { id: NavTab; label: string; icon: typeof LayoutDashboard }[] = [
  { id: "overview", label: "Operations Overview", icon: LayoutDashboard },
  { id: "patient", label: "Patient #402 DAG", icon: GitBranch },
  { id: "exceptions", label: "Exception Inbox", icon: Inbox },
  { id: "assistant", label: "AI Assistant", icon: Bot },
  { id: "audit", label: "Audit Timeline", icon: FileClock },
];

export function Sidebar({
  activeTab,
  onSelectTab,
  openExceptionCount,
  patientId,
}: SidebarProps) {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-slate-800 bg-[#0a1426] text-slate-200">
      <div className="flex items-center gap-3 border-b border-slate-800/80 px-5 py-4">
        <div className="min-w-0">
          <img
            src="/images/bain-logo.svg"
            alt="bAIn"
            className="h-10 w-[148px] object-contain object-left"
          />
          <div className="mt-0.5 pl-1 text-[11px] tracking-wide text-slate-400">
            Hospital Discharge Ops
          </div>
        </div>
      </div>

      <nav className="flex-1 space-y-1 px-3 py-4" aria-label="Primary navigation">
        <div className="px-2.5 pb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          Command Center
        </div>
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => {
          const active = activeTab === id;
          return (
            <button
              key={id}
              type="button"
              onClick={() => onSelectTab(id)}
              className={cn(
                "flex w-full items-center justify-between rounded-md px-2.5 py-2 text-left text-sm font-medium transition-colors cursor-pointer",
                active
                  ? "bg-white/10 text-white shadow-xs"
                  : "text-slate-400 hover:bg-white/5 hover:text-slate-100",
              )}
            >
              <span className="flex items-center gap-2.5">
                <Icon className="h-4 w-4 shrink-0" />
              <span>{id === "patient" ? `Patient #${patientId} DAG` : label}</span>
              </span>
              {id === "exceptions" && openExceptionCount > 0 && (
                <span className="rounded-full bg-rose-500/20 px-2 py-0.5 font-mono text-[11px] font-semibold text-rose-300">
                  {openExceptionCount}
                </span>
              )}
            </button>
          );
        })}
      </nav>

      <div className="border-t border-slate-800/80 px-4 py-3.5 text-[11px] text-slate-400">
        <div className="flex items-center gap-2 text-slate-300 font-medium">
          <ShieldCheck className="h-3.5 w-3.5 text-emerald-400" />
          <span>Synthetic Demo Mode</span>
        </div>
        <p className="mt-1 leading-relaxed text-slate-500">
          Backend FastAPI enforces all DAG transitions, RBAC, and PHI redaction.
        </p>
      </div>
    </aside>
  );
}
