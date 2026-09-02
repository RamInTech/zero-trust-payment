import { useState } from "react"
import { ChevronRight } from "lucide-react"
import { Card, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Row, Rows } from "@/components/ui/rows"
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
    <Card>
      <CardHeader>
        <CardTitle>Transactions</CardTitle>
        <span className="text-xs text-muted-foreground">{transactions.length} shown</span>
      </CardHeader>

      {transactions.length === 0 ? (
        <p className="px-5 py-4 text-sm text-muted-foreground">Nothing yet — start a purchase.</p>
      ) : (
        <>
          <div className="grid grid-cols-[20px_128px_1fr_auto] items-center gap-3 border-b border-border px-5 py-2">
            <span aria-hidden="true" />
            <span className="label">Status</span>
            <span className="label">Item</span>
            <span className="label">Updated</span>
          </div>
          <ul className="divide-rows">
            {transactions.map(t => (
              <li key={t.request_id}>
                <button
                  onClick={() => toggle(t.request_id)}
                  aria-expanded={open === t.request_id}
                  className="grid w-full grid-cols-[20px_128px_1fr_auto] items-center gap-3 px-5 py-2.5 text-left transition-colors hover:bg-muted/60 focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                >
                  <ChevronRight aria-hidden="true"
                    className={cn("h-4 w-4 text-faint transition-transform",
                      open === t.request_id && "rotate-90")} />
                  <span>
                    <Badge variant={TONE[t.status] ?? "default"}>
                      {t.status.replace(/_/g, " ").toLowerCase()}
                    </Badge>
                  </span>
                  <span className="mono min-w-0 truncate text-foreground">
                    {t.sku ?? "—"} · {rupees(t.amount_paise)}
                    {t.rule && <span className="text-danger"> · {t.rule}</span>}
                  </span>
                  <span className="mono shrink-0 text-muted-foreground">
                    {new Date(t.updated_at * 1000).toLocaleTimeString("en-GB")}
                  </span>
                </button>

                {open === t.request_id && (
                  <div className="border-t border-border bg-muted/40 px-5 py-4">
                    {!explained[t.request_id] ? (
                      <p className="text-xs text-muted-foreground">Loading…</p>
                    ) : (
                      <Explanation data={explained[t.request_id]} />
                    )}
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </Card>
  )
}

/** WHY / WHAT / EVIDENCE, straight from GET /api/explain/{request_id}. */
function Explanation({ data }: { data: Json }) {
  const { what, why, evidence } = data
  return (
    <div className="grid gap-5 md:grid-cols-2">
      <section>
        <h4 className="label mb-2">Why</h4>
        <div className="flex flex-wrap items-center gap-2">
          <Badge variant={why.decision === "APPROVED" ? "ok"
            : why.decision === "DENIED" ? "danger" : "default"}>
            {why.decision.toLowerCase()}
          </Badge>
          {why.rule && <span className="mono text-danger">{why.rule}</span>}
          {why.decided_by && <ActorTag actor={why.decided_by} />}
        </div>
        {why.reason && <p className="mt-2 text-xs text-foreground">{why.reason}</p>}
        {Object.keys(why.figures ?? {}).length > 0 && (
          <Rows className="mt-2 border-t border-border pt-2">
            {Object.entries(why.figures as Json).map(([k, v]) => (
              <Row key={k} label={k.replace(/_/g, " ")} value={String(v)} />
            ))}
          </Rows>
        )}
        {why.human_confirmed && (
          <p className="mt-2 text-2xs text-muted-foreground">
            A human confirmed this. The decision above was made separately.
          </p>
        )}
      </section>

      <section>
        <h4 className="label mb-2">What</h4>
        <Rows>
          <Row label="Agent" value={what.agent_id} />
          <Row label="Item" value={what.sku ?? "—"} />
          <Row label="Amount" value={rupees(what.amount_paise)} />
          <Row label="Key" value={
            <span className="block max-w-[220px] truncate">{what.idempotency_key ?? "—"}</span>
          } />
          <Row label="Money moved" value={what.money_moved ? "Yes" : "No"} />
        </Rows>
      </section>

      <section className="md:col-span-2">
        <h4 className="label mb-2">Evidence</h4>
        <ul className="divide-rows border-t border-border">
          {(evidence as Json[]).map(e => (
            <li key={e.event_id}
                className="grid grid-cols-[minmax(0,190px)_92px_minmax(0,1fr)] gap-3 py-1.5">
              <span className="mono truncate text-foreground">{e.event_type}</span>
              <ActorTag actor={e.actor} />
              <span className="truncate text-xs text-muted-foreground">
                {e.narrative ?? e.rule ?? e.reason ?? ""}
              </span>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
