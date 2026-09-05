import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, ShieldCheck, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"

/**
 * The log is written before the money moves -- shown by breaking the log.
 *
 * Runs against a fully throwaway stack (its own mandate, policy engine, and
 * idempotency store) with an audit log rigged to fail on every write. If the
 * ordering in `gateway.py` ever slipped -- execute first, log second -- this
 * would show a provider call going through anyway. It never does.
 */
export function AuditWriteBlocksPayment({ onChanged }: { freshAgent?: () => Promise<string>; onChanged?: () => void }) {
  const [result, setResult] = useState<any>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setResult(null)
    const res = await api.auditWriteBlocksPaymentDemo()
    if (res.ok) setResult(res.body)
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Rigs a throwaway audit log to fail on every write, then tries to run
          a purchase through it.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Breaking the log…" : "Break the log, then try to pay"}
        </Button>
      </div>

      {result && (
        <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                    className="flex flex-col gap-1.5">
          <p className="mono flex items-center gap-1.5 text-2xs text-danger">
            <Ban className="h-3.5 w-3.5" aria-hidden="true" />
            AuditWriteError: {result.raised}
          </p>
          <p className="mono flex items-center gap-1.5 text-2xs text-ok">
            <ShieldCheck className="h-3.5 w-3.5" aria-hidden="true" />
            provider calls made: {result.provider_calls} — the payment never ran
          </p>
        </motion.div>
      )}
    </div>
  )
}
