import { useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { Ban, User, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn, rupees } from "@/lib/utils"

type Stage = "idle" | "proposing" | "confirmed" | "denied"

/**
 * "A human said yes and the system still said no" -- shown, not narrated.
 *
 * Buys SKU-BEANS (Rs.900) against the demo mandate's Rs.500 cap. The pipeline
 * advances one real step at a time: propose -> a HUMAN confirms (the click
 * itself) -> the POLICY ENGINE evaluates independently and refuses. Two
 * different actors, two different verdicts -- which is the actual claim this
 * card makes, not merely "denied".
 */
export function MandateOverride({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [stage, setStage] = useState<Stage>("idle")
  const [requested, setRequested] = useState<number | null>(null)
  const [cap, setCap] = useState<number | null>(null)
  const [rule, setRule] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setStage("proposing")
    const who = await freshAgent()
    const [mandateRes, draftRes] = await Promise.all([
      api.mandate(who),
      api.intentFromSku(who, "SKU-BEANS"),
    ])
    if (!draftRes.ok) { setBusy(false); return }
    const p = draftRes.body.awaiting_confirmation
    setRequested(p.displayed_amount_paise)
    setCap(mandateRes.ok ? mandateRes.body.max_amount_paise : null)

    // The "confirm" click IS the human step -- shown as its own stage before
    // the policy verdict lands, not collapsed into one instant result.
    await new Promise(r => setTimeout(r, 450))
    setStage("confirmed")

    const res = await api.confirm(p.request_id)
    await new Promise(r => setTimeout(r, 350))
    setRule(res.body.rule)
    setStage("denied")
    setBusy(false)
    onChanged?.()
  }

  const overCap = requested != null && cap != null
    ? Math.min(100, (requested / cap) * 100) : 0

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          A human confirms a purchase above the mandate's cap. Confirmation is
          not authorisation -- watch them disagree.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy && stage !== "denied" ? "Running…" : "Confirm it anyway"}
        </Button>
      </div>

      <div className="flex items-center gap-2">
        {(["proposing", "confirmed", "denied"] as Stage[]).map((s, i) => {
          const order = ["idle", "proposing", "confirmed", "denied"]
          const reached = order.indexOf(stage) >= order.indexOf(s)
          const active = stage === s
          return (
            <div key={s} className="flex flex-1 items-center gap-2">
              <div className={cn(
                "flex flex-1 items-center justify-center gap-1.5 rounded-md border px-2 py-2 text-2xs font-medium transition-colors duration-300",
                s === "denied" && reached ? "border-danger/40 bg-danger/[0.08] text-danger"
                  : reached ? "border-ok/40 bg-ok/[0.06] text-ok" : "border-dashed border-border text-faint",
                active && "animate-pulse",
              )}>
                {s === "proposing" && <>Proposed</>}
                {s === "confirmed" && <><User className="h-3 w-3" aria-hidden="true" /> Human confirms</>}
                {s === "denied" && <><Ban className="h-3 w-3" aria-hidden="true" /> Policy denies</>}
              </div>
              {i < 2 && <span className="text-faint">→</span>}
            </div>
          )
        })}
      </div>

      <AnimatePresence>
        {requested != null && cap != null && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                      className="mt-3 border-t border-border pt-2.5">
            <div className="flex items-center justify-between text-2xs text-faint">
              <span>Requested {rupees(requested)}</span>
              <span>Cap {rupees(cap)}</span>
            </div>
            <div className="relative mt-1.5 h-2 overflow-hidden rounded-full bg-muted">
              <div className="absolute inset-y-0 left-0 rounded-full bg-ok/50"
                   style={{ width: "100%" }} />
              <motion.div
                initial={{ width: 0 }} animate={{ width: `${overCap}%` }}
                transition={{ duration: 0.6, ease: "easeOut" }}
                className={cn("absolute inset-y-0 left-0 rounded-full",
                  overCap > 100 ? "bg-danger" : "bg-ok")}
              />
              <div className="absolute inset-y-0 border-l-2 border-foreground/60"
                   style={{ left: "100%" }} />
            </div>
            {stage === "denied" && rule && (
              <p className="mono mt-2 flex items-center gap-1.5 text-2xs text-danger">
                <Ban className="h-3 w-3" aria-hidden="true" /> {rule}
              </p>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}
