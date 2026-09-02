import { useEffect, useState } from "react"
import { api, type Json } from "@/api"
import { cn, rupees } from "@/lib/utils"

/**
 * Where every in-flight transaction has actually got to.
 *
 * This replaces an opaque "Working…" line. The difference is not cosmetic: the
 * stages below are read from the audit log via /demo/transactions, so each dot
 * that lights up is evidence that a specific event was recorded, not an
 * animation timed to look busy. A tracker showing stages the log cannot
 * substantiate would be exactly the theatre the Security Hub exists to avoid.
 */

const STAGES = [
  { key: "proposed", label: "Proposed", who: "agent" },
  { key: "confirmed", label: "Confirmed", who: "human" },
  { key: "authorised", label: "Authorised", who: "policy" },
  { key: "executing", label: "Executing", who: "provider" },
  { key: "settled", label: "Settled", who: "provider" },
]

/** Ends the track early — the request finished rather than stalled. */
const HALTS: Record<string, { label: string; tone: string }> = {
  denied: { label: "Denied by policy", tone: "text-danger" },
  declined: { label: "Declined by you", tone: "text-muted-foreground" },
  failed: { label: "Payment failed", tone: "text-danger" },
  unknown: { label: "Outcome unknown", tone: "text-warn" },
}

export function LifecycleTracker({ refreshKey }: { refreshKey: number }) {
  const [rows, setRows] = useState<Json[]>([])

  useEffect(() => {
    let alive = true
    const load = async () => {
      const res = await api.transactions(12)
      if (alive && res.ok) setRows(res.body.transactions ?? [])
    }
    load()
    // Polled rather than pushed: this is a demo client, and a websocket would
    // add a second delivery path to keep honest for no extra insight.
    const timer = setInterval(load, 1500)
    return () => { alive = false; clearInterval(timer) }
  }, [refreshKey])

  if (rows.length === 0) {
    return (
      <p className="px-3 py-4 text-2xs text-faint">
        No transactions yet. Ask the agent for something and each one will
        appear here as it moves.
      </p>
    )
  }

  return (
    <ul className="flex flex-col gap-3 px-3 py-3" aria-label="Transaction lifecycle">
      {rows.map(row => {
        const reached: string[] = row.reached ?? []
        const halt = row.halted_at ? HALTS[row.halted_at] : null
        return (
          <li key={row.request_id} className="rounded-md border border-border bg-card p-2.5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-2xs font-medium text-foreground">
                {row.sku ?? "—"}
              </span>
              {row.amount_paise != null && (
                <span className="shrink-0 text-2xs tabular-nums text-muted-foreground">
                  {rupees(row.amount_paise)}
                </span>
              )}
            </div>

            <ol className="mt-2 flex flex-col gap-1">
              {STAGES.map(stage => {
                const hit = reached.includes(stage.key)
                // Once halted, later stages are not "pending" — they will
                // never happen, and showing them as upcoming would mislead.
                const unreachable = !!halt && !hit
                return (
                  <li key={stage.key} className="flex items-center gap-2">
                    <span
                      className={cn(
                        "h-1.5 w-1.5 shrink-0 rounded-full",
                        hit ? "bg-ok" : unreachable ? "bg-border" : "bg-muted-foreground/40",
                      )}
                      aria-hidden="true"
                    />
                    <span className={cn(
                      "text-2xs",
                      hit ? "text-foreground"
                        : unreachable ? "text-faint line-through" : "text-faint",
                    )}>
                      {stage.label}
                    </span>
                  </li>
                )
              })}
            </ol>

            {halt && (
              <p className={cn("mt-1.5 border-t border-border pt-1.5 text-2xs", halt.tone)}>
                {halt.label}
                {row.rule && <span className="mono"> · {row.rule}</span>}
              </p>
            )}
          </li>
        )
      })}
    </ul>
  )
}
