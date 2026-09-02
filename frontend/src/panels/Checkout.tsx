import { useEffect, useRef, useState } from "react"
import { Check, X } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { Row, Rows } from "@/components/ui/rows"
import { api, type ConfirmResult, type Json, type Pending } from "@/api"
import { cn, rupees } from "@/lib/utils"

type Step = "browse" | "draft" | "result"

const STEPS: { id: Step; label: string }[] = [
  { id: "browse", label: "Choose" },
  { id: "draft", label: "Confirm" },
  { id: "result", label: "Result" },
]

export function Checkout({ agent, catalog, onChanged }: {
  agent: string; catalog: Json[]; onChanged: () => void
}) {
  const [step, setStep] = useState<Step>("browse")
  const [text, setText] = useState("buy me some filter coffee")
  const [pending, setPending] = useState<Pending | null>(null)
  const [result, setResult] = useState<ConfirmResult | null>(null)
  const [error, setError] = useState<{ code: string; reason: string } | null>(null)
  const [busy, setBusy] = useState(false)
  const headingRef = useRef<HTMLHeadingElement>(null)

  // Move focus with the step, or keyboard users are stranded behind it.
  useEffect(() => { headingRef.current?.focus() }, [step])

  async function propose(fromSku?: string) {
    setBusy(true); setError(null)
    const res = fromSku
      ? await api.intentFromSku(agent, fromSku)
      : await api.intentFromText(agent, text)
    setBusy(false)
    if (!res.ok) {
      setError(res.body.detail ?? { code: "ERROR", reason: "request failed" })
      onChanged()
      return
    }
    const draft: Pending = res.body.awaiting_confirmation
    const keyed = await api.pending(draft.request_id)
    if (keyed.ok) draft.idempotency_key = keyed.body.idempotency_key
    setPending(draft); setResult(null); setStep("draft")
    onChanged()
  }

  async function confirm() {
    if (!pending) return
    setBusy(true); setError(null)
    const res = await api.confirm(pending.request_id)
    setBusy(false)
    if (res.status === 409 || res.status === 410 || res.status === 404 || res.status === 503) {
      setError(res.body.detail); setStep("result"); onChanged(); return
    }
    setResult(res.body as ConfirmResult); setStep("result"); onChanged()
  }

  async function decline() {
    if (!pending) return
    await api.decline(pending.request_id)
    setResult(null)
    setError({ code: "DECLINED", reason: "You declined. No policy check, no execution, no charge." })
    setStep("result"); onChanged()
  }

  const stepIndex = STEPS.findIndex(s => s.id === step)

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
      <Card>
        <CardHeader>
          <CardTitle>Purchase flow</CardTitle>
          <ol className="flex items-center gap-2" aria-label="Progress">
            {STEPS.map((s, i) => (
              <li key={s.id} className="flex items-center gap-2">
                <span
                  aria-current={step === s.id ? "step" : undefined}
                  className={cn(
                    "text-xs",
                    i < stepIndex && "text-muted-foreground",
                    i === stepIndex && "font-medium text-primary",
                    i > stepIndex && "text-faint"
                  )}
                >
                  {s.label}
                </span>
                {i < STEPS.length - 1 && (
                  <span className="h-px w-5 bg-border" aria-hidden="true" />
                )}
              </li>
            ))}
          </ol>
        </CardHeader>
        <CardContent>
          <h3 ref={headingRef} tabIndex={-1} className="sr-only">
            Step {stepIndex + 1}: {STEPS[stepIndex]?.label}
          </h3>

          {step === "browse" && (
            <div className="grid gap-5">
              <div>
                <label htmlFor="intent" className="label">Describe what you want</label>
                <div className="mt-1.5 flex gap-2">
                  <input id="intent" value={text} onChange={e => setText(e.target.value)}
                    onKeyDown={e => e.key === "Enter" && propose()}
                    className="h-9 flex-1 rounded-md border border-input bg-card px-3 text-sm transition-colors placeholder:text-faint focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-ring" />
                  <Button onClick={() => propose()} disabled={busy}>Propose</Button>
                </div>
              </div>

              <div>
                <div className="label mb-1.5">Or select an item</div>
                <ul className="divide-rows overflow-hidden rounded-md border border-border">
                  {catalog.map(item => (
                    <li key={item.sku}>
                      <button
                        onClick={() => propose(item.sku)} disabled={busy}
                        className="flex w-full items-center justify-between gap-4 px-3.5 py-2.5 text-left transition-colors hover:bg-muted focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring disabled:opacity-50"
                      >
                        <span className="min-w-0">
                          <span className="block truncate text-sm text-foreground">{item.name}</span>
                          <span className="mono text-faint">{item.sku}</span>
                        </span>
                        <span className="mono shrink-0 text-foreground">
                          {rupees(item.price_paise)}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}

          {step === "draft" && pending && (
            <div>
              <Badge variant="warn">Proposed by the agent — not approved</Badge>
              <p className="mt-3 text-lg font-semibold text-foreground">{pending.prompt}</p>
              <Rows className="mt-3 border-t border-border pt-2.5">
                <Row label="Parsed by" value={pending.parser} />
                <Row label="Request" value={pending.request_id} />
                <Row label="Idempotency key" value={pending.idempotency_key ?? "—"} />
              </Rows>
              <div className="mt-4 flex gap-2">
                <Button onClick={confirm} disabled={busy}>
                  <Check className="h-4 w-4" aria-hidden="true" />Confirm
                </Button>
                <Button variant="outline" onClick={decline} disabled={busy}>
                  <X className="h-4 w-4" aria-hidden="true" />Decline
                </Button>
              </div>
              <p className="mt-3 text-xs text-muted-foreground">Not authorised yet.</p>
            </div>
          )}

          {step === "result" && (
            <div>
              <div aria-live="polite">
                {error ? (
                  <>
                    <Badge variant={error.code === "PENDING_VERIFICATION" ? "warn" : "danger"}>
                      {error.code}
                    </Badge>
                    <p className="mt-2.5 text-sm text-foreground">{error.reason}</p>
                    {(error as Json).guidance && (
                      <p className="mt-1.5 text-xs text-muted-foreground">
                        {(error as Json).guidance}
                      </p>
                    )}
                  </>
                ) : result?.approved ? (
                  <>
                    <Badge variant="ok" dot>Approved — {result.idempotency_outcome}</Badge>
                    <ul className="mt-3 grid gap-1.5">
                      {[
                        "Amount within the per-transaction cap",
                        "Item on the mandate allowlist",
                        "Mandate not expired",
                        "Velocity slot claimed",
                      ].map(check => (
                        <li key={check} className="flex items-center gap-2 text-xs text-muted-foreground">
                          <Check className="h-3.5 w-3.5 shrink-0 text-ok" aria-hidden="true" />
                          {check}
                        </li>
                      ))}
                    </ul>
                    <Rows className="mt-3 border-t border-border pt-2.5">
                      <Row label="Confirmed by" value="HUMAN" />
                      <Row label="Decided by" value="POLICY_ENGINE" tone="ok" />
                      <Row label="Order" value={result.response?.order_id ?? "—"} />
                      <Row label="Money moved"
                           value={result.executed ? "Yes, once" : "No — replayed"} />
                    </Rows>
                    <p className="mt-3 text-xs text-warn">Capture is simulated.</p>
                  </>
                ) : result ? (
                  <>
                    <Badge variant="danger">Denied — {result.rule}</Badge>
                    <p className="mt-2.5 text-sm text-foreground">{result.reason}</p>
                    <Rows className="mt-3 border-t border-border pt-2.5">
                      <Row label="Confirmed by" value="HUMAN" />
                      <Row label="Decided by" value="POLICY_ENGINE" tone="ok" />
                      <Row label="Money moved" value="None" />
                    </Rows>
                  </>
                ) : null}
              </div>
              <div className="mt-4 flex gap-2">
                {pending && result && (
                  <Button variant="outline" onClick={confirm} disabled={busy}>Confirm again</Button>
                )}
                <Button variant="ghost"
                        onClick={() => { setStep("browse"); setPending(null); setResult(null); setError(null) }}>
                  Start another
                </Button>
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      <Card className="h-fit">
        <CardHeader><CardTitle>What happens on confirm</CardTitle></CardHeader>
        <CardContent>
          <ol className="divide-rows">
            {[
              ["Price re-read", "Compared against what you were shown"],
              ["Mandate re-checked", "Against the mandate in force now"],
              ["Key claimed", "Unique-constraint INSERT"],
              ["Recorded, then executed", "Audit entry written first"],
            ].map(([title, body]) => (
              <li key={title} className="py-2 first:pt-0 last:pb-0">
                <p className="text-xs font-medium text-foreground">{title}</p>
                <p className="text-xs text-muted-foreground">{body}</p>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  )
}
