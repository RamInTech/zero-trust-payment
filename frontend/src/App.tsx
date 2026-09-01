import { useCallback, useEffect, useState } from "react"
import {
  LayoutDashboard, MessagesSquare, Receipt, ShieldCheck, ShoppingCart,
} from "lucide-react"
import { Tabs, TabsContent } from "@/components/ui/tabs"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Badge } from "@/components/ui/badge"
import { Dashboard } from "@/panels/Dashboard"
import { Chat } from "@/panels/Chat"
import { Checkout } from "@/panels/Checkout"
import { Transactions } from "@/panels/Transactions"
import { SecurityHub } from "@/panels/SecurityHub"
import { api, type DemoConfig, type Json } from "@/api"
import { cn } from "@/lib/utils"

const NAV = [
  { value: "chat", label: "Chat", Icon: MessagesSquare },
  { value: "dashboard", label: "Dashboard", Icon: LayoutDashboard },
  { value: "checkout", label: "Checkout", Icon: ShoppingCart },
  { value: "transactions", label: "Transactions", Icon: Receipt },
  { value: "hub", label: "Security", Icon: ShieldCheck },
]

export default function App() {
  const [agent, setAgent] = useState("agent_alpha")
  const [catalog, setCatalog] = useState<Json[]>([])
  const [mandate, setMandate] = useState<Json | null>(null)
  const [stats, setStats] = useState<Json | null>(null)
  const [audit, setAudit] = useState<Json[]>([])
  const [transactions, setTransactions] = useState<Json[]>([])
  const [layers, setLayers] = useState<Json | null>(null)
  const [adversarial, setAdversarial] = useState<Json | null>(null)
  const [config, setConfig] = useState<DemoConfig | null>(null)
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

  return (
    <TooltipProvider delayDuration={200}>
      <a href="#main"
         className="sr-only focus:not-sr-only focus:absolute focus:left-4 focus:top-4 focus:z-50 focus:rounded-md focus:bg-primary focus:px-4 focus:py-2 focus:text-primary-foreground">
        Skip to content
      </a>

      <div className="flex min-h-screen">
        {/* Sidebar */}
        <aside className="hidden w-[228px] shrink-0 flex-col border-r border-border bg-card md:flex">
          <div className="flex items-center gap-2.5 border-b border-border px-5 py-4">
            <span className="grid h-8 w-8 place-items-center rounded-md bg-primary" aria-hidden="true">
              <ShieldCheck className="h-[17px] w-[17px] text-primary-foreground" />
            </span>
            <span className="text-[13.5px] font-semibold leading-tight text-navy">
              Zero-Trust<br />Authorization
            </span>
          </div>

          <nav className="flex-1 p-3" aria-label="Sections">
            <ul className="grid gap-0.5">
              {NAV.map(({ value, label, Icon }) => (
                <li key={value}>
                  <button
                    onClick={() => setTab(value)}
                    aria-current={tab === value ? "page" : undefined}
                    className={cn(
                      "flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-[13.5px] font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                      tab === value
                        ? "bg-primary/[0.08] text-primary"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    )}
                  >
                    <Icon className="h-4 w-4" aria-hidden="true" />
                    {label}
                  </button>
                </li>
              ))}
            </ul>
          </nav>

          <div className="border-t border-border p-4 text-[11px] leading-relaxed text-muted-foreground">
            Built on Razorpay test mode. Not an official Razorpay product.
            Capture is simulated.
          </div>
        </aside>

        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex items-center justify-between gap-4 border-b border-border bg-card px-6 py-3">
            <div className="flex items-center gap-2 md:hidden">
              {NAV.map(({ value, label }) => (
                <button key={value} onClick={() => setTab(value)}
                  className={cn("rounded px-2 py-1 text-[12.5px]",
                    tab === value ? "bg-primary/[0.08] text-primary" : "text-muted-foreground")}>
                  {label}
                </button>
              ))}
            </div>
            <h1 className="hidden text-[14px] font-semibold text-navy md:block">
              {NAV.find(n => n.value === tab)?.label}
            </h1>
            <div className="flex items-center gap-2.5">
              <Badge variant="ok" dot>live</Badge>
              <span className="mono rounded border border-border bg-muted px-2.5 py-1 text-muted-foreground">
                {agent}
              </span>
            </div>
          </header>

          <main id="main" className="flex-1 px-6 py-6">
            <Tabs value={tab} onValueChange={setTab}>
              <TabsContent value="chat" className="focus-visible:outline-none">
                <Chat agent={agent} config={config} onChanged={() => refresh()} />
              </TabsContent>
              <TabsContent value="dashboard" className="focus-visible:outline-none">
                <Dashboard mandate={mandate} stats={stats} audit={audit} sweep={sweep} />
              </TabsContent>
              <TabsContent value="checkout" className="focus-visible:outline-none">
                <Checkout agent={agent} catalog={catalog} onChanged={() => refresh()} />
              </TabsContent>
              <TabsContent value="transactions" className="focus-visible:outline-none">
                <Transactions transactions={transactions} />
              </TabsContent>
              <TabsContent value="hub" className="focus-visible:outline-none">
                <SecurityHub agent={agent} layers={layers} adversarial={adversarial}
                             onChanged={() => refresh()} />
              </TabsContent>
            </Tabs>
          </main>
        </div>
      </div>
    </TooltipProvider>
  )
}
