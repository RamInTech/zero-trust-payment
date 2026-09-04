import { useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { Ban, CheckCircle2, Power } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn } from "@/lib/utils"

type Attempt = { label: string; approved: boolean; rule?: string; orderId?: string }

/**
 * The merchant taking authority back, mid-session.
 *
 * Runs a purchase that succeeds, revokes the mandate, then runs the same
 * purchase again and shows it refused. The second attempt is drafted BEFORE
 * the revocation on purpose -- a kill switch that only applied to new drafts
 * would leave every already-pending request still spendable, which is exactly
 * the window a merchant reaching for this is trying to close.
 *
 * Uses a throwaway agent, like every other card here, so pressing it during a
 * demo cannot brick the main agent everything else on the page depends on.
 */
export function KillSwitch({ freshAgent, onChanged }: {
  freshAgent: () => Promise<string>; onChanged?: () => void
}) {
  const [before, setBefore] = useState<Attempt | null>(null)
  const [after, setAfter] = useState<Attempt | null>(null)
  const [revoked, setRevoked] = useState(false)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setBefore(null); setAfter(null); setRevoked(false)
    const who = await freshAgent()

    const first = await api.intentFromSku(who, "SKU-COFFEE")
    if (!first.ok) { setBusy(false); return }
    const okRes = await api.confirm(first.body.awaiting_confirmation.request_id)
    setBefore({
      label: "Before", approved: okRes.body.approved,
      orderId: okRes.body.response?.id,
    })
    await new Promise(r => setTimeout(r, 500))

    // Drafted while authority still stands, confirmed after it is gone.
    const queued = await api.intentFromSku(who, "SKU-COFFEE")
    await api.revokeMandate(who)
    setRevoked(true)
    await new Promise(r => setTimeout(r, 550))

    const denied = await api.confirm(queued.body.awaiting_confirmation.request_id)
    setAfter({
      label: "After", approved: denied.body.approved, rule: denied.body.rule,
    })
    setBusy(false)
    onChanged?.()
  }

  const row = (a: Attempt | null, placeholder: string) => (
    <div className={cn(
      "rounded-md border p-3 transition-colors duration-300",
      !a ? "border-dashed border-border"
        : a.approved ? "border-ok/40 bg-ok/[0.06]" : "border-danger/40 bg-danger/[0.08]",
    )}>
      {!a ? (
        <p className="text-2xs text-faint">{placeholder}</p>
      ) : a.approved ? (
        <>
          <p className="flex items-center gap-1.5 text-xs font-medium text-ok">
            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" /> Purchased
          </p>
          <p className="mono mt-0.5 truncate text-2xs text-faint">{a.orderId}</p>
        </>
      ) : (
        <>
          <p className="flex items-center gap-1.5 text-xs font-medium text-danger">
            <Ban className="h-3.5 w-3.5" aria-hidden="true" /> Refused
          </p>
          <p className="mono mt-0.5 text-2xs text-faint">{a.rule}</p>
        </>
      )}
    </div>
  )

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Buys once, withdraws the mandate, then confirms a purchase that was
          already drafted and waiting.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Power className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Running…" : "Revoke mid-session"}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        {row(before, "not run yet")}
        {row(after, "waiting…")}
      </div>

      <AnimatePresence>
        {revoked && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                      className="mt-3 flex items-center gap-2 border-t border-border pt-2.5">
            <span className="flex items-center gap-1.5 rounded-full bg-danger/[0.12] px-2 py-0.5 text-2xs font-medium text-danger">
              <Power className="h-3 w-3" aria-hidden="true" /> Mandate revoked
            </span>
            <span className="text-2xs text-faint">
              The record is kept, not deleted — only the authority is gone.
            </span>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
