import { useEffect, useRef, useState } from "react"
import { Bot, Check, KeyRound, Lock, Send, X } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { api, type DemoConfig, type Json, type Pending } from "@/api"
import { LifecycleTracker } from "@/components/LifecycleTracker"
import { cn, rupees } from "@/lib/utils"

/**
 * A conversational surface over the real intent → confirm → policy flow.
 *
 * Every agent line here is built from actual system state: the parse result,
 * the clarification the parser genuinely asked for, or the policy engine's
 * verdict. Nothing is generated prose, because the intent layer is a parser
 * rather than a chat model -- and a chat UI that implied otherwise would be
 * the same kind of dishonesty as an indicator for a protection that does not
 * exist.
 */

export type Message =
  | { id: number; kind: "customer"; text: string }
  | { id: number; kind: "agent"; text: string; parser?: string; note?: string }
  | { id: number; kind: "draft"; pending: Pending }
  | { id: number; kind: "basket"; lines: Pending[]; totalPaise: number }
  | { id: number; kind: "verdict"; body: Json; declined?: boolean }
  | { id: number; kind: "stock-offer"; request: string }

/** Omit does not distribute over a union; this does. */
type NewMessage<T = Message> = T extends any ? Omit<T, "id"> : never

const SUGGESTIONS = [
  "buy me a mobile phone",
  "I want 2 mugs",
  "something cold to drink",
  "coffee or tea?",
]

/**
 * Conversation state lives in App, not here.
 *
 * This panel unmounts whenever another tab is selected, so state held locally
 * was destroyed on every navigation -- the customer lost the whole thread just
 * by glancing at the dashboard. Lifting it up is the fix; the alternative
 * (keeping every tab mounted forever) costs more and hides the reason.
 */
