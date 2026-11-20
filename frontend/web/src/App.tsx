import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Sidebar, type NavTab } from "@/components/layout/Sidebar";
import { TopBar } from "@/components/layout/TopBar";
import { ApiErrorBanner } from "@/components/shared/ApiErrorBanner";
import { ApiError, clearTurnApi } from "@/lib/api";
import { getRoleProfile } from "@/lib/roles";
import type {
  AuditEventItem,
  DashboardSummary,
  ExceptionItem,
  HealthResponse,
  PatientDetail,
  PatientSummary,
  Role,
  WorkflowResponse,
} from "@/types/api";

const DEFAULT_PATIENT_ID = "402";

const AiAssistantView = lazy(() => import("@/pages/AiAssistantView").then((m) => ({ default: m.AiAssistantView })));
const AuditTimelineView = lazy(() => import("@/pages/AuditTimelineView").then((m) => ({ default: m.AuditTimelineView })));
const ExceptionInboxView = lazy(() => import("@/pages/ExceptionInboxView").then((m) => ({ default: m.ExceptionInboxView })));
const OperationsOverview = lazy(() => import("@/pages/OperationsOverview").then((m) => ({ default: m.OperationsOverview })));
const PatientDetailView = lazy(() => import("@/pages/PatientDetailView").then((m) => ({ default: m.PatientDetailView })));

export function App() {
  const [activeTab, setActiveTab] = useState<NavTab>("overview");
  const [activeRole, setActiveRole] = useState<Role>("doctor");

  const [health, setHealth] = useState<{
    status: "checking" | "online" | "offline";
    data: HealthResponse | null;
    latencyMs: number | null;
    error: string | null;
  }>({
    status: "checking",
    data: null,
    latencyMs: null,
    error: null,
  });

  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [activePatientId, setActivePatientId] = useState(DEFAULT_PATIENT_ID);
  const [dashboardSummary, setDashboardSummary] = useState<DashboardSummary | null>(null);
  const [patientDetail, setPatientDetail] = useState<PatientDetail | null>(null);
  const [workflow, setWorkflow] = useState<WorkflowResponse | null>(null);
  const [exceptions, setExceptions] = useState<ExceptionItem[]>([]);
  const [auditEvents, setAuditEvents] = useState<AuditEventItem[]>([]);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [loadError, setLoadError] = useState<ApiError | null>(null);

  const roleProfile = getRoleProfile(activeRole);
  const token = roleProfile.demoToken;

  const checkHealth = useCallback(async () => {
    setHealth((prev) => ({ ...prev, status: "checking", error: null }));
    const started = performance.now();
    try {
      const data = await clearTurnApi.getHealth();
      const latencyMs = Math.max(1, Math.round(performance.now() - started));
      setHealth({ status: "online", data, latencyMs, error: null });
    } catch (err) {
      const message = err instanceof Error ? err.message : "Health check failed.";
      setHealth({ status: "offline", data: null, latencyMs: null, error: message });
    }
  }, []);

  const syncAll = useCallback(async () => {
    setIsRefreshing(true);
    setLoadError(null);
    try {
      const [summaryRes, patientsRes, detailRes, workflowRes, exceptionsRes, auditRes] =
        await Promise.all([
          clearTurnApi.getDashboardSummary(token),
          clearTurnApi.listPatients(token),
          clearTurnApi.getPatientDetail(token, activePatientId),
          clearTurnApi.getPatientWorkflow(token, activePatientId),
          clearTurnApi.listExceptions(token),
          clearTurnApi.getPatientAudit(token, activePatientId),
        ]);

      setDashboardSummary(summaryRes);
      setPatients(patientsRes.items);
      setPatientDetail(detailRes);
      setWorkflow(workflowRes);
      setExceptions(exceptionsRes.items);
      setAuditEvents(auditRes.items);
    } catch (err) {
      if (err instanceof ApiError) {
        setLoadError(err);
      } else {
        setLoadError(
          new ApiError(0, "NETWORK_ERROR", "Unable to load operational data from FastAPI."),
        );
      }
    } finally {
      setIsRefreshing(false);
    }
  }, [activePatientId, token]);

  useEffect(() => {
    void checkHealth();
  }, [checkHealth]);

  useEffect(() => {
    // Immediately purge prior role data from memory so clinical PHI never lingers across role switches.
    setPatients([]);
    setPatientDetail(null);
    setWorkflow(null);
    setExceptions([]);
    setAuditEvents([]);
    void syncAll();
  }, [activeRole, syncAll]);

  const openExceptionCount = exceptions.filter((e) => e.status === "open").length;

  return (
    <div className="flex min-h-screen bg-slate-50 text-slate-900">
      <Sidebar
        activeTab={activeTab}
        onSelectTab={setActiveTab}
        openExceptionCount={openExceptionCount}
        patientId={activePatientId}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          health={health}
          onCheckHealth={() => void checkHealth()}
          activeRole={activeRole}
          onChangeRole={(role) => setActiveRole(role)}
          onRefreshData={() => {
            void checkHealth();
            void syncAll();
          }}
          isRefreshing={isRefreshing}
        />

        <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-6">
          {loadError && (
            <div className="mb-5">
              <ApiErrorBanner
                error={loadError}
                onRefresh={() => void syncAll()}
                onDismiss={() => setLoadError(null)}
              />
            </div>
          )}

          <Suspense fallback={<div className="rounded-xl border border-slate-200 bg-white p-6 text-sm text-slate-500">Loading workspace…</div>}>
          <AnimatePresence mode="wait">
            <motion.div
              key={`${activeTab}-${activeRole}`}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }}
              transition={{ duration: 0.14 }}
            >
              {activeTab === "overview" && (
                <OperationsOverview
                  summary={dashboardSummary}
                  patients={patients}
                  exceptions={exceptions}
                  workflow={workflow}
                  patientId={activePatientId}
                  onOpenPatient={(patientId) => {
                    setActivePatientId(patientId);
                    setActiveTab("patient");
                  }}
                  onOpenExceptions={() => setActiveTab("exceptions")}
                />
              )}

              {activeTab === "patient" && (
                <PatientDetailView
                  activeRole={activeRole}
                  token={token}
                  detail={patientDetail}
                  workflow={workflow}
                  onRefresh={syncAll}
                />
              )}

              {activeTab === "exceptions" && (
                <ExceptionInboxView
                  activeRole={activeRole}
                  token={token}
                  exceptions={exceptions}
                  onRefresh={syncAll}
                />
              )}

              {activeTab === "assistant" && (
                <AiAssistantView
                  activeRole={activeRole}
                  token={token}
                  workflow={workflow}
                />
              )}

              {activeTab === "audit" && (
                <AuditTimelineView
                  activeRole={activeRole}
                  patientId={activePatientId}
                  events={auditEvents}
                />
              )}
            </motion.div>
          </AnimatePresence>
          </Suspense>
        </main>
      </div>
    </div>
  );
}

export default App;
