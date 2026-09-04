import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, HelpCircle, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn } from "@/lib/utils"

type Stage = "idle" | "sending" | "timeout" | "retried"

/**
 * The bank transfer that lost signal.
 *
 * A timeout is neither success nor failure -- it is genuinely unknown, and the
 * three-way landing (not a spinner resolving to a checkmark or an X) is the
 * point. The retry that follows must be refused, not resolved with a guess:
 * that refusal is what stops a timeout from becoming a double charge.
 */
export function UnknownOutcomeTimeline({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [stage, setStage] = useState<Stage>("idle")
  const [retryOutcome, setRetryOutcome] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setStage("idle"); setRetryOutcome(null)
    const who = await freshAgent()
    const draft = await api.intentFromSku(who, "SKU-COFFEE")
    if (!draft.ok) { setBusy(false); return }
    const requestId = draft.body.awaiting_confirmation.request_id

    setStage("sending")
    await api.armTimeout()
    const first = await api.confirm(requestId)
    if (first.status !== 503) { setBusy(false); return }
    await new Promise(r => setTimeout(r, 500))
    setStage("timeout")

    await new Promise(r => setTimeout(r, 600))
    const retry = await api.confirm(requestId)
    setRetryOutcome(retry.body.idempotency_outcome ?? `HTTP ${retry.status}`)
    setStage("retried")
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Forces the provider call to time out, then retries the same request
          right after.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Running…" : "Time it out, then retry"}
        </Button>
      </div>

      <div className="flex items-center justify-center gap-4 py-2">
        {(["Success", "Unknown", "Failed"] as const).map(label => {
          const isUnknown = label === "Unknown"
          const landed = isUnknown && (stage === "timeout" || stage === "retried")
          return (
            <div key={label} className={cn(
              "flex flex-col items-center gap-1 rounded-md border px-4 py-2.5 text-2xs font-medium transition-all duration-300",
              landed
                ? "scale-110 border-warn/40 bg-warn/[0.08] text-warn"
                : "border-dashed border-border text-faint",
            )}>
              {isUnknown && <HelpCircle className="h-3.5 w-3.5" aria-hidden="true" />}
              {label}
            </div>
          )
        })}
      </div>

      {stage === "retried" && (
        <motion.p initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                  className="mono mt-1 flex items-center justify-center gap-1.5 text-2xs text-danger">
          <Ban className="h-3.5 w-3.5" aria-hidden="true" />
          retry → {retryOutcome} — refused, not resolved with a guess
        </motion.p>
      )}
    </div>
  )
}
