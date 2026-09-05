import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, CheckCircle2, Webhook } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn } from "@/lib/utils"

type Stage = "idle" | "genuine" | "tampered"

/**
 * A webhook delivery is generated and signed by this server (Razorpay sends
 * nothing in test mode), then posted through the same receiver a real
 * delivery would hit. First unmodified -- the signature checks out and the
 * delivery is accepted. Then the same body is edited after signing -- the
 * signature now covers different bytes than what arrives, and the receiver
 * refuses it. Either way, "accepted" only ever triggers reconciliation; it
 * never writes the ledger.
 */
export function WebhookVerification({ onChanged }: { freshAgent?: () => Promise<string>; onChanged?: () => void }) {
  const [stage, setStage] = useState<Stage>("idle")
  const [genuine, setGenuine] = useState<any>(null)
  const [tampered, setTampered] = useState<any>(null)
  const [busy, setBusy] = useState(false)
  const [unavailable, setUnavailable] = useState(false)

  async function run() {
    setBusy(true); setUnavailable(false)
    const first = await api.simulateWebhook(false)
    if (!first.ok) { setBusy(false); setUnavailable(true); return }
    setGenuine(first.body); setStage("genuine")
    await new Promise(r => setTimeout(r, 500))

    const second = await api.simulateWebhook(true)
    if (second.ok) { setTampered(second.body); setStage("tampered") }
    setBusy(false)
    onChanged?.()
  }

  if (unavailable) {
    return (
      <p className="text-2xs text-faint">
        No RAZORPAY_WEBHOOK_SECRET configured for this run — the receiver has
        nothing to verify against, so this demonstration is unavailable.
      </p>
    )
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Sends this server a webhook it signs itself, then the same body
          edited after signing.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Webhook className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Delivering…" : "Simulate a delivery"}
        </Button>
      </div>

      {genuine && (
        <motion.p initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                  className="mono flex items-center gap-1.5 text-2xs text-ok">
          <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
          genuine signature — accepted, reconcile_requested={String(genuine.result.reconcile_requested)}
        </motion.p>
      )}
      {stage === "tampered" && tampered && (
        <motion.p initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                  className={cn("mono mt-1.5 flex items-center gap-1.5 text-2xs",
                    tampered.result.accepted ? "text-danger" : "text-danger")}>
          <Ban className="h-3.5 w-3.5" aria-hidden="true" />
          edited after signing — {tampered.result.rejection ?? "refused"}, nothing written
        </motion.p>
      )}
    </div>
  )
}
