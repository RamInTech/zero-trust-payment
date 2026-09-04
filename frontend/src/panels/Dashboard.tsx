import { useEffect, useState } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { Badge } from "@/components/ui/badge"
import { Row, Rows, Stat } from "@/components/ui/rows"
import { AnimatedNumber } from "@/components/AnimatedNumber"
import { VelocityDots } from "@/components/VelocityDots"
import { ActorTag } from "@/components/ActorTag"
import { rupees } from "@/lib/utils"
import type { Json } from "@/api"

export function Dashboard({
  mandate, stats, audit, sweep, agent, onChanged,
}: {
  mandate: Json | null; stats: Json | null; audit: Json[]; sweep: Json | null
  agent: string; onChanged: () => void
}) {
  const denials = (stats?.denials ?? {}) as Record<string, number>
  const expiryHours = mandate ? Math.floor(mandate.seconds_until_expiry / 3600) : 0
  const expiryMins = mandate ? Math.floor((mandate.seconds_until_expiry % 3600) / 60) : 0
  const pending = (stats?.pending_verification ?? 0) as number

  const decisions = audit
    .filter(e => e.event_type === "POLICY_APPROVED" || e.event_type === "POLICY_DENIED")
    .slice(-9).reverse()

  return (
    <div className="grid gap-4">
      {/* An unauthenticated dashboard should say so on its face. */}
      <p className="rounded-lg border border-border bg-card px-4 py-2.5 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Demo — no authentication.</span>{" "}
        Mandate internals and keys are exposed deliberately.
      </p>

      <Card>
        <div className="grid divide-y divide-border sm:grid-cols-2 sm:divide-y-0 lg:grid-cols-4 lg:divide-x">
          <div className="px-5 py-4">
            <Stat label="Purchases executed"
                  value={<AnimatedNumber value={stats?.purchases ?? 0} />} />
          </div>
          <div className="px-5 py-4">
            <Stat label="Total spend"
                  value={<AnimatedNumber value={stats?.spend_paise ?? 0} format={rupees} />} />
          </div>
          <div className="px-5 py-4">
            <Stat label="Replays served"
                  value={<AnimatedNumber value={stats?.replays ?? 0} />}
                  hint="charged once, answered twice" />
          </div>
          <div className="px-5 py-4">
            <Stat label="Awaiting verification"
                  value={<AnimatedNumber value={pending} />}
                  hint={pending > 0 ? "outcome unknown" : "none outstanding"} />
          </div>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Mandate in force</CardTitle>
            {mandate && (
              <Badge variant={mandate.expired ? "danger" : "ok"} dot>
                {mandate.expired ? "Expired" : "Active"}
              </Badge>
            )}
          </CardHeader>
          <CardContent className="grid gap-4">
            {!mandate ? (
              <p className="text-sm text-muted-foreground">No mandate for this agent.</p>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <CapControl mandate={mandate} agent={agent} onChanged={onChanged} />
                  <Stat label="Expires in"
                        value={mandate.expired ? "Expired" : `${expiryHours}h ${expiryMins}m`} />
                </div>

                <div className="border-t border-border pt-3.5">
                  <div className="label mb-2">Velocity</div>
                  <VelocityDots used={mandate.velocity_used} limit={mandate.velocity_limit} />
                </div>

                <div className="border-t border-border pt-3.5">
                  <div className="label mb-2">Allowed items</div>
                  {mandate.allows_any_sku ? (
                    <p className="text-xs text-muted-foreground">
                      Any item in the catalog. The per-transaction limit is what
                      refuses a purchase, not a fixed list — so anything stocked
                      later is covered without reissuing the mandate.
                    </p>
                  ) : (
                    <div className="flex flex-wrap gap-1.5">
                      {mandate.allowed_skus.map((s: string) => (
                        <span key={s} className="mono rounded bg-muted px-1.5 py-0.5 text-foreground">
                          {s}
                        </span>
                      ))}
                    </div>
                  )}
                </div>

                {mandate.cooldown_denials > 0 && (
                  <div className="border-t border-border pt-3.5">
                    <div className="label mb-1.5">Denial cool-down</div>
                    <p className="text-xs text-muted-foreground">
                      Throttled after {mandate.cooldown_denials} denials in{" "}
                      {Math.round(mandate.cooldown_window_secs / 60)} minutes.
                    </p>
                  </div>
                )}
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Reconciliation sweep</CardTitle>
            {sweep && (
              <Badge variant={sweep.running ? "ok" : "default"} dot={!!sweep.running}>
                {sweep.running ? `Every ${Math.round(sweep.interval_seconds)}s` : "Stopped"}
              </Badge>
            )}
          </CardHeader>
          <CardContent>
            {!sweep ? (
              <p className="text-sm text-muted-foreground">Not running.</p>
            ) : (
              <Rows>
                <Row label="Cycles" value={sweep.cycles} />
                <Row label="Errors" value={sweep.errors}
                     tone={sweep.errors > 0 ? "danger" : undefined} />
                {Object.entries(sweep.records_resolved ?? {}).map(([k, v]) => (
                  <Row key={k} label={k.toLowerCase().replace(/_/g, " ")} value={String(v)} />
                ))}
              </Rows>
            )}
          </CardContent>
        </Card>
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader><CardTitle>Recent decisions</CardTitle></CardHeader>
          {decisions.length === 0 ? (
            <CardContent>
              <p className="text-sm text-muted-foreground">No decisions yet.</p>
            </CardContent>
          ) : (
            <table className="w-full table-fixed text-left">
              <thead>
                <tr className="border-b border-border">
                  <th className="label w-[92px] px-5 py-2 font-medium">Time</th>
                  <th className="label w-[168px] px-5 py-2 font-medium">Outcome</th>
                  <th className="label w-[140px] px-5 py-2 font-medium">Decided by</th>
                  <th className="label px-5 py-2 font-medium">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {decisions.map(e => (
                  <tr key={e.event_id}>
                    <td className="mono whitespace-nowrap px-5 py-2 text-muted-foreground">
                      {new Date(e.occurred_at * 1000).toLocaleTimeString("en-GB")}
                    </td>
                    <td className="px-5 py-2">
                      <Badge variant={e.event_type === "POLICY_APPROVED" ? "ok" : "danger"}>
                        {e.event_type === "POLICY_APPROVED" ? "Approved" : e.rule}
                      </Badge>
                    </td>
                    <td className="px-5 py-2"><ActorTag actor={e.actor} /></td>
                    <td className="truncate px-5 py-2 text-xs text-muted-foreground">
                      {e.reason}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card>
          <CardHeader><CardTitle>Denials by rule</CardTitle></CardHeader>
          <CardContent>
            {Object.keys(denials).length === 0 ? (
              <p className="text-sm text-muted-foreground">Nothing denied yet.</p>
            ) : (
              <ul className="grid gap-3">
                {Object.entries(denials).sort((a, b) => b[1] - a[1]).map(([rule, count]) => {
                  const max = Math.max(...Object.values(denials))
                  return (
                    <li key={rule}>
                      <div className="flex items-baseline justify-between gap-3">
                        <span className="mono truncate text-foreground">{rule}</span>
                        <span className="mono shrink-0 text-muted-foreground">{count}</span>
                      </div>
                      <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-muted">
                        <div className="h-full rounded-full bg-danger/60"
                             style={{ width: `${(count / max) * 100}%` }} />
                      </div>
                    </li>
                  )
                })}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}


/**
 * The per-transaction limit, editable.
 *
 * Issuing a mandate is a MERCHANT action -- it is the merchant stating how
 * much they are willing to let an agent spend -- which is why a control for it
 * belongs on this page at all. The agent has no route to it. Nothing here lets
 * an agent raise its own ceiling; that would invert the whole model.
 */
function CapControl({ mandate, agent, onChanged }: {
  mandate: Json; agent: string; onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [rupeeValue, setRupeeValue] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setRupeeValue(String(mandate.max_amount_paise / 100))
  }, [mandate.max_amount_paise])

  async function save() {
    const rupeesTyped = Number(rupeeValue)
    if (!Number.isFinite(rupeesTyped) || rupeesTyped <= 0) {
      setError("Enter an amount greater than zero.")
      return
    }
    setSaving(true); setError(null)
    const res = await api.setCap(agent, Math.round(rupeesTyped * 100))
    setSaving(false)
    if (!res.ok) {
      setError(res.body?.detail?.reason ?? "Could not update the limit.")
      return
    }
    setEditing(false)
    onChanged()
  }

  if (!editing) {
    return (
      <div>
        <div className="label mb-1">Max per transaction</div>
        <div className="flex items-baseline gap-2">
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {rupees(mandate.max_amount_paise)}
          </span>
          <button
            onClick={() => { setEditing(true); setError(null) }}
            className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring"
          >
            Change
          </button>
        </div>
        <p className="mt-1 text-2xs text-faint">Set by the merchant, never by the agent.</p>
      </div>
    )
  }

  return (
    <div>
      <label htmlFor="cap-input" className="label mb-1 block">Max per transaction (₹)</label>
      <div className="flex items-center gap-2">
        <input
          id="cap-input" type="number" min="1" step="1" value={rupeeValue}
          autoFocus
          onChange={e => setRupeeValue(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter") save()
            if (e.key === "Escape") { setEditing(false); setError(null) }
          }}
          className="w-28 rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button size="sm" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="outline"
                onClick={() => { setEditing(false); setError(null) }}>
          Cancel
        </Button>
      </div>
      {error
        ? <p className="mt-1 text-2xs text-danger">{error}</p>
        : <p className="mt-1 text-2xs text-faint">
            Replaces the mandate; the old one is revoked, not edited.
          </p>}
    </div>
  )
}
