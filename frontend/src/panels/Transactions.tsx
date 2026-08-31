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
  const [events, setEvents] = useState<Record<string, Json[]>>({})

  async function toggle(requestId: string) {
    if (open === requestId) { setOpen(null); return }
    setOpen(requestId)
    if (!events[requestId]) {
      const res = await api.auditFor(requestId)
      if (res.ok) setEvents(prev => ({ ...prev, [requestId]: res.body.events }))
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
                        <div className="border-t border-border px-3 py-2.5">
                          <dl className="mono mb-2 grid grid-cols-[92px_1fr] gap-x-3 text-muted-foreground">
                            <dt>request</dt><dd>{t.request_id}</dd>
                            <dt>key</dt><dd>{t.idempotency_key ?? "—"}</dd>
                            {t.reason && (<><dt>reason</dt><dd className="text-foreground/80">{t.reason}</dd></>)}
                          </dl>
                          <ul className="grid gap-0.5 border-l border-border pl-3">
                            {(events[t.request_id] ?? []).map(e => (
                              <li key={e.event_id} className="mono grid grid-cols-[190px_96px_1fr] gap-2">
                                <span>{e.event_type}</span>
                                <ActorTag actor={e.actor} />
                                <span className="truncate text-muted-foreground">{e.rule ?? e.reason ?? ""}</span>
                              </li>
                            ))}
                            {!events[t.request_id] && (
                              <li className="mono text-muted-foreground">loading…</li>
                            )}
                          </ul>
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
