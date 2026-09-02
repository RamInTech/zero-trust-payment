import * as React from "react"
import { cn } from "@/lib/utils"

/**
 * A key/value list rendered as hairline-separated rows rather than a tinted
 * box nested inside a card. Nesting a bordered, filled panel inside an already
 * bordered card is the single thing that made this UI read as a template; one
 * container, one rule between rows, is enough.
 */
export function Rows({ className, ...props }: React.HTMLAttributes<HTMLDListElement>) {
  return <dl className={cn("divide-rows", className)} {...props} />
}

export function Row({
  label, value, mono = true, tone,
}: {
  label: string
  value: React.ReactNode
  mono?: boolean
  tone?: "ok" | "warn" | "danger"
}) {
  const toneClass = tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn"
    : tone === "danger" ? "text-danger" : "text-foreground"
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 first:pt-0 last:pb-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className={cn("shrink-0 text-right tabular-nums", mono ? "mono" : "text-xs", toneClass)}>
        {value}
      </dd>
    </div>
  )
}

/** A single figure with its label above it. No box, no tint, no border. */
export function Stat({
  label, value, hint,
}: {
  label: string
  value: React.ReactNode
  hint?: string
}) {
  return (
    <div>
      <div className="label">{label}</div>
      <div className="figure mt-1">{value}</div>
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </div>
  )
}
