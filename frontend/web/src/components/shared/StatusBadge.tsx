import { CheckCircle2, Clock, Loader2, OctagonAlert, CircleDot } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { WorkflowStatus } from "@/types/api";

const STATUS_META: Record<
  WorkflowStatus,
  {
    label: string;
    variant: "success" | "danger" | "warning" | "info" | "muted";
    Icon: typeof CheckCircle2;
  }
> = {
  completed: { label: "Completed", variant: "success", Icon: CheckCircle2 },
  blocked: { label: "Blocked", variant: "danger", Icon: OctagonAlert },
  ready: { label: "Ready", variant: "info", Icon: CircleDot },
  in_progress: { label: "In Progress", variant: "warning", Icon: Loader2 },
  pending: { label: "Pending", variant: "muted", Icon: Clock },
};

export function StatusBadge({ status }: { status: WorkflowStatus }) {
  const { label, variant, Icon } = STATUS_META[status];
  return (
    <Badge variant={variant}>
      <Icon className="h-3 w-3 shrink-0" />
      <span>{label}</span>
    </Badge>
  );
}
