import { motion } from "framer-motion"
import { ShieldAlert } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { AnimatedNumber } from "@/components/AnimatedNumber"
import { VelocityDots } from "@/components/VelocityDots"
import { ActorTag } from "@/components/ActorTag"
import { rupees } from "@/lib/utils"
import type { Json } from "@/api"

/** Children enter in sequence rather than all at once. */
const container = {
  hidden: {},
  show: { transition: { staggerChildren: 0.055, delayChildren: 0.04 } },
}
const item = {
  hidden: { opacity: 0, y: 14 },
  show: { opacity: 1, y: 0, transition: { duration: 0.42, ease: [0.22, 1, 0.36, 1] as const } },
}

function Stat({ label, value, hint, accent }: {
  label: string; value: React.ReactNode; hint?: string; accent?: boolean
}) {
  return (
    <div className={`rounded-md border px-4 py-3.5 ${
      accent ? "border-primary/25 bg-primary/[0.04]" : "border-border bg-muted/40"
    }`}>
      <div className="text-[10px] font-medium uppercase tracking-[0.13em] text-muted-foreground">{label}</div>
      <div className="display mt-1.5">{value}</div>
      {hint && <div className="mt-1 text-[11.5px] leading-snug text-muted-foreground/80">{hint}</div>}
    </div>
  )
}

