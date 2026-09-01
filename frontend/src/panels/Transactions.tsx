import { useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { ChevronRight } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ActorTag } from "@/components/ActorTag"
import { api, type Json } from "@/api"
import { cn, rupees } from "@/lib/utils"

const TONE: Record<string, "ok" | "danger" | "warn" | "default" | "info"> = {
  COMPLETED: "ok", APPROVED: "ok", REPLAYED: "info",
  DENIED: "danger", FAILED: "danger",
  PENDING_VERIFICATION: "warn", DECLINED: "default", IN_PROGRESS: "default",
}

export function Transactions({ transactions }: { transactions: Json[] }) {
  const [open, setOpen] = useState<string | null>(null)
  const [explained, setExplained] = useState<Record<string, Json>>({})

  async function toggle(requestId: string) {
    if (open === requestId) { setOpen(null); return }
    setOpen(requestId)
    if (!explained[requestId]) {
      const res = await api.explain(requestId)
      if (res.ok) setExplained(prev => ({ ...prev, [requestId]: res.body }))
    }
  }

  return (
    <motion.div initial={{ opacity: 0, y: 10 }} animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.32 }}>
      <Card>
        <CardHeader className="flex flex-row items-baseline justify-between">
          <CardTitle>Transactions</CardTitle>
          <span className="mono text-muted-foreground">{transactions.length} shown</span>
        </CardHeader>
        <CardContent>
          {transactions.length === 0 ? (
            <p className="text-[13px] text-muted-foreground">Nothing yet — start a purchase.</p>
          ) : (
            <ul className="grid gap-1.5">
              {transactions.map(t => (
                <li key={t.request_id} className="overflow-hidden rounded-xl border border-border bg-muted/50 transition-colors hover:border-border">
                  <button
                    onClick={() => toggle(t.request_id)}
                    aria-expanded={open === t.request_id}
                    className="flex w-full items-center gap-3 rounded-xl px-3.5 py-3 text-left transition-colors hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <ChevronRight aria-hidden="true"
                      className={cn("h-4 w-4 shrink-0 text-muted-foreground transition-transform",
                        open === t.request_id && "rotate-90")} />
                    <Badge variant={TONE[t.status] ?? "default"}>{t.status.replace(/_/g, " ")}</Badge>
                    <span className="mono min-w-0 flex-1 truncate text-muted-foreground">
                      {t.sku ?? "—"} · {rupees(t.amount_paise)}
                      {t.rule && <span className="text-danger"> · {t.rule}</span>}
                    </span>
                    <span className="mono shrink-0 text-muted-foreground">
                      {new Date(t.updated_at * 1000).toLocaleTimeString("en-GB")}
                    </span>
                  </button>

                  <AnimatePresence initial={false}>
                    {open === t.request_id && (
                      <motion.div
                        initial={{ height: 0, opacity: 0 }}
                        animate={{ height: "auto", opacity: 1 }}
                        exit={{ height: 0, opacity: 0 }}
                        transition={{ duration: 0.24, ease: [0.22, 1, 0.36, 1] }}
                        className="overflow-hidden"
                      >
                        <div className="border-t border-border px-4 py-3">
                          {!explained[t.request_id] ? (
                            <p className="mono text-muted-foreground">loading…</p>
                          ) : (
                            <Explanation data={explained[t.request_id]} />
                          )}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </motion.div>
  )
}


/** WHY / WHAT / EVIDENCE, straight from GET /api/explain/{request_id}. */
function Explanation({ data }: { data: Json }) {
  const { what, why, evidence } = data
  return (
    <div className="grid gap-3">
      <section>
        <h4 className="eyebrow mb-1.5">Why</h4>
        <div className="rounded-md subtle px-3 py-2">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant={why.decision === "APPROVED" ? "ok"
              : why.decision === "DENIED" ? "danger" : "default"}>
              {why.decision}
            </Badge>
            {why.rule && <span className="mono text-danger">{why.rule}</span>}
            {why.decided_by && <ActorTag actor={why.decided_by} />}
          </div>
          {why.reason && <p className="mt-1.5 text-[12.5px]">{why.reason}</p>}
          {Object.keys(why.figures ?? {}).length > 0 && (
            <dl className="mono mt-1.5 grid grid-cols-2 gap-x-4 text-muted-foreground">
              {Object.entries(why.figures as Json).map(([k, v]) => (
                <div key={k} className="flex justify-between gap-3">
                  <dt>{k.replace(/_/g, " ")}</dt>
                  <dd className="tabular-nums text-foreground">{String(v)}</dd>
                </div>
              ))}
            </dl>
          )}
          {why.human_confirmed && (
            <p className="mt-1.5 text-[11.5px] text-muted-foreground">
              A human confirmed this. The decision above was made separately.
            </p>
          )}
        </div>
      </section>

      <section>
        <h4 className="eyebrow mb-1.5">What</h4>
        <dl className="mono grid grid-cols-[104px_1fr] gap-x-3 rounded-md subtle px-3 py-2 text-muted-foreground">
          <dt>agent</dt><dd className="text-foreground">{what.agent_id}</dd>
          <dt>item</dt><dd className="text-foreground">{what.sku ?? "—"}</dd>
          <dt>amount</dt><dd className="text-foreground">{rupees(what.amount_paise)}</dd>
          <dt>key</dt><dd className="truncate text-foreground">{what.idempotency_key ?? "—"}</dd>
          <dt>money moved</dt><dd className="text-foreground">{what.money_moved ? "yes" : "no"}</dd>
        </dl>
      </section>

      <section>
        <h4 className="eyebrow mb-1.5">Evidence</h4>
        <ul className="grid gap-0.5 border-l border-border pl-3">
          {(evidence as Json[]).map(e => (
            <li key={e.event_id} className="mono grid grid-cols-[188px_92px_1fr] gap-2">
              <span>{e.event_type}</span>
              <ActorTag actor={e.actor} />
              <span className="truncate text-muted-foreground">
                {e.narrative ?? e.rule ?? e.reason ?? ""}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
