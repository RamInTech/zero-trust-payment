import { useCallback, useEffect, useState } from "react"
import { MotionConfig, motion } from "framer-motion"
import {
  LayoutDashboard, MessagesSquare, PanelLeftClose, PanelLeftOpen, Receipt,
  ShieldCheck, ShoppingCart,
} from "lucide-react"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from "@/components/ui/tooltip"
import { Dashboard } from "@/panels/Dashboard"
import { Chat, type Message } from "@/panels/Chat"
import { Checkout } from "@/panels/Checkout"
import { Transactions } from "@/panels/Transactions"
import { SecurityHub } from "@/panels/SecurityHub"
import { api, type DemoConfig, type Json } from "@/api"
import { EASE, cn } from "@/lib/utils"

const NAV = [
  {
    value: "chat", label: "Chat", Icon: MessagesSquare,
    blurb: "An untrusted agent proposes a purchase. Only a human can confirm it.",
  },
  {
    value: "dashboard", label: "Overview", Icon: LayoutDashboard,
    blurb: "The mandate in force and what has been spent under it.",
  },
  {
    value: "checkout", label: "Checkout", Icon: ShoppingCart,
    blurb: "The same authorisation path, one step at a time.",
  },
  {
    value: "transactions", label: "Transactions", Icon: Receipt,
    blurb: "Every request, and why it was approved or denied.",
  },
  {
    value: "hub", label: "Security", Icon: ShieldCheck,
    blurb: "Each protection, and a way to make it refuse something now.",
  },
]

const SIDEBAR_WIDE = 232
const SIDEBAR_NARROW = 64