export function Chat({ agent, config, onChanged, messages, setMessages }: {
  agent: string; config: DemoConfig | null; onChanged: () => void
  messages: Message[]
  setMessages: React.Dispatch<React.SetStateAction<Message[]>>
}) {
  const [text, setText] = useState("")
  const [thinking, setThinking] = useState(false)
  const [e2e, setE2e] = useState(false)
  // Nudges the tracker to re-read immediately after an action, instead of
  // waiting out its poll interval.
  const [trackerKey, setTrackerKey] = useState(0)
  const bump = () => setTrackerKey(k => k + 1)
  // Derived, not a ref: a ref resets to 1 on remount and would collide with
  // ids already in the restored thread.
  const nextId = useRef(1)
  nextId.current = Math.max(1, ...messages.map(m => m.id + 1), 1)
  const endRef = useRef<HTMLDivElement>(null)

  const push = (m: NewMessage) =>
    setMessages(prev => [...prev, { ...m, id: nextId.current++ } as Message])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [messages, thinking])

  useEffect(() => {
    // The greeting is now seeded by App when the thread is created, so this
    // effect must not add one -- returning to the tab would prepend a second.
    api.e2ePublicKey().then(res => setE2e(res.ok))
  }, [])

  async function send(value?: string) {
    const message = (value ?? text).trim()
    if (!message || thinking) return
    push({ kind: "customer", text: message })
    setText("")
    setThinking(true)

    const res = await api.intentFromTextSealed(agent, message)

    if (!res.ok) {
      const detail = res.body.detail ?? {}
      // The parser's own words, not a stand-in for them.
      push({
        kind: "agent",
        text: detail.reason ?? "I couldn't turn that into a request.",
        parser: config?.parser,
      })
      // The agent could not name a catalog item. Offer the one legitimate way
      // forward: a MERCHANT stocks it, with a merchant-set price. The customer
      // and the model never supply a price -- that invariant is what makes
      // confirm-time re-validation mean anything.
      push({ kind: "stock-offer", request: message })
      setThinking(false)
      onChanged()
      return
    }

    const lines: Pending[] = res.body.basket ?? [res.body.awaiting_confirmation]
    // Each line is its own transaction with its own key, so each is fetched
    // individually rather than assuming one key covers the basket.
    for (const line of lines) {
      const keyed = await api.pending(line.request_id)
      if (keyed.ok) line.idempotency_key = keyed.body.idempotency_key
    }

    if (lines.length === 1) {
      const draft = lines[0]
      push({
        kind: "agent",
        text: `${draft.item_name} (${draft.sku})${draft.quantity > 1 ? ` × ${draft.quantity}` : ""} — confirm below.`,
        parser: draft.parser,
      })
      push({ kind: "draft", pending: draft })
    } else {
      push({
        kind: "agent",
        text: `${lines.length} items — each is a separate transaction, checked on its own.`,
        parser: lines[0].parser,
      })
      push({
        kind: "basket",
        lines,
        totalPaise: res.body.basket_total_paise
          ?? lines.reduce((sum, l) => sum + l.displayed_amount_paise, 0),
      })
    }
    setThinking(false)
    onChanged(); bump()
  }

  async function confirm(pending: Pending) {
    setThinking(true)
    const res = await api.confirm(pending.request_id)
    setThinking(false)
    if (res.status === 409 || res.status === 410 || res.status === 404 || res.status === 503) {
      push({ kind: "verdict", body: { rejected: true, ...res.body.detail } })
    } else {
      push({ kind: "verdict", body: res.body })
    }
    onChanged(); bump()
  }

  async function decline(pending: Pending) {
    await api.decline(pending.request_id)
    push({ kind: "verdict", body: {}, declined: true })
    onChanged(); bump()
  }

  return (
    <div className="grid gap-4 lg:grid-cols-[240px_1fr_300px]">
      {/* Left rail: what each transaction is actually doing right now. */}
      <Card className="order-2 flex h-[calc(100vh-11rem)] min-h-[520px] flex-col overflow-hidden lg:order-1">
        <CardHeader>
          <div>
            <CardTitle>Live transactions</CardTitle>
            <p className="text-xs text-muted-foreground">Read from the audit log</p>
          </div>
        </CardHeader>
        <div className="flex-1 overflow-y-auto">
          <LifecycleTracker refreshKey={trackerKey} />
        </div>
      </Card>

      <Card className="order-1 flex h-[calc(100vh-11rem)] min-h-[520px] flex-col overflow-hidden lg:order-2">
        <CardHeader>
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-md bg-muted" aria-hidden="true">
              <Bot className="h-4 w-4 text-muted-foreground" />
            </span>
            <div className="min-w-0">
              <CardTitle>Shopping agent</CardTitle>
              <p className="text-xs text-muted-foreground">
                Intent parser: {config?.parser ?? "…"}
              </p>
            </div>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            {e2e && (
              <Badge variant="default">
                <Lock className="h-3 w-3" aria-hidden="true" />
                Encrypted
              </Badge>
            )}
            <Badge variant="warn">Untrusted</Badge>
          </div>
        </CardHeader>

        <div
          className="flex-1 space-y-4 overflow-y-auto px-5 py-5"
          role="log"
          aria-live="polite"
          aria-label="Conversation with the shopping agent"
        >
          {messages.map(m => (
            <div key={m.id}>
              {m.kind === "customer" && (
                <div className="flex justify-end">
                  <p className="w-fit max-w-[76%] rounded-lg rounded-br-sm bg-primary px-3 py-2 text-sm text-primary-foreground">
                    {m.text}
                  </p>
                </div>
              )}

              {m.kind === "agent" && (
                <div className="flex max-w-[76%] flex-col items-start">
                  <p className="w-fit rounded-lg rounded-bl-sm bg-muted px-3 py-2 text-sm text-foreground">
                    {m.text}
                  </p>
                  {(m.parser || m.note) && (
                    <p className="mt-1 text-2xs text-faint">
                      {m.note}
                      {m.parser && <span> Parsed by {m.parser}.</span>}
                    </p>
                  )}
                </div>
              )}

              {m.kind === "draft" && (
                <DraftCard pending={m.pending} onConfirm={confirm} onDecline={decline} />
              )}

              {m.kind === "basket" && (
                <BasketCard lines={m.lines} totalPaise={m.totalPaise}
                            onConfirm={confirm} onDecline={decline} />
              )}

              {m.kind === "verdict" && <Verdict body={m.body} declined={m.declined} />}

              {m.kind === "stock-offer" && (
                <StockOffer request={m.request} onStocked={sku => {
                  onChanged()
                  send(`buy the ${sku}`)
                }} />
              )}
            </div>
          ))}

          {thinking && (
            <p className="text-xs text-faint">
              Parsing with {config?.parser ?? "the agent"}…
            </p>
          )}
          <div ref={endRef} />
        </div>

        <div className="border-t border-border px-5 py-3.5">
          <div className="mb-2.5 flex flex-wrap gap-1.5">
            {SUGGESTIONS.map(s => (
              <button key={s} onClick={() => send(s)} disabled={thinking}
                className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40">
                {s}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <label htmlFor="chat-input" className="sr-only">Message the shopping agent</label>
            <input
              id="chat-input" value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => e.key === "Enter" && send()}
              placeholder="Ask for something…"
              disabled={thinking}
              className="h-9 flex-1 rounded-md border border-input bg-card px-3 text-sm transition-colors placeholder:text-faint focus-visible:border-primary focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            />
            <Button onClick={() => send()} disabled={thinking || !text.trim()} aria-label="Send message">
              <Send className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </div>
      </Card>

      <Card className="order-3 h-fit lg:order-3">
        <CardHeader><CardTitle>Agent permissions</CardTitle></CardHeader>
        <CardContent className="grid gap-4">
          <ul className="grid gap-2">
            {[
              ["Can", "Name a catalog item"],
              ["Can", "Ask for clarification"],
              ["Cannot", "Set a price"],
              ["Cannot", "Approve a purchase"],
              ["Cannot", "Write to the audit log"],
            ].map(([verb, what], i) => (
              <li key={i} className="flex items-baseline gap-2 text-xs">
                <span className={cn("w-12 shrink-0 font-medium",
                  verb === "Can" ? "text-ok" : "text-danger")}>{verb}</span>
                <span className="text-muted-foreground">{what}</span>
              </li>
            ))}
          </ul>
          {config && !config.conversational && (
            <p className="border-t border-border pt-3 text-2xs leading-relaxed text-muted-foreground">
              Replies are built from system state, not generated. The intent
              layer parses; it does not converse.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function DraftCard({ pending, onConfirm, onDecline }: {
  pending: Pending
  onConfirm: (p: Pending) => void
  onDecline: (p: Pending) => void
}) {
  const [done, setDone] = useState(false)
  return (
    <div className="max-w-[86%] rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-3">
        <p className="text-md font-semibold text-foreground">{pending.prompt}</p>
        <Badge variant="warn">Proposed</Badge>
      </div>
      <dl className="mt-3 border-t border-border pt-2.5">
        <div className="flex items-baseline justify-between gap-4 py-1">
          <dt className="text-xs text-muted-foreground">Amount</dt>
          <dd className="mono text-foreground">{rupees(pending.displayed_amount_paise)}</dd>
        </div>
        <div className="flex items-baseline justify-between gap-4 py-1">
          <dt className="flex items-center gap-1 text-xs text-muted-foreground">
            <KeyRound className="h-3 w-3" aria-hidden="true" />Idempotency key
          </dt>
          <dd className="mono min-w-0 truncate text-muted-foreground">
            {pending.idempotency_key ?? "—"}
          </dd>
        </div>
      </dl>
      {!done ? (
        <div className="mt-3 flex gap-2">
          <Button size="sm" onClick={() => { setDone(true); onConfirm(pending) }}>
            <Check className="h-3.5 w-3.5" aria-hidden="true" />Confirm
          </Button>
          <Button size="sm" variant="outline" onClick={() => { setDone(true); onDecline(pending) }}>
            <X className="h-3.5 w-3.5" aria-hidden="true" />Decline
          </Button>
        </div>
      ) : (
        <p className="mt-3 text-xs text-faint">Answered — see below.</p>
      )}
    </div>
  )
}

function Verdict({ body, declined }: { body: Json; declined?: boolean }) {
  if (declined) {
    return <Notice tone="default" title="Declined">No policy check, no charge.</Notice>
  }
  if (body.rejected) {
    return (
      <Notice tone={body.code === "PENDING_VERIFICATION" ? "warn" : "danger"} title={body.code}>
        {body.reason}
        {body.guidance && <span className="mt-1 block">{body.guidance}</span>}
      </Notice>
    )
  }
  if (body.approved) {
    return (
      <Notice tone="ok" title={`Approved — ${body.idempotency_outcome}`}>
        {body.executed ? "Charged once." : "Replayed — no second charge."}
        {body.response?.order_id && (
          <span className="mono mt-1 block text-muted-foreground">{body.response.order_id}</span>
        )}
      </Notice>
    )
  }
  return <Notice tone="danger" title={`Denied — ${body.rule}`}>{body.reason}</Notice>
}

/** A left-aligned notice with a colour rule, not a centred tinted capsule. */
function Notice({ tone, title, children }: {
  tone: "ok" | "danger" | "warn" | "default"
  title: string
  children: React.ReactNode
}) {
  const rule = {
    ok: "border-l-ok",
    danger: "border-l-danger",
    warn: "border-l-warn",
    default: "border-l-border-strong",
  }[tone]
  const text = {
    ok: "text-ok", danger: "text-danger", warn: "text-warn", default: "text-foreground",
  }[tone]
  return (
    <div className={cn("max-w-[86%] rounded-r border-l-2 bg-muted/60 px-3.5 py-2.5", rule)}>
      <p className={cn("text-xs font-semibold", text)}>{title}</p>
      <p className="mt-0.5 text-xs leading-relaxed text-muted-foreground">{children}</p>
    </div>
  )
}


/**
 * Stock something the shop does not carry yet.
 *
 * This is a MERCHANT action deliberately placed in the customer's view, because
 * the demo has one operator playing both parts. The distinction that matters is
 * not who clicks it but where the PRICE comes from: it is typed here and stored
 * in the catalog, never proposed by the customer's sentence or by the model.
 * `ParsedIntent` has no field capable of carrying a price, so there is no path
 * by which "buy me a phone for Rs.1" could ever set one.
 */
function StockOffer({ request, onStocked }: {
  request: string; onStocked: (sku: string) => void
}) {
  const [open, setOpen] = useState(false)
  const [name, setName] = useState("")
  const [price, setPrice] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function stock() {
    const rupeesTyped = Number(price)
    if (!name.trim()) { setError("Give the item a name."); return }
    if (!Number.isFinite(rupeesTyped) || rupeesTyped <= 0) {
      setError("Enter a price greater than zero."); return
    }
    const sku = "SKU-" + name.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "-").replace(/^-|-$/g, "")
    setBusy(true); setError(null)
    const res = await api.addItem(sku, name.trim(), Math.round(rupeesTyped * 100))
    setBusy(false)
    if (!res.ok) {
      setError(res.body?.detail?.reason ?? "Could not stock that item.")
      return
    }
    setOpen(false)
    onStocked(name.trim())
  }

  if (!open) {
    return (
      <div className="max-w-[76%]">
        <button
          onClick={() => { setOpen(true); setName(request.slice(0, 40)) }}
          className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring"
        >
          Not stocked — add it to the catalog
        </button>
      </div>
    )
  }

  return (
    <div className="max-w-[76%] rounded-lg border border-border bg-card p-3">
      <p className="mb-2 text-xs font-medium text-foreground">Stock a new item</p>
      <div className="grid gap-2">
        <input
          value={name} onChange={e => setName(e.target.value)} autoFocus
          placeholder="Item name"
          className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        <input
          value={price} onChange={e => setPrice(e.target.value)}
          type="number" min="1" placeholder="Price in ₹"
          onKeyDown={e => { if (e.key === "Enter") stock() }}
          className="rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
      </div>
      <div className="mt-2 flex gap-2">
        <Button size="sm" onClick={stock} disabled={busy}>
          {busy ? "Stocking…" : "Stock it"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setOpen(false)}>Cancel</Button>
      </div>
      {error
        ? <p className="mt-1.5 text-2xs text-danger">{error}</p>
        : <p className="mt-1.5 text-2xs text-faint">
            The price is set by the merchant here and read from the catalog at
            confirmation. It never comes from the message or the model.
          </p>}
    </div>
  )
}


/**
 * Several items in one request.
 *
 * The total is a DISPLAY figure and is labelled as one. Nobody is authorised
 * to spend it: each line is confirmed and policy-checked separately, and the
 * per-transaction cap applies to each line rather than to this sum. Showing a
 * single total next to a single button would imply a batch approval the
 * system deliberately does not have.
 */
function BasketCard({ lines, totalPaise, onConfirm, onDecline }: {
  lines: Pending[]; totalPaise: number
  onConfirm: (p: Pending) => void; onDecline: (p: Pending) => void
}) {
  const [done, setDone] = useState<Record<string, "confirmed" | "declined">>({})
  const remaining = lines.filter(l => !done[l.request_id])

  const mark = (p: Pending, how: "confirmed" | "declined") =>
    setDone(prev => ({ ...prev, [p.request_id]: how }))

  return (
    <div className="max-w-[85%] rounded-lg border border-border bg-card">
      <div className="border-b border-border px-3.5 py-2.5">
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-xs font-medium text-foreground">
            {lines.length} items
          </span>
          <span className="text-sm font-semibold tabular-nums text-foreground">
            {rupees(totalPaise)}
          </span>
        </div>
        <p className="mt-0.5 text-2xs text-faint">
          Cumulative total, shown for information. Each line is authorised on
          its own — the limit applies per transaction, not to this sum.
        </p>
      </div>

      <ul className="divide-y divide-border">
        {lines.map(l => (
          <li key={l.request_id} className="flex items-center justify-between gap-3 px-3.5 py-2.5">
            <div className="min-w-0">
              <p className="truncate text-xs text-foreground">
                {l.item_name}
                {l.quantity > 1 && <span className="text-muted-foreground"> × {l.quantity}</span>}
              </p>
              <p className="mono text-2xs text-faint">{l.sku}</p>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <span className="text-xs tabular-nums text-muted-foreground">
                {rupees(l.displayed_amount_paise)}
              </span>
              {done[l.request_id] ? (
                <span className="text-2xs text-faint">
                  {done[l.request_id] === "confirmed" ? "Sent" : "Declined"}
                </span>
              ) : (
                <>
                  <Button size="sm" onClick={() => { mark(l, "confirmed"); onConfirm(l) }}>
                    <Check className="h-3 w-3" aria-hidden="true" />
                  </Button>
                  <Button size="sm" variant="outline"
                          onClick={() => { mark(l, "declined"); onDecline(l) }}>
                    <X className="h-3 w-3" aria-hidden="true" />
                  </Button>
                </>
              )}
            </div>
          </li>
        ))}
      </ul>

      {remaining.length > 1 && (
        <div className="border-t border-border px-3.5 py-2.5">
          <Button size="sm" onClick={() => remaining.forEach(l => {
            mark(l, "confirmed"); onConfirm(l)
          })}>
            Confirm all {remaining.length}
          </Button>
          <p className="mt-1 text-2xs text-faint">
            Sends each one separately. Any line can still be refused on its own.
          </p>
        </div>
      )}
    </div>
  )
}