export function Dashboard({
  mandate, stats, audit, sweep,
}: { mandate: Json | null; stats: Json | null; audit: Json[]; sweep: Json | null }) {
  const denials = (stats?.denials ?? {}) as Record<string, number>
  const expiryHours = mandate ? Math.floor(mandate.seconds_until_expiry / 3600) : 0
  const expiryMins = mandate ? Math.floor((mandate.seconds_until_expiry % 3600) / 60) : 0

  return (
    <motion.div variants={container} initial="hidden" animate="show"
                className="grid gap-5 lg:grid-cols-3">
      {/* An unauthenticated dashboard should say so on its face. */}
      <motion.div variants={item} className="lg:col-span-3">
        <div className="flex items-start gap-3 rounded-md border border-warn/30 bg-warn/[0.06] px-4 py-3">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-warn" aria-hidden="true" />
          <p className="text-[12.5px] text-foreground/90">
            <strong className="font-semibold">No authentication.</strong> Demo
            dashboard — mandate internals and keys are exposed deliberately.
          </p>
        </div>
      </motion.div>

      <motion.div variants={item} className="lg:col-span-2">
      <Card className="h-full">
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Mandate in force</CardTitle>
          {mandate && (
            <Badge variant={mandate.expired ? "danger" : "ok"}>
              {mandate.expired ? "expired" : "active"}
            </Badge>
          )}
        </CardHeader>
        <CardContent>
          {!mandate ? (
            <p className="text-[13px] text-muted-foreground">No mandate for this agent.</p>
          ) : (
            <div className="grid gap-4 sm:grid-cols-2">
              <Stat label="Max per transaction" value={rupees(mandate.max_amount_paise)} accent />
              <Stat label="Expires" value={mandate.expired ? "expired" : `${expiryHours}h ${expiryMins}m`} />
              {mandate.cooldown_denials > 0 && (
                <div className="sm:col-span-2 rounded-md border border-border bg-muted/40 px-4 py-3">
                  <div className="text-[10px] font-medium uppercase tracking-[0.13em] text-muted-foreground">
                    Denial cool-down
                  </div>
                  <p className="mono mt-1 text-muted-foreground">
                    throttled after {mandate.cooldown_denials} denials in{" "}
                    {Math.round(mandate.cooldown_window_secs / 60)} min
                  </p>
                </div>
              )}
              <div className="sm:col-span-2 rounded-md border border-border bg-muted/50 px-4 py-3">
                <div className="text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground">Velocity</div>
                <div className="mt-2"><VelocityDots used={mandate.velocity_used} limit={mandate.velocity_limit} /></div>
              </div>
              <div className="sm:col-span-2 rounded-md border border-border bg-muted/50 px-4 py-3">
                <div className="text-[10.5px] uppercase tracking-[0.08em] text-muted-foreground">Allowed items</div>
                <div className="mono mt-1.5 flex flex-wrap gap-1.5">
                  {mandate.allowed_skus.map((s: string) => (
                    <span key={s} className="rounded border border-border bg-card px-2 py-0.5 text-foreground">{s}</span>
                  ))}
                </div>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
      </motion.div>

      <motion.div variants={item}>
      <Card className="h-full">
        <CardHeader><CardTitle>Activity</CardTitle></CardHeader>
        <CardContent className="grid gap-3">
          <Stat label="Purchases executed" value={<AnimatedNumber value={stats?.purchases ?? 0} />} />
          <Stat label="Total spend" value={rupees(stats?.spend_paise ?? 0)} accent />
          <Stat label="Replays served" value={<AnimatedNumber value={stats?.replays ?? 0} />} />
          {(stats?.pending_verification ?? 0) > 0 && (
            <Stat label="Awaiting verification"
                  value={<AnimatedNumber value={stats?.pending_verification ?? 0} />}
                  hint="outcome unknown" />
          )}
        </CardContent>
      </Card>
      </motion.div>

      <motion.div variants={item}>
      <Card className="h-full">
        <CardHeader><CardTitle>Denials by rule</CardTitle></CardHeader>
        <CardContent>
          {Object.keys(denials).length === 0 ? (
            <p className="text-[13px] text-muted-foreground">Nothing denied yet.</p>
          ) : (
            <ul className="grid gap-2.5">
              {Object.entries(denials).sort((a, b) => b[1] - a[1]).map(([rule, count]) => {
                const max = Math.max(...Object.values(denials))
                return (
                  <li key={rule}>
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="mono text-danger">{rule}</span>
                      <span className="mono tabular-nums text-muted-foreground">{count}</span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-muted">
                      <motion.div
                        initial={{ width: 0 }}
                        animate={{ width: `${(count / max) * 100}%` }}
                        transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
                        className="h-full rounded-full bg-danger/70"
                      />
                    </div>
                  </li>
                )
              })}
            </ul>
          )}
        </CardContent>
      </Card>
      </motion.div>

      <motion.div variants={item}>
      <Card className="h-full">
        <CardHeader><CardTitle>Reconciliation sweep</CardTitle></CardHeader>
        <CardContent className="grid gap-2 text-[12.5px]">
          {!sweep ? (
            <p className="text-muted-foreground">not running</p>
          ) : (
            <>
              <div className="flex items-center justify-between">
                <span className="text-muted-foreground">status</span>
                <Badge variant={sweep.running ? "ok" : "default"} dot={sweep.running}>
                  {sweep.running ? `every ${Math.round(sweep.interval_seconds)}s` : "stopped"}
                </Badge>
              </div>
              <div className="mono flex justify-between text-muted-foreground">
                <span>cycles</span><span className="tabular-nums text-foreground">{sweep.cycles}</span>
              </div>
              <div className="mono flex justify-between text-muted-foreground">
                <span>errors</span><span className="tabular-nums text-foreground">{sweep.errors}</span>
              </div>
              {Object.entries(sweep.records_resolved ?? {}).map(([k, v]) => (
                <div key={k} className="mono flex justify-between text-muted-foreground">
                  <span>{k.toLowerCase().replace(/_/g, " ")}</span>
                  <span className="tabular-nums text-foreground">{String(v)}</span>
                </div>
              ))}
            </>
          )}
        </CardContent>
      </Card>
      </motion.div>

      <motion.div variants={item} className="lg:col-span-2">
      <Card className="h-full">
        <CardHeader><CardTitle>Recent decisions</CardTitle></CardHeader>
        <CardContent>
          <ul className="grid gap-1.5" aria-label="Recent decisions">
            {audit.filter(e => e.event_type === "POLICY_APPROVED" || e.event_type === "POLICY_DENIED")
              .slice(-8).reverse().map(e => (
              <li key={e.event_id} className="flex items-baseline gap-3 border-b border-border pb-1.5 last:border-0">
                <span className="mono w-16 shrink-0 text-muted-foreground">
                  {new Date(e.occurred_at * 1000).toLocaleTimeString("en-GB")}
                </span>
                <Badge variant={e.event_type === "POLICY_APPROVED" ? "ok" : "danger"}>
                  {e.event_type === "POLICY_APPROVED" ? "approved" : e.rule}
                </Badge>
                <ActorTag actor={e.actor} />
                <span className="truncate text-[12.5px] text-muted-foreground">{e.reason}</span>
              </li>
            ))}
            {audit.length === 0 && <li className="text-[13px] text-muted-foreground">No decisions yet.</li>}
          </ul>
        </CardContent>
      </Card>
      </motion.div>
    </motion.div>
  )
}
