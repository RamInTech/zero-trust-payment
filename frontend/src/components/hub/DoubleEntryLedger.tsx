import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, SearchCheck, ShieldCheck, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"

/**
 * Four attempts to make the books stop adding up, on a throwaway schema.
 *
 * The first three are refused outright -- by the ledger's own check, by the
 * database when a line is added to a closed posting, and by Postgres at COMMIT
 * when a forged posting written with raw SQL does not net to zero. The fourth
 * repeats that forgery after dropping the commit-time trigger, which someone
 * with DDL rights could do: it lands, and re-adding the books names it. That
 * is the honest boundary, rather than pretending the write is impossible.
 */
export function DoubleEntryLedger({ onChanged }: { freshAgent?: () => Promise<string>; onChanged?: () => void }) {
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setResult(null)
    const res = await api.ledgerImbalanceDemo()
    if (res.ok) setResult(res.body)
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          On a throwaway schema: an unbalanced posting, a line added to an
          existing posting, a forged raw-SQL posting, then the same forgery
          with the commit-time check dropped.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Trying…" : "Try to unbalance the books"}
        </Button>
      </div>

      {result && (
        <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                    className="flex flex-col gap-1.5">
          <p className="mono flex items-center gap-1.5 text-2xs text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5 text-ok" aria-hidden="true" />
            before: {result.before.summary}
          </p>
          <p className="mono flex items-center gap-1.5 text-2xs text-ok">
            <Ban className="h-3.5 w-3.5" aria-hidden="true" />
            unbalanced posting refused — {result.refused}
          </p>
          <p className="mono flex items-center gap-1.5 text-2xs text-ok">
            <Ban className="h-3.5 w-3.5" aria-hidden="true" />
            line added to an existing posting: blocked by the database
          </p>
          <p className="mono flex items-center gap-1.5 text-2xs text-ok">
            <Ban className="h-3.5 w-3.5" aria-hidden="true" />
            forged raw-SQL posting refused at COMMIT — {result.raw_sql_refused}
          </p>
          <p className="mono flex items-center gap-1.5 text-2xs text-warn">
            <SearchCheck className="h-3.5 w-3.5" aria-hidden="true" />
            with the commit-time trigger dropped, it lands, and is caught: {result.after_check_removed.summary}
          </p>
        </motion.div>
      )}
    </div>
  )
}