export default function App() {
  const [collapsed, setCollapsed] = useState(() => {
    // Remembered per browser; a viewer who collapsed it should not have to
    // collapse it again on every visit.
    try { return localStorage.getItem("sidebar-collapsed") === "1" } catch { return false }
  })
  const [agent, setAgent] = useState("agent_alpha")
  const [catalog, setCatalog] = useState<Json[]>([])
  const [mandate, setMandate] = useState<Json | null>(null)
  const [stats, setStats] = useState<Json | null>(null)
  const [audit, setAudit] = useState<Json[]>([])
  const [transactions, setTransactions] = useState<Json[]>([])
  const [layers, setLayers] = useState<Json | null>(null)
  const [adversarial, setAdversarial] = useState<Json | null>(null)
  const [config, setConfig] = useState<DemoConfig | null>(null)
  // Held here so the conversation survives tab changes -- the Chat panel
  // unmounts when another tab is selected, which used to wipe the thread.
  const [messages, setMessages] = useState<Message[]>([
    { id: 1, kind: "agent", text: "What would you like to buy?" },
  ])
  const [sweep, setSweep] = useState<Json | null>(null)
  const [tab, setTab] = useState("chat")

  const refresh = useCallback(async (who = agent) => {
    const [m, s, a, t, l, w] = await Promise.all([
      api.mandate(who), api.stats(), api.audit(), api.transactions(), api.layers(),
      api.sweep(),
    ])
    if (m.ok) setMandate(m.body)
    if (s.ok) setStats(s.body)
    if (a.ok) setAudit(a.body.events)
    if (t.ok) setTransactions(t.body.transactions)
    if (l.ok) setLayers(l.body)
    if (w.ok) setSweep(w.body)
  }, [agent])

  useEffect(() => {
    (async () => {
      const cfg = await api.config()
      if (cfg.ok) setConfig(cfg.body as DemoConfig)
      const who = cfg.ok ? cfg.body.agent_id : "agent_alpha"
      setAgent(who)
      const [c, adv] = await Promise.all([api.catalog(), api.adversarial()])
      if (c.ok) setCatalog(c.body.items)
      if (adv.ok) setAdversarial(adv.body)
      refresh(who)
    })()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  useEffect(() => {
    try { localStorage.setItem("sidebar-collapsed", collapsed ? "1" : "0") } catch { /* storage may be blocked */ }
  }, [collapsed])

  const current = NAV.find(n => n.value === tab)

  return (
    // Framer Motion does not consult prefers-reduced-motion on its own; without
    // this every transition below would play regardless of the OS setting. The
    // CSS media block in index.css only reaches CSS transitions, not JS-driven
    // ones, so the two together are what actually honour the preference.
    <MotionConfig reducedMotion="user">
    <TooltipProvider delayDuration={200}>
      <a href="#main"
         className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-primary-foreground">
        Skip to content
      </a>

      <div className="flex min-h-screen">
        <motion.aside
          initial={false}
          animate={{ width: collapsed ? SIDEBAR_NARROW : SIDEBAR_WIDE }}
          transition={{ duration: 0.28, ease: EASE }}
          className="sticky top-0 hidden h-screen shrink-0 flex-col overflow-hidden whitespace-nowrap border-r border-border bg-card md:flex"
        >
          <div className={cn("flex items-center gap-2.5 py-5",
                             collapsed ? "justify-center px-0" : "px-5")}>
            <span className="grid h-7 w-7 shrink-0 place-items-center rounded-md bg-navy" aria-hidden="true">
              <ShieldCheck className="h-4 w-4 text-white" />
            </span>
            {!collapsed && (
              <span className="min-w-0 text-sm font-semibold leading-tight text-foreground">
                Zero-Trust
                <span className="block truncate text-2xs font-normal text-muted-foreground">
                  Payment Authorization
                </span>
              </span>
            )}
          </div>

          <nav className={cn("flex-1", collapsed ? "px-2" : "px-3")} aria-label="Sections">
            <ul className="grid gap-px">
              {NAV.map(({ value, label, Icon }) => {
                const active = tab === value
                const button = (
                  <button
                    onClick={() => setTab(value)}
                    aria-current={active ? "page" : undefined}
                    className={cn(
                      "relative flex w-full items-center gap-2.5 rounded-md py-[7px] text-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      collapsed ? "justify-center px-0" : "px-2.5",
                      active
                        ? "font-medium text-foreground"
                        : "text-muted-foreground hover:bg-muted/60 hover:text-foreground"
                    )}
                  >
                    {/* The selected ground slides between items rather than
                        blinking out of one and into the next. */}
                    {active && (
                      <motion.span
                        layoutId="nav-active"
                        transition={{ duration: 0.24, ease: EASE }}
                        className="absolute inset-0 -z-10 rounded-md bg-muted"
                        aria-hidden="true"
                      />
                    )}
                    <Icon
                      className={cn("h-4 w-4 shrink-0", active ? "text-primary" : "text-faint")}
                      aria-hidden="true"
                    />
                    {!collapsed && <span className="truncate">{label}</span>}
                  </button>
                )
                return (
                  <li key={value}>
                    {collapsed ? (
                      <Tooltip>
                        <TooltipTrigger asChild>{button}</TooltipTrigger>
                        <TooltipContent side="right">{label}</TooltipContent>
                      </Tooltip>
                    ) : button}
                  </li>
                )
              })}
            </ul>
          </nav>

          {!collapsed && (
            <p className="whitespace-normal px-5 pb-4 text-2xs leading-relaxed text-faint">
              Built on Razorpay test mode. Not an official Razorpay product.
              Capture is simulated.
            </p>
          )}

          <div className={cn("border-t border-border py-2", collapsed ? "px-2" : "px-3")}>
            <button
              onClick={() => setCollapsed(c => !c)}
              aria-expanded={!collapsed}
              aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
              className={cn(
                "flex w-full items-center gap-2.5 rounded-md py-[7px] text-xs text-muted-foreground transition-colors hover:bg-muted/60 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                collapsed ? "justify-center px-0" : "px-2.5"
              )}
            >
              {collapsed
                ? <PanelLeftOpen className="h-4 w-4 shrink-0" aria-hidden="true" />
                : <PanelLeftClose className="h-4 w-4 shrink-0" aria-hidden="true" />}
              {!collapsed && "Collapse"}
            </button>
          </div>
        </motion.aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="sticky top-0 z-20 border-b border-border bg-card/95 backdrop-blur">
            <div className="mx-auto flex max-w-[1180px] items-center justify-between gap-4 px-6 py-3">
              <nav className="flex items-center gap-1 md:hidden" aria-label="Sections">
                {NAV.map(({ value, label }) => (
                  <button key={value} onClick={() => setTab(value)}
                    className={cn("rounded px-2 py-1 text-xs",
                      tab === value ? "bg-muted font-medium text-foreground" : "text-muted-foreground")}>
                    {label}
                  </button>
                ))}
              </nav>
              <div className="hidden md:block">
                <h1 className="text-sm font-semibold text-foreground">{current?.label}</h1>
                <p className="text-xs text-muted-foreground">{current?.blurb}</p>
              </div>
              <div className="flex shrink-0 items-center gap-3">
                {/* Reports what the backend is actually doing. A static
                    "Test mode" read the same whether orders reached Razorpay
                    or never left the process, which is the one distinction
                    this badge exists to make. */}
                <span className="hidden items-center gap-1.5 text-xs text-muted-foreground sm:flex"
                      title={
                        config?.payments_mode === "razorpay-test"
                          ? "Real server-to-server calls to Razorpay's test-mode API. Real orders, no real money."
                          : config?.payments_mode === "simulated"
                            ? "No Razorpay credentials configured — orders are generated locally and never leave this machine."
                            : "Payment mode not reported by the backend."
                      }>
                  <span
                    className={cn("h-1.5 w-1.5 rounded-full",
                      config?.payments_mode === "razorpay-test" ? "bg-ok"
                        : config?.payments_mode === "simulated" ? "bg-warn"
                          : "bg-muted-foreground")}
                    aria-hidden="true"
                  />
                  {config?.payments_mode === "razorpay-test" ? "Razorpay test mode"
                    : config?.payments_mode === "simulated" ? "Simulated payments"
                      : "Payments: unknown"}
                </span>
                <span className="mono rounded-md bg-muted px-2 py-1 text-muted-foreground">
                  {agent}
                </span>
              </div>
            </div>
          </header>

          <main id="main" className="flex-1">
            <motion.div
              key={tab}
              initial={{ opacity: 0, y: 6 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.22, ease: EASE }}
              className="mx-auto max-w-[1180px] px-6 py-6"
            >
              <Tabs value={tab} onValueChange={setTab}>
                <TabsContent value="chat" className="focus-visible:outline-none">
                  <Chat agent={agent} config={config} onChanged={() => refresh()}
                        messages={messages} setMessages={setMessages}
                        transactions={transactions} />
                </TabsContent>
                <TabsContent value="dashboard" className="focus-visible:outline-none">
                  <Dashboard mandate={mandate} stats={stats} audit={audit} sweep={sweep}
                             agent={agent} onChanged={() => refresh()} />
                </TabsContent>
                <TabsContent value="checkout" className="focus-visible:outline-none">
                  <Checkout agent={agent} catalog={catalog} onChanged={() => refresh()} />
                </TabsContent>
                <TabsContent value="transactions" className="focus-visible:outline-none">
                  <Transactions transactions={transactions} catalog={catalog} />
                </TabsContent>
                <TabsContent value="hub" className="focus-visible:outline-none">
                  <SecurityHub agent={agent} layers={layers} adversarial={adversarial}
                               onChanged={() => refresh()} />
                </TabsContent>
              </Tabs>
            </motion.div>
          </main>
        </div>
      </div>
    </TooltipProvider>
    </MotionConfig>
  )
}
