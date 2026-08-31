import { useEffect, useRef, useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { Check, CircleAlert, KeyRound, X } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { api, type ConfirmResult, type Json, type Pending } from "@/api"
import { rupees } from "@/lib/utils"

type Step = "browse" | "draft" | "result"

const STEPS: { id: Step; label: string }[] = [
  { id: "browse", label: "Choose" },
  { id: "draft", label: "Confirm" },
  { id: "result", label: "Result" },
]

const slide = {
  initial: { opacity: 0, x: 18 },
  animate: { opacity: 1, x: 0 },
  exit: { opacity: 0, x: -18 },
  transition: { duration: 0.28, ease: [0.22, 1, 0.36, 1] as const },
}

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

  // Move focus with the step, or keyboard users are stranded behind the animation.
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

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_360px]">
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle>Purchase flow</CardTitle>
          <ol className="flex items-center gap-1.5" aria-label="Progress">
            {STEPS.map((s, i) => {
              const active = STEPS.findIndex(x => x.id === step) >= i
              return (
                <li key={s.id} className="flex items-center gap-1.5">
                  <span className={`mono rounded px-2 py-0.5 ${active ? "bg-primary/15 text-primary" : "text-muted-foreground"}`}
                        aria-current={step === s.id ? "step" : undefined}>
                    {i + 1}. {s.label}
                  </span>
                  {i < STEPS.length - 1 && <span className="text-muted-foreground" aria-hidden="true">→</span>}
                </li>
              )
            })}
          </ol>
        </CardHeader>
        <CardContent>
          <h3 ref={headingRef} tabIndex={-1} className="sr-only">
            Step {STEPS.findIndex(s => s.id === step) + 1}: {STEPS.find(s => s.id === step)?.label}
          </h3>

          <AnimatePresence mode="wait">
            {step === "browse" && (
              <motion.div key="browse" {...slide} className="grid gap-4">
                <div>
                  <label htmlFor="intent" className="text-[12.5px] text-muted-foreground">
                    Describe what you want
                  </label>
                  <div className="mt-1.5 flex gap-2">
                    <input id="intent" value={text} onChange={e => setText(e.target.value)}
                      onKeyDown={e => e.key === "Enter" && propose()}
                      className="mono flex-1 rounded-lg border border-border bg-muted/60 px-3.5 py-2.5 text-foreground transition-colors placeholder:text-muted-foreground/60 focus-visible:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring" />
                    <Button onClick={() => propose()} disabled={busy}>Send</Button>
                  </div>
                </div>
                <div>
                  <div className="text-[12.5px] text-muted-foreground">Or select an item</div>
                  <div className="mt-2 grid gap-2 sm:grid-cols-2">
                    {catalog.map(item => (
                      <button key={item.sku} onClick={() => propose(item.sku)} disabled={busy}
                        className="group flex items-center justify-between rounded-xl border border-border bg-muted/50 px-3.5 py-2.5 text-left transition-all hover:-translate-y-0.5 hover:border-primary/30 hover:bg-muted/50 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
                        <span>
                          <span className="block text-[13.5px]">{item.name}</span>
                          <span className="mono text-muted-foreground">{item.sku}</span>
                        </span>
                        <span className="mono tabular-nums">{rupees(item.price_paise)}</span>
                      </button>
                    ))}
                  </div>
                </div>
              </motion.div>
            )}

            {step === "draft" && pending && (
              <motion.div key="draft" {...slide}>
                <Badge variant="warn">
                  <CircleAlert className="h-3 w-3" aria-hidden="true" />
                  Proposed by the agent — not approved
                </Badge>
                <p className="mt-3.5 text-[19px] font-semibold tracking-[-0.015em]">{pending.prompt}</p>
                <dl className="mono mt-3 grid grid-cols-[86px_1fr] gap-x-3 gap-y-1 text-muted-foreground">
                  <dt>parsed by</dt><dd>{pending.parser}</dd>
                  <dt>request</dt><dd>{pending.request_id}</dd>
                  <dt className="flex items-center gap-1"><KeyRound className="h-3 w-3" aria-hidden="true" />key</dt>
                  <dd className="text-foreground/80">{pending.idempotency_key ?? "—"}</dd>
                </dl>
                <div className="mt-4 flex gap-2">
                  <Button onClick={confirm} disabled={busy}><Check className="h-4 w-4" aria-hidden="true" />Confirm</Button>
                  <Button variant="outline" onClick={decline} disabled={busy}><X className="h-4 w-4" aria-hidden="true" />Decline</Button>
                </div>
                <p className="mt-3 text-[12.5px] text-muted-foreground">
                  Not authorised yet.
                </p>
              </motion.div>
            )}

            {step === "result" && (
              <motion.div key="result" {...slide}>
                <div aria-live="polite">
                  {error ? (
                    <>
                      <Badge variant={error.code === "PENDING_VERIFICATION" ? "warn" : "danger"}>{error.code}</Badge>
                      <p className="mono mt-3 text-foreground/90">{error.reason}</p>
                      {(error as Json).guidance && (
                        <p className="mt-2 text-[12.5px] italic text-muted-foreground">{(error as Json).guidance}</p>
                      )}
                    </>
                  ) : result?.approved ? (
                    <>
                      <Badge variant="ok">Approved — {result.idempotency_outcome}</Badge>
                      <ul className="mono mt-3 grid gap-1">
                        <li><span className="text-ok">✓</span> amount within the per-transaction cap</li>
                        <li><span className="text-ok">✓</span> item on the mandate allowlist</li>
                        <li><span className="text-ok">✓</span> mandate not expired</li>
                        <li><span className="text-ok">✓</span> velocity slot claimed</li>
                      </ul>
                      <dl className="mono mt-3 grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-muted-foreground">
                        <dt>confirmed by</dt><dd className="text-primary">HUMAN ✓</dd>
                        <dt>decided by</dt><dd className="text-ok">POLICY_ENGINE</dd>
                        <dt>order</dt><dd>{result.response?.order_id ?? "—"}</dd>
                        <dt>money moved</dt><dd>{result.executed ? "yes, once" : "no — replayed"}</dd>
                      </dl>
                      <p className="mt-3 text-[12px] text-warn">Capture simulated.</p>
                    </>
                  ) : result ? (
                    <>
                      <Badge variant="danger">Denied — {result.rule}</Badge>
                      <p className="mono mt-3 text-foreground/90">{result.reason}</p>
                      <dl className="mono mt-3 grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-muted-foreground">
                        <dt>confirmed by</dt><dd className="text-primary">HUMAN ✓</dd>
                        <dt>decided by</dt><dd className="text-ok">POLICY_ENGINE</dd>
                        <dt>money moved</dt><dd>none</dd>
                      </dl>

                    </>
                  ) : null}
                </div>
                <div className="mt-4 flex gap-2">
                  {pending && result && (
                    <Button variant="outline" onClick={confirm} disabled={busy}>Confirm again</Button>
                  )}
                  <Button variant="ghost" onClick={() => { setStep("browse"); setPending(null); setResult(null); setError(null) }}>
                    Start another
                  </Button>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </CardContent>
      </Card>

      <Card className="h-fit">
        <CardHeader><CardTitle>What happens on confirm</CardTitle></CardHeader>
        <CardContent>
          <ol className="grid gap-3 text-[12.5px]">
            {[
              ["Price re-read", "compared against what you were shown"],
              ["Mandate re-checked", "against the mandate in force now"],
              ["Key claimed", "unique-constraint INSERT"],
              ["Recorded, then executed", "audit entry written first"],
            ].map(([title, body], i) => (
              <li key={title} className="grid grid-cols-[22px_1fr] gap-2.5">
                <span className="mono grid h-5 w-5 place-items-center rounded border border-border bg-muted/50 text-[10px] text-muted-foreground">{i + 1}</span>
                <span><strong className="font-semibold">{title}.</strong>{" "}
                  <span className="text-muted-foreground">{body}</span></span>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>
    </div>
  )
}
