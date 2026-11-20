import * as React from "react";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/utils";

const badgeVariants = cva(
  "inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs font-medium tracking-tight transition-colors",
  {
    variants: {
      variant: {
        default: "border-slate-200 bg-slate-100 text-slate-800",
        navy: "border-[#172a4d]/20 bg-[#0f1d36]/8 text-[#0f1d36]",
        success: "border-emerald-200 bg-emerald-50 text-emerald-800",
        warning: "border-amber-200 bg-amber-50 text-amber-800",
        danger: "border-rose-200 bg-rose-50 text-rose-800",
        info: "border-sky-200 bg-sky-50 text-sky-800",
        muted: "border-slate-200 bg-slate-50 text-slate-600",
      },
    },
    defaultVariants: {
      variant: "default",
    },
  },
);

export interface BadgeProps
  extends React.HTMLAttributes<HTMLSpanElement>,
    VariantProps<typeof badgeVariants> {}

export function Badge({ className, variant, ...props }: BadgeProps) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />;
}
