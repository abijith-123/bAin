import { AlertTriangle, RefreshCw, ShieldAlert, X } from "lucide-react";
import { motion } from "motion/react";
import { ApiError } from "@/lib/api";
import { Button } from "@/components/ui/button";

interface ApiErrorBannerProps {
  error: ApiError;
  onRefresh?: () => void;
  onDismiss?: () => void;
}

export function ApiErrorBanner({ error, onRefresh, onDismiss }: ApiErrorBannerProps) {
  const isForbidden = error.status === 403 || error.code === "FORBIDDEN";
  const isStaleVersion = error.code === "STALE_VERSION";
  const currentVersion =
    typeof error.details?.current_version === "number"
      ? error.details.current_version
      : undefined;

  return (
    <motion.div
      initial={{ opacity: 0, y: -6 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18 }}
      role="alert"
      className={
        isForbidden
          ? "flex items-start justify-between gap-3 rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-amber-950"
          : "flex items-start justify-between gap-3 rounded-lg border border-rose-300 bg-rose-50 px-4 py-3 text-rose-950"
      }
    >
      <div className="flex items-start gap-2.5">
        {isForbidden ? (
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-700" />
        ) : (
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-rose-700" />
        )}
        <div className="space-y-1 text-xs">
          <div className="flex flex-wrap items-center gap-2 font-semibold">
            <span>{error.code}</span>
            {error.status > 0 && (
              <span className="rounded bg-black/5 px-1.5 py-0.5 font-mono text-[11px]">
                HTTP {error.status}
              </span>
            )}
            {isStaleVersion && currentVersion !== undefined && (
              <span className="rounded bg-rose-200/70 px-1.5 py-0.5 font-mono text-[11px] text-rose-900">
                Server version: v{currentVersion}
              </span>
            )}
          </div>
          <p className="leading-relaxed opacity-90">{error.message}</p>
          {isStaleVersion && (
            <p className="text-[11px] opacity-80">
              Another department updated this case. Refresh state before retrying your action.
            </p>
          )}
        </div>
      </div>

      <div className="flex shrink-0 items-center gap-1.5">
        {isStaleVersion && onRefresh && (
          <Button size="sm" variant="outline" onClick={onRefresh}>
            <RefreshCw className="h-3.5 w-3.5" />
            Refresh State
          </Button>
        )}
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            aria-label="Dismiss error"
            className="rounded p-1 opacity-70 hover:bg-black/5 hover:opacity-100 cursor-pointer"
          >
            <X className="h-3.5 w-3.5" />
          </button>
        )}
      </div>
    </motion.div>
  );
}
