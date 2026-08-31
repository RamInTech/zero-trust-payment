import { useEffect, useRef, useState } from "react"
import { AnimatePresence, motion } from "framer-motion"
import { Bot, Check, Info, KeyRound, Send, User, X } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { Badge } from "@/components/ui/badge"
import { api, type DemoConfig, type Json, type Pending } from "@/api"
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

type Message =
  | { id: number; kind: "customer"; text: string }
  | { id: number; kind: "agent"; text: string; parser?: string; note?: string }
  | { id: number; kind: "draft"; pending: Pending }
  | { id: number; kind: "verdict"; body: Json; declined?: boolean }

/** Omit does not distribute over a union; this does. */
type NewMessage<T = Message> = T extends any ? Omit<T, "id"> : never

const SUGGESTIONS = [
  "buy me some filter coffee",
  "I want 2 mugs",
  "get me a yacht",
  "coffee or tea?",
]

export function Chat({ agent, config, onChanged }: {
  agent: string; config: DemoConfig | null; onChanged: () => void
}) {
  const [messages, setMessages] = useState<Message[]>([])
  const [text, setText] = useState("")
  const [thinking, setThinking] = useState(false)
  const nextId = useRef(1)
  const endRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLInputElement>(null)

  const push = (m: NewMessage) =>
    setMessages(prev => [...prev, { ...m, id: nextId.current++ } as Message])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" })
  }, [messages, thinking])

  useEffect(() => {
    if (messages.length === 0) {
      push({ kind: "agent", text: "What would you like to buy?" })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function send(value?: string) {
    const message = (value ?? text).trim()
    if (!message || thinking) return
    push({ kind: "customer", text: message })
    setText("")
    setThinking(true)

    const res = await api.intentFromText(agent, message)

    if (!res.ok) {
      const detail = res.body.detail ?? {}
      // The parser's own words, not a stand-in for them.
      push({
        kind: "agent",
        text: detail.reason ?? "I couldn't turn that into a request.",
        parser: config?.parser,

      })
      setThinking(false)
      onChanged()
      return
    }

    const draft: Pending = res.body.awaiting_confirmation
    const keyed = await api.pending(draft.request_id)
    if (keyed.ok) draft.idempotency_key = keyed.body.idempotency_key

    push({
      kind: "agent",
      text: `${draft.item_name} (${draft.sku})${draft.quantity > 1 ? ` × ${draft.quantity}` : ""} — confirm below.`,
      parser: draft.parser,
    })
    push({ kind: "draft", pending: draft })
    setThinking(false)
    onChanged()
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
    onChanged()
  }

  async function decline(pending: Pending) {
    await api.decline(pending.request_id)
    push({ kind: "verdict", body: {}, declined: true })
    onChanged()
  }

  return (
    <div className="grid gap-5 lg:grid-cols-[1fr_330px]">
      <Card interactive={false} className="flex h-[640px] flex-col overflow-hidden">
        <CardHeader className="flex flex-row items-center justify-between border-b border-border">
          <div className="flex items-center gap-2.5">
            <span className="grid h-8 w-8 place-items-center rounded-lg border border-primary/25 bg-primary/[0.12]" aria-hidden="true">
              <Bot className="h-4 w-4 text-primary" />
            </span>
            <div>
              <CardTitle className="normal-case tracking-[-0.01em] text-[13.5px] text-foreground">
                Shopping agent
              </CardTitle>
              <p className="mono text-muted-foreground">
                intent parser: {config?.parser ?? "…"}
              </p>
            </div>
          </div>
          <Badge variant="warn">untrusted</Badge>
        </CardHeader>

        <div
          className="flex-1 space-y-3.5 overflow-y-auto bg-background px-5 py-4"
          role="log"
          aria-live="polite"
          aria-label="Conversation with the shopping agent"
        >
          <AnimatePresence initial={false}>
            {messages.map(m => (
              <motion.div
                key={m.id}
                initial={{ opacity: 0, y: 12, scale: 0.985 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
              >
                {m.kind === "customer" && (
                  <div className="flex justify-end gap-2.5">
                    <p className="max-w-[78%] rounded-lg rounded-br-sm bg-primary px-3.5 py-2.5 text-[13.5px] text-primary-foreground">
                      {m.text}
                    </p>
                    <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full border border-border bg-muted/50" aria-hidden="true">
                      <User className="h-3.5 w-3.5 text-muted-foreground" />
                    </span>
                  </div>
                )}

                {m.kind === "agent" && (
                  <div className="flex gap-2.5">
                    <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full border border-border bg-muted/50" aria-hidden="true">
                      <Bot className="h-3.5 w-3.5 text-muted-foreground" />
                    </span>
                    <div className="max-w-[78%]">
                      <p className="rounded-lg rounded-bl-sm border border-border bg-card px-3.5 py-2.5 text-[13.5px]">
                        {m.text}
                      </p>
                      {(m.parser || m.note) && (
                        <p className="mt-1 px-1 text-[11.5px] italic text-muted-foreground">
                          {m.note}
                          {m.parser && (
                            <span className="mono not-italic"> · parsed by {m.parser}</span>
                          )}
                        </p>
                      )}
                    </div>
                  </div>
                )}

                {m.kind === "draft" && (
                  <DraftCard pending={m.pending} onConfirm={confirm} onDecline={decline} />
                )}

                {m.kind === "verdict" && <Verdict body={m.body} declined={m.declined} />}
              </motion.div>
            ))}
          </AnimatePresence>

          {thinking && (
            <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }} className="flex gap-2.5">
              <span className="mt-0.5 grid h-7 w-7 shrink-0 place-items-center rounded-full border border-border bg-muted/50" aria-hidden="true">
                <Bot className="h-3.5 w-3.5 text-muted-foreground" />
              </span>
              <span className="flex items-center gap-1 rounded-lg rounded-bl-sm border border-border bg-card px-3.5 py-3">
                {[0, 1, 2].map(i => (
                  <motion.span key={i} className="h-1.5 w-1.5 rounded-full bg-muted-foreground"
                    animate={{ opacity: [0.25, 1, 0.25] }}
                    transition={{ duration: 1.1, repeat: Infinity, delay: i * 0.16 }} />
                ))}
                <span className="sr-only">Working…</span>
              </span>
            </motion.div>
          )}
          <div ref={endRef} />
        </div>

        <div className="border-t border-border px-5 py-3.5">
          <div className="mb-2.5 flex flex-wrap gap-1.5">
            {SUGGESTIONS.map(s => (
              <button key={s} onClick={() => send(s)} disabled={thinking}
                className="mono rounded-full border border-border bg-muted/50 px-2.5 py-1 text-muted-foreground transition-colors hover:border-border hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-40">
                {s}
              </button>
            ))}
          </div>
          <div className="flex gap-2">
            <label htmlFor="chat-input" className="sr-only">Message the shopping agent</label>
            <input
              id="chat-input" ref={inputRef} value={text}
              onChange={e => setText(e.target.value)}
              onKeyDown={e => e.key === "Enter" && send()}
              placeholder="Ask for something…"
              disabled={thinking}
              className="flex-1 rounded-xl border border-border bg-muted/60 px-3.5 py-2.5 text-[13.5px] transition-colors placeholder:text-muted-foreground/60 focus-visible:border-primary/40 focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-60"
            />
            <Button onClick={() => send()} disabled={thinking || !text.trim()} aria-label="Send message">
              <Send className="h-4 w-4" aria-hidden="true" />
            </Button>
          </div>
        </div>
      </Card>

      <Card interactive={false} className="h-fit">
        <CardHeader><CardTitle>Agent permissions</CardTitle></CardHeader>
        <CardContent className="grid gap-3 text-[12.5px]">
          <ul className="grid gap-2">
            {[
              ["Can", "name a catalog item"],
              ["Can", "ask for clarification"],
              ["Cannot", "set a price"],
              ["Cannot", "approve a purchase"],
              ["Cannot", "write to the audit log"],
            ].map(([verb, what], i) => (
              <li key={i} className="flex gap-2">
                <span className={cn("mono shrink-0 font-semibold",
                  verb === "Can" ? "text-ok" : "text-danger")}>{verb}</span>
                <span className="text-muted-foreground">{what}</span>
              </li>
            ))}
          </ul>
          {config && !config.conversational && (
            <p className="rounded-md subtle px-3 py-2 text-[11.5px] leading-relaxed text-muted-foreground">
              <Info className="mr-1 inline h-3 w-3 -translate-y-px" aria-hidden="true" />
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
    <div className="ml-10 max-w-[80%] rounded-lg border border-warn/30 bg-warn/[0.05] p-4">
      <Badge variant="warn">Proposed by the agent — not approved</Badge>
      <p className="mt-2.5 text-[15px] font-semibold tracking-[-0.01em]">{pending.prompt}</p>
      <dl className="mono mt-2 grid grid-cols-[62px_1fr] gap-x-2 text-muted-foreground">
        <dt>amount</dt><dd>{rupees(pending.displayed_amount_paise)}</dd>
        <dt className="flex items-center gap-1">
          <KeyRound className="h-3 w-3" aria-hidden="true" />key
        </dt>
        <dd className="truncate text-foreground/75">{pending.idempotency_key ?? "—"}</dd>
      </dl>
      {!done && (
        <div className="mt-3 flex gap-2">
          <Button size="sm" onClick={() => { setDone(true); onConfirm(pending) }}>
            <Check className="h-3.5 w-3.5" aria-hidden="true" />Confirm
          </Button>
          <Button size="sm" variant="outline" onClick={() => { setDone(true); onDecline(pending) }}>
            <X className="h-3.5 w-3.5" aria-hidden="true" />Decline
          </Button>
        </div>
      )}
      {done && (
        <p className="mono mt-3 text-muted-foreground">answered — see below</p>
      )}
    </div>
  )
}

function Verdict({ body, declined }: { body: Json; declined?: boolean }) {
  if (declined) {
    return (
      <Centered tone="default" title="Declined">
        No policy check, no charge.
      </Centered>
    )
  }
  if (body.rejected) {
    return (
      <Centered tone={body.code === "PENDING_VERIFICATION" ? "warn" : "danger"}
               title={body.code}>
        {body.reason}
        {body.guidance && <span className="mt-1 block italic">{body.guidance}</span>}
      </Centered>
    )
  }
  if (body.approved) {
    return (
      <Centered tone="ok" title={`Approved — ${body.idempotency_outcome}`}>
        {body.executed ? "Charged once." : "Replayed — no second charge."}
        {body.response?.order_id && (
          <span className="mono mt-1 block">{body.response.order_id}</span>
        )}
      </Centered>
    )
  }
  return (
    <Centered tone="danger" title={`Denied — ${body.rule}`}>
      {body.reason}
    </Centered>
  )
}

function Centered({ tone, title, children }: {
  tone: "ok" | "danger" | "warn" | "default"
  title: string
  children: React.ReactNode
}) {
  const ring = {
    ok: "border-ok/25 bg-ok/[0.06]",
    danger: "border-danger/25 bg-danger/[0.05]",
    warn: "border-warn/30 bg-warn/[0.06]",
    default: "border-border bg-muted",
  }[tone]
  return (
    <div className={cn("mx-auto max-w-[86%] rounded-xl border px-4 py-3 text-center", ring)}>
      <p className="text-[11px] font-semibold uppercase tracking-[0.12em]">{title}</p>
      <p className="mt-1 text-[12.5px] leading-relaxed text-muted-foreground">{children}</p>
    </div>
  )
}
