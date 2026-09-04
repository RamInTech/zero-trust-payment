import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Row, Rows } from "@/components/ui/rows"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { api, type Json } from "@/api"
import { DoubleChargeRace } from "@/components/DoubleChargeRace"
import { MandateOverride } from "@/components/hub/MandateOverride"
import { VelocityBurst } from "@/components/hub/VelocityBurst"
import { TamperTimeline } from "@/components/hub/TamperTimeline"
import { PriceSwap } from "@/components/hub/PriceSwap"
import { UnknownOutcomeTimeline } from "@/components/hub/UnknownOutcomeTimeline"
import { InjectionAttempt } from "@/components/hub/InjectionAttempt"
import { CiphertextReveal } from "@/components/hub/CiphertextReveal"
import { KillSwitch } from "@/components/hub/KillSwitch"
import { cn } from "@/lib/utils"

/**
 * Every panel here is backed by a mechanism that exists. The three layers a
 * reader might expect but that this system does NOT implement are listed
 * separately and honestly, rather than rendered as though they were real --
 * an animated indicator for a protection you do not have is theatre, and
 * theatre is what this page exists to avoid.
 *
 * Every card that CAN run a live proof does, rather than printing a sentence
 * about one: a button that says "denied — AMOUNT_EXCEEDS_CAP" is a claim, and
 * a claim drawn by the system you're being asked to trust is not evidence.
 * Watching the collision happen -- two requests racing, a price changing
 * underneath a pending purchase, raw SQL aborting against a live database --
 * is. Each component below owns its own real HTTP calls against a throwaway
 * agent; nothing here is scripted or replayed.
 */

export function SecurityHub({ agent, layers, adversarial, onChanged }: {
  agent: string; layers: Json | null; adversarial: Json | null; onChanged: () => void
}) {
  /**
   * Each demonstration takes a throwaway agent with its own mandate.
   * Sharing one agent meant sharing a velocity budget, so a proof run later
   * measured a budget the earlier ones had already spent -- and reported the
   * wrong result while looking fine. Same failure as JOURNAL.md Entry 7,
   * same fix.
   */
  const freshAgent = async () => {
    const res = await api.freshAgent()
    return res.ok ? (res.body.agent_id as string) : agent
  }

  //: One live component per mechanism. Keyed by the same `id` the backend
  //: reports in `/demo/security/layers`, so a mechanism added there without
  //: a matching entry here is immediately visible as a gap, not silently
  //: falling back to nothing.
  const demonstrations: Record<string, () => React.JSX.Element> = {
    exactly_once: () => <DoubleChargeRace freshAgent={freshAgent} onChanged={onChanged} />,
    mandate: () => <MandateOverride freshAgent={freshAgent} onChanged={onChanged} />,
    confirmation: () => <VelocityBurst freshAgent={freshAgent} onChanged={onChanged} />,
    append_only_audit: () => <TamperTimeline onChanged={onChanged} />,
    price_revalidation: () => <PriceSwap freshAgent={freshAgent} onChanged={onChanged} />,
    unknown_outcomes: () => <UnknownOutcomeTimeline freshAgent={freshAgent} onChanged={onChanged} />,
    llm_no_authority: () => <InjectionAttempt freshAgent={freshAgent} onChanged={onChanged} />,
    e2e_chat_encryption: () => <CiphertextReveal freshAgent={freshAgent} onChanged={onChanged} />,
    instant_revocation: () => <KillSwitch freshAgent={freshAgent} onChanged={onChanged} />,
  }

  const cards = (layers?.implemented ?? []) as Json[]
  const totals = adversarial?.totals

  return (
    <div className="grid gap-4">
      <p className="rounded-lg border border-border bg-card px-4 py-2.5 text-xs text-muted-foreground">
        <span className="font-medium text-foreground">Each protection below is implemented.</span>{" "}
        Every one can be made to refuse something now.
      </p>

      <div className="grid gap-4 md:grid-cols-2">
        {cards.map(layer => {
          const Demonstration = demonstrations[layer.id]
          return (
            <Card key={layer.id} className="flex flex-col">
              <CardHeader>
                <CardTitle>{layer.title}</CardTitle>
                <Badge variant="ok" dot>Enforced</Badge>
              </CardHeader>
              <CardContent className="flex flex-1 flex-col gap-3.5">
                <p className="text-xs leading-relaxed text-muted-foreground">
                  {layer.mechanism}
                </p>

                {/* What the mechanism does NOT cover, shown beside what it
                    does. A caveat that lives only in the API response is a
                    caveat nobody reads. */}
                {layer.boundary && (
                  <p className="border-l-2 border-warn/40 pl-2.5 text-2xs leading-relaxed text-faint">
                    <span className="font-medium text-muted-foreground">Limits: </span>
                    {layer.boundary}
                  </p>
                )}

                <Rows className="border-t border-border pt-2.5">
                  {Object.entries(layer.evidence as Json).slice(0, 4).map(([k, v]) => (
                    <Row
                      key={k}
                      label={k.replace(/_/g, " ")}
                      value={
                        Array.isArray(v) ? v.length
                          : typeof v === "boolean" ? (v ? "yes" : "no")
                          : v !== null && typeof v === "object" ? Object.keys(v).length
                          : String(v)
                      }
                    />
                  ))}
                </Rows>

                <div className="mt-auto">
                  {Demonstration ? <Demonstration /> : (
                    <p className="text-2xs text-faint">
                      No live demonstration wired up for this mechanism yet.
                    </p>
                  )}
                </div>
              </CardContent>
            </Card>
          )
        })}
      </div>

      {adversarial && (
        <Card>
          <CardHeader>
            <div>
              <CardTitle>Adversarial suite</CardTitle>
              <p className="mt-0.5 text-xs text-muted-foreground">
                Generated by <span className="mono">scripts/run_adversarial_suite.py</span>
              </p>
            </div>
            <div className="shrink-0 text-right">
              <div className="text-sm font-semibold tabular-nums text-foreground">
                {totals.defended} / {totals.attacks} defended
              </div>
              <div className="text-xs text-muted-foreground">
                {totals.unintended_charges} unintended charges
              </div>
            </div>
          </CardHeader>
          <ul className="divide-rows">
            {(adversarial.attacks as Json[]).map((a, i) => (
              <li key={a.name}
                  className="grid grid-cols-[28px_1fr_auto] items-center gap-3 px-5 py-2">
                <span className="mono text-faint">{String(i + 1).padStart(2, "0")}</span>
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="cursor-help truncate text-xs text-foreground">{a.attack}</span>
                  </TooltipTrigger>
                  <TooltipContent>{a.defence}</TooltipContent>
                </Tooltip>
                <span className={cn("flex items-center gap-1.5 text-xs",
                  a.status === "DEFENDED" ? "text-ok" : "text-danger")}>
                  <span className="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true" />
                  {a.status.toLowerCase()}
                </span>
              </li>
            ))}
          </ul>
        </Card>
      )}

      <Card>
        <CardHeader>
          <div>
            <CardTitle>Not implemented — stated rather than implied</CardTitle>
            <p className="mt-0.5 text-xs text-muted-foreground">
              Commonly expected, and absent here.
            </p>
          </div>
        </CardHeader>
        <CardContent>
          <ul className="grid gap-3 sm:grid-cols-3">
            {((layers?.not_implemented ?? []) as Json[]).map(item => (
              <li key={item.id}>
                <p className="text-xs font-medium text-muted-foreground">{item.title}</p>
                <p className="mt-0.5 text-xs text-faint">{item.note}</p>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  )
}
