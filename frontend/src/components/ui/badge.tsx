import * as React from "react"
import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

/**
 * A quiet status pill: tinted ground, no border, no uppercase shouting. At the
 * densities this UI reaches -- fourteen results in one list -- a loud badge on
 * every row stops carrying information and becomes texture.
 */
const badgeVariants = cva(
  "inline-flex items-center gap-1.5 rounded-full px-2 py-[3px] text-2xs font-medium leading-none",
  {
    variants: {
      variant: {
        default: "bg-muted text-muted-foreground",
        ok: "bg-ok/[0.10] text-ok",
        warn: "bg-warn/[0.12] text-warn",
        danger: "bg-danger/[0.10] text-danger",
        info: "bg-primary/[0.10] text-primary",
      },
    },
    defaultVariants: { variant: "default" },
  }
)

export function Badge({
  className, variant, dot, children, ...props
}: React.HTMLAttributes<HTMLSpanElement> & VariantProps<typeof badgeVariants> & { dot?: boolean }) {
  return (
    <span className={cn(badgeVariants({ variant }), className)} {...props}>
      {dot && <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-current" aria-hidden="true" />}
      {children}
    </span>
  )
}
