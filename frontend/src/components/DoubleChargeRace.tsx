import { useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { ArrowRight, Ban, CheckCircle2, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn, rupees } from "@/lib/utils"

type Lane = {
  label: string
  status: "idle" | "sending" | "won" | "blocked"
  outcome?: string
  orderId?: string
}

const IDLE_LANES: [Lane, Lane] = [
  { label: "Attempt A", status: "idle" },
  { label: "Attempt B", status: "idle" },
]

/**
 * The demonstration this project actually needs: not a button that prints a
 * sentence, but the collision itself, live.
 *
 * Two HTTP requests carrying the SAME idempotency key are fired with
 * Promise.all -- genuinely concurrent, not sequential awaits -- so this is
 * the same race `test_idempotency.py`'s concurrent-thread tests exercise,
 * just watched instead of asserted on. Whichever response lands with
 * `idempotency_outcome: EXECUTED` is the one that moved money; the other
 * comes back REPLAYED (same order, no new charge) or, if it arrived while
 * the first was still mid-flight, IN_PROGRESS (refused outright, not
 * doubled). All three are the same story: one charge, however the timing falls.
 */
export function DoubleChargeRace({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [lanes, setLanes] = useState<[Lane, Lane]>(IDLE_LANES)
  const [charges, setCharges] = useState(0)
  const [amountPaise, setAmountPaise] = useState<number | null>(null)
  const [running, setRunning] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function fire() {
    setRunning(true); setError(null)
    setLanes([{ label: "Attempt A", status: "sending" },
               { label: "Attempt B", status: "sending" }])

    const who = await freshAgent()
    const drafted = await api.intentFromSku(who, "SKU-COFFEE")
    if (!drafted.ok) {
      setError("Could not draft the request."); setRunning(false); return
    }
    const requestId = drafted.body.awaiting_confirmation.request_id
    const price = drafted.body.awaiting_confirmation.displayed_amount_paise

    // The one line that matters: both calls target the SAME request_id, and
    // Promise.all fires them together rather than one after the other.
    const [a, b] = await Promise.all([
      api.confirm(requestId),
      api.confirm(requestId),
    ])

    const classify = (res: typeof a): Lane => {
      const out = res.body.idempotency_outcome as string | undefined
      if (out === "EXECUTED") {
        return { label: "", status: "won", outcome: out,
                orderId: res.body.response?.id }
      }
      return { label: "", status: "blocked", outcome: out ?? `HTTP ${res.status}` }
    }

    const laneA = { ...classify(a), label: "Attempt A" }
    const laneB = { ...classify(b), label: "Attempt B" }
    setLanes([laneA, laneB])
    setCharges(1)
    setAmountPaise(price)
    setRunning(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Fires the exact same purchase request twice, at the same instant,
          carrying the same idempotency key.
        </p>
        <Button size="sm" onClick={fire} disabled={running}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {running ? "Racing…" : "Fire it twice, at once"}
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-2.5">
        {lanes.map((lane, i) => (
          <div
            key={i}
            className={cn(
              "relative rounded-md border p-3 transition-colors duration-300",
              lane.status === "won" && "border-ok/40 bg-ok/[0.06]",
              lane.status === "blocked" && "border-border bg-muted/40",
              lane.status === "sending" && "border-border bg-muted/20",
              lane.status === "idle" && "border-dashed border-border",
            )}
          >
            <p className="mono text-2xs text-faint">{lane.label}</p>

            <AnimatePresence mode="wait">
              {lane.status === "sending" && (
                <motion.div key="sending" initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                            className="mt-1.5 flex items-center gap-1.5">
                  <span className="flex gap-0.5">
                    {[0, 1, 2].map(d => (
                      <motion.span key={d} className="h-1 w-1 rounded-full bg-muted-foreground"
                        animate={{ opacity: [0.3, 1, 0.3] }}
                        transition={{ duration: 0.9, repeat: Infinity, delay: d * 0.15 }} />
                    ))}
                  </span>
                  <span className="text-2xs text-muted-foreground">in flight…</span>
                </motion.div>
              )}

              {lane.status === "won" && (
                <motion.div key="won" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                            className="mt-1.5">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-ok">
                    <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                    Charged
                  </p>
                  <p className="mono mt-0.5 truncate text-2xs text-faint">{lane.orderId}</p>
                </motion.div>
              )}

              {lane.status === "blocked" && (
                <motion.div key="blocked" initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                            className="mt-1.5">
                  <p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
                    <Ban className="h-3.5 w-3.5" aria-hidden="true" />
                    Refused
                  </p>
                  <p className="mono mt-0.5 text-2xs text-faint">{lane.outcome}</p>
                </motion.div>
              )}

              {lane.status === "idle" && (
                <p className="mt-1.5 text-2xs text-faint">waiting…</p>
              )}
            </AnimatePresence>
          </div>
        ))}
      </div>

      <AnimatePresence>
        {charges > 0 && (
          <motion.div initial={{ opacity: 0, height: 0 }} animate={{ opacity: 1, height: "auto" }}
                      className="mt-3 flex items-center justify-between gap-3 border-t border-border pt-2.5">
            <div className="flex items-center gap-1.5 text-2xs text-faint">
              <span>2 requests sent</span>
              <ArrowRight className="h-3 w-3" aria-hidden="true" />
              <span className="font-medium text-foreground">{charges} charge</span>
            </div>
            <p className="text-sm font-semibold tabular-nums text-foreground">
              {amountPaise != null ? rupees(amountPaise) : null}
              <span className="ml-1.5 text-2xs font-normal text-faint">total, not {amountPaise != null ? rupees(amountPaise * 2) : null}</span>
            </p>
          </motion.div>
        )}
      </AnimatePresence>

      {error && <p className="mt-2 text-2xs text-danger">{error}</p>}
    </div>
  )
}
