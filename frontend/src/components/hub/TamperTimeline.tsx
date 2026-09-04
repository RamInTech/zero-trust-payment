import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, ShieldCheck, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn } from "@/lib/utils"

/**
 * Four raw SQL statements, run one at a time against the live audit database,
 * bypassing the application entirely -- and the entry count that never moves.
 *
 * This is the strongest card in the Hub precisely because it is not "our code
 * checks permissions" -- it is "even a direct sqlite3 connection cannot do
 * this", enforced by BEFORE UPDATE / BEFORE DELETE triggers on the table
 * itself. Revealing one statement at a time, with a pause, is what makes that
 * legible on video instead of a single instant "4/4 blocked" line.
 */
export function TamperTimeline({ onChanged }: { onChanged?: () => void } = {}) {
  const [attempts, setAttempts] = useState<any[]>([])
  const [before, setBefore] = useState<number | null>(null)
  const [after, setAfter] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setAttempts([]); setAfter(null)
    const res = await api.tamper()
    if (res.status === 409) { setBusy(false); return }
    setBefore(res.body.entries_before)
    const all = res.body.attempts as any[]
    for (let i = 0; i < all.length; i++) {
      await new Promise(r => setTimeout(r, 380))
      setAttempts(prev => [...prev, all[i]])
    }
    setAfter(res.body.entries_after)
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Runs raw UPDATE and DELETE statements against the live database, one
          at a time, outside the application entirely.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Attacking…" : "Attack the database directly"}
        </Button>
      </div>

      {attempts.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {attempts.map((a, i) => (
            <motion.div key={i} initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
                        className="flex items-center gap-2 rounded-md border border-border bg-muted/30 px-2.5 py-1.5">
              <code className="mono flex-1 truncate text-2xs text-muted-foreground">{a.sql}</code>
              <span className={cn(
                "mono flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-medium",
                a.outcome === "BLOCKED" && "bg-ok/[0.12] text-ok",
                a.outcome === "SUCCEEDED" && "bg-danger/[0.12] text-danger",
                a.outcome === "NO_ROWS_MATCHED" && "bg-muted text-faint",
              )}>
                {a.outcome === "BLOCKED" && <Ban className="h-2.5 w-2.5" aria-hidden="true" />}
                {a.outcome}
              </span>
            </motion.div>
          ))}
        </div>
      )}

      {after != null && (
        <motion.p initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                  className="mt-2.5 flex items-center gap-1.5 border-t border-border pt-2.5 text-2xs">
          <ShieldCheck className="h-3.5 w-3.5 text-ok" aria-hidden="true" />
          <span className="text-foreground">{before} entries before</span>
          <span className="text-faint">→</span>
          <span className="font-medium text-ok">{after} entries after</span>
          <span className="text-faint">— unchanged</span>
        </motion.p>
      )}
    </div>
  )
}
