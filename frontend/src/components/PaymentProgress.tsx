import { useEffect, useState } from "react"
import { motion } from "framer-motion"
import { Check } from "lucide-react"
import { api, type Json } from "@/api"
import { EASE, cn } from "@/lib/utils"

/**
 * The authorisation pipeline, while it is running.
 *
 * Each step lights up only when the audit log actually contains the event that
 * proves it happened -- this polls `GET /api/audit/{request_id}` rather than
 * advancing on a timer. That distinction is the whole point: a progress bar
 * that animates on setTimeout would be claiming a purchase reached the policy
 * engine when the request might have died before leaving the browser.
 *
 * The consequence is that steps can appear to jump or finish out of order on a
 * fast local call. That is honest -- it is what the log says happened.
 */

const STEPS: { event: string; label: string }[] = [
  { event: "USER_CONFIRMED", label: "Confirmation recorded" },
  { event: "PRICE_VALIDATED", label: "Price re-read from the catalog" },
  { event: "POLICY_APPROVED", label: "Checked against the mandate" },
  { event: "PAYMENT_EXECUTED", label: "Charged once" },
]

const TERMINAL = new Set([
  "POLICY_DENIED", "PAYMENT_FAILED", "PAYMENT_PENDING_VERIFICATION",
])

export function PaymentProgress({ requestId }: { requestId: string }) {
  const [seen, setSeen] = useState<Set<string>>(new Set())

  useEffect(() => {
    let live = true
    const tick = async () => {
      const res = await api.auditFor(requestId)
      if (!live || !res.ok) return
      const types = new Set((res.body.events as Json[] ?? []).map(e => e.event_type as string))
      setSeen(types)
    }
    tick()
    const id = setInterval(tick, 220)
    return () => { live = false; clearInterval(id) }
  }, [requestId])

  // Once the request has reached an end state, this component's job is done --
  // the verdict that follows says what happened.
  const halted = [...TERMINAL].some(t => seen.has(t))

  return (
    <div className="max-w-[86%] rounded-lg border border-border bg-card px-4 py-3">
      <p className="text-xs font-medium text-foreground">Authorising</p>
      <ul className="mt-2.5 grid gap-1.5">
        {STEPS.map(({ event, label }) => {
          const done = seen.has(event)
          return (
            <li key={event} className="flex items-center gap-2 text-xs">
              <span
                className={cn("grid h-4 w-4 shrink-0 place-items-center rounded-full",
                              done ? "bg-ok/15" : "bg-muted")}
                aria-hidden="true"
              >
                {done ? (
                  <motion.span
                    initial={{ scale: 0.4, opacity: 0 }}
                    animate={{ scale: 1, opacity: 1 }}
                    transition={{ duration: 0.2, ease: EASE }}
                  >
                    <Check className="h-2.5 w-2.5 text-ok" />
                  </motion.span>
                ) : (
                  !halted && (
                    <motion.span
                      className="h-1 w-1 rounded-full bg-faint"
                      animate={{ opacity: [0.3, 1, 0.3] }}
                      transition={{ duration: 1.4, repeat: Infinity, ease: "linear" }}
                    />
                  )
                )}
              </span>
              <span className={done ? "text-foreground" : "text-faint"}>{label}</span>
            </li>
          )
        })}
      </ul>
      <p className="mt-2.5 border-t border-border pt-2 text-2xs text-faint">
        Steps light up from the audit log, not from a timer.
      </p>
    </div>
  )
}
