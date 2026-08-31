import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded border px-2 py-[2px] text-[10.5px] font-semibold uppercase tracking-[0.06em]",
  {
    variants: {
      variant: {
        default: "border-border bg-muted text-muted-foreground",
        ok: "border-ok/25 bg-ok/[0.08] text-ok",
        warn: "border-warn/30 bg-warn/[0.09] text-warn",
        danger: "border-danger/25 bg-danger/[0.07] text-danger",
        info: "border-primary/25 bg-primary/[0.08] text-primary",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

export function Badge({
  className, variant, dot, ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props}>
      {dot && <span className="pulse-dot h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />}
      {props.children}
    </span>
  )
}
