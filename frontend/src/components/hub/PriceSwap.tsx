import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { isAdminSignedIn } from "@/lib/adminAuth"
import { cn, rupees } from "@/lib/utils"

type Stage = "idle" | "displayed" | "swapped" | "rejected"

/**
 * The price tag versus the register.
 *
 * A price is displayed, then changed underneath the pending request -- exactly
 * as a race between "what the customer saw" and "what is true now" would look
 * -- and confirmation is rejected because the amount is re-read at confirm
 * time, never trusted from what was shown.
 */
export function PriceSwap({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [stage, setStage] = useState<Stage>("idle")
  const [shown, setShown] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [needsAdmin, setNeedsAdmin] = useState(false)

  async function run() {
    // Changing a price is a merchant action, gated behind the same admin
    // login as the mandate editor -- checked up front rather than letting
    // the swap fail midway and leave the price altered with no revert.
    if (!isAdminSignedIn()) { setNeedsAdmin(true); return }
    setNeedsAdmin(false)
    setBusy(true); setStage("idle")
    const who = await freshAgent()
    const draft = await api.intentFromSku(who, "SKU-COFFEE")
    if (!draft.ok) { setBusy(false); return }
    const p = draft.body.awaiting_confirmation
    setShown(p.displayed_amount_paise)
    setStage("displayed")
    await new Promise(r => setTimeout(r, 500))

    const swapped = await api.setPrice("SKU-COFFEE", p.displayed_amount_paise + 20000)
    if (!swapped.ok) { setBusy(false); setNeedsAdmin(swapped.status === 401); return }
    setStage("swapped")
    await new Promise(r => setTimeout(r, 500))

    const res = await api.confirm(p.request_id)
    await api.setPrice("SKU-COFFEE", p.displayed_amount_paise)
    setStage(res.status === 409 ? "rejected" : "idle")
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Changes the catalog price after it was shown, then tries to confirm
          at the price that was displayed a moment ago.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Running…" : "Swap the price mid-flight"}
        </Button>
      </div>

      {needsAdmin && (
        <p className="text-2xs text-faint">
          Changing a price is a merchant action. Sign in as admin (Overview
          tab) to run this demonstration.
        </p>
      )}

      {shown != null && (
        <div className="flex items-center justify-center gap-3 rounded-md border border-border bg-muted/20 py-4">
          <div className="text-center">
            <p className="text-2xs text-faint">Shown to customer</p>
            <p className={cn("mt-0.5 text-lg font-semibold tabular-nums",
              stage === "swapped" || stage === "rejected" ? "text-faint line-through" : "text-foreground")}>
              {rupees(shown)}
            </p>
          </div>
          {(stage === "swapped" || stage === "rejected") && (
            <motion.div initial={{ opacity: 0, x: -8 }} animate={{ opacity: 1, x: 0 }} className="text-center">
              <p className="text-2xs text-faint">Catalog now says</p>
              <p className="mt-0.5 text-lg font-semibold tabular-nums text-warn">
                {rupees(shown + 20000)}
              </p>
            </motion.div>
          )}
        </div>
      )}

      {stage === "rejected" && (
        <motion.p initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                  className="mono mt-2.5 flex items-center gap-1.5 text-2xs text-danger">
          <Ban className="h-3.5 w-3.5" aria-hidden="true" /> 409 PRICE_MISMATCH — nothing charged
        </motion.p>
      )}
    </div>
  )
}
