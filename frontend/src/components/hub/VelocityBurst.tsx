import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { cn } from "@/lib/utils"

type Slot = { status: "empty" | "used" | "overflow" }

/**
 * A velocity budget filling up, live, one confirmed purchase at a time.
 *
 * The slot count comes from the agent's OWN mandate (read right before firing,
 * not a hardcoded number) -- so if the demo mandate's velocity limit ever
 * changes, this card keeps telling the truth instead of a stale story.
 */
export function VelocityBurst({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [slots, setSlots] = useState<Slot[]>([])
  const [limit, setLimit] = useState<number | null>(null)
  const [rule, setRule] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setRule(null)
    const who = await freshAgent()
    const mandateRes = await api.mandate(who)
    const cap = mandateRes.ok ? mandateRes.body.velocity_limit as number : 3
    setLimit(cap)
    setSlots(Array.from({ length: cap + 1 }, () => ({ status: "empty" })))

    for (let i = 0; i < cap + 1; i++) {
      const draft = await api.intentFromSku(who, "SKU-TEA")
      if (!draft.ok) break
      const res = await api.confirm(draft.body.awaiting_confirmation.request_id)
      await new Promise(r => setTimeout(r, 260))
      setSlots(prev => prev.map((s, idx) =>
        idx === i ? { status: res.body.approved ? "used" : "overflow" } : s))
      if (!res.body.approved) { setRule(res.body.rule); break }
    }
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Confirms one purchase past the velocity limit and watches the slot
          that overflows it get refused.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Running…" : "Fill the budget, then overflow it"}
        </Button>
      </div>

      {slots.length > 0 && (
        <>
          <div className="flex gap-1.5">
            {slots.map((s, i) => (
              <motion.div
                key={i}
                initial={{ scale: 0.6, opacity: 0 }}
                animate={{ scale: 1, opacity: 1 }}
                className={cn(
                  "flex h-9 flex-1 items-center justify-center rounded-md border text-2xs font-medium transition-colors duration-300",
                  s.status === "used" && "border-ok/40 bg-ok/[0.08] text-ok",
                  s.status === "overflow" && "border-danger/40 bg-danger/[0.08] text-danger",
                  s.status === "empty" && "border-dashed border-border text-faint",
                )}
              >
                {s.status === "overflow" ? <Ban className="h-3.5 w-3.5" aria-hidden="true" /> : i + 1}
              </motion.div>
            ))}
          </div>
          <p className="mt-1.5 text-2xs text-faint">
            {limit} purchases/window allowed · attempt {slots.length} is the overflow
          </p>
          {rule && (
            <p className="mono mt-1.5 text-2xs text-danger">{rule}</p>
          )}
        </>
      )}
    </div>
  )
}
