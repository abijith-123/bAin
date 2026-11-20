import { useMemo } from "react";
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { Lock } from "lucide-react";
import { StatusBadge } from "@/components/shared/StatusBadge";
import { cn } from "@/lib/utils";
import type { MilestoneItem, WorkflowStatus } from "@/types/api";

interface MilestoneNodeData extends Record<string, unknown> {
  milestone: MilestoneItem;
  selected: boolean;
}

const NODE_POSITIONS: Record<string, { x: number; y: number }> = {
  clinical_clearance: { x: 20, y: 165 },
  social_work: { x: 300, y: 455 },
  pharmacy: { x: 300, y: 20 },
  insurance: { x: 300, y: 165 },
  transport: { x: 300, y: 310 },
  patient_departed: { x: 610, y: 165 },
  housekeeping_dispatched: { x: 900, y: 165 },
  bed_ready: { x: 1190, y: 165 },
};

const BORDER_BY_STATUS: Record<WorkflowStatus, string> = {
  completed: "border-emerald-300 bg-white",
  blocked: "border-rose-300 bg-rose-50/40",
  ready: "border-sky-300 bg-sky-50/30",
  in_progress: "border-amber-300 bg-amber-50/30",
  pending: "border-slate-200 bg-slate-50/70",
};

function MilestoneNode({ data }: NodeProps<Node<MilestoneNodeData>>) {
  const { milestone, selected } = data;
  return (
    <div
      className={cn(
        "w-[210px] rounded-lg border px-3.5 py-3 shadow-xs transition-all",
        BORDER_BY_STATUS[milestone.status],
        selected && "ring-2 ring-[#0f1d36] ring-offset-2",
      )}
    >
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !border-slate-400 !bg-white"
      />
      <div className="flex items-start justify-between gap-2">
        <span className="text-xs font-semibold text-slate-900 leading-snug">
          {milestone.label}
        </span>
        {milestone.locked && (
          <span
            title="Locked until prerequisite milestones complete"
            className="inline-flex items-center rounded bg-slate-200/80 p-1 text-slate-600"
          >
            <Lock className="h-3 w-3" />
          </span>
        )}
      </div>
      <div className="mt-2.5 flex items-center justify-between gap-2">
        <StatusBadge status={milestone.status} />
        <span className="font-mono text-[10px] text-slate-400">
          {milestone.milestone_id}
        </span>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border-slate-400 !bg-white"
      />
    </div>
  );
}

const nodeTypes = { milestone: MilestoneNode };

interface DischargeDagProps {
  milestones: MilestoneItem[];
  selectedMilestoneId: string | null;
  onSelectMilestone: (milestoneId: string) => void;
}

export function DischargeDag({
  milestones,
  selectedMilestoneId,
  onSelectMilestone,
}: DischargeDagProps) {
  const statusById = useMemo(
    () => new Map(milestones.map((m) => [m.milestone_id, m.status])),
    [milestones],
  );

  const nodes: Node<MilestoneNodeData>[] = useMemo(
    () =>
      milestones.map((m, idx) => ({
        id: m.milestone_id,
        type: "milestone",
        position: NODE_POSITIONS[m.milestone_id] ?? { x: idx * 240, y: 100 },
        data: {
          milestone: m,
          selected: m.milestone_id === selectedMilestoneId,
        },
        draggable: false,
      })),
    [milestones, selectedMilestoneId],
  );

  const edges: Edge[] = useMemo(() => {
    const result: Edge[] = [];
    for (const m of milestones) {
      for (const dep of m.dependencies) {
        const satisfied = statusById.get(dep) === "completed";
        result.push({
          id: `${dep}->${m.milestone_id}`,
          source: dep,
          target: m.milestone_id,
          animated: !satisfied && m.status === "blocked",
          markerEnd: {
            type: MarkerType.ArrowClosed,
            width: 16,
            height: 16,
            color: satisfied ? "#059669" : "#94a3b8",
          },
          style: {
            stroke: satisfied ? "#059669" : "#94a3b8",
            strokeWidth: satisfied ? 2 : 1.5,
            strokeDasharray: satisfied ? undefined : "5 4",
          },
        });
      }
    }
    return result;
  }, [milestones, statusById]);

  return (
    <div className="h-[440px] w-full overflow-hidden rounded-lg border border-slate-200 bg-slate-50/70">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.16 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable
        onNodeClick={(_, node) => onSelectMilestone(node.id)}
      >
        <Background gap={18} size={1} color="#cbd5e1" />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
