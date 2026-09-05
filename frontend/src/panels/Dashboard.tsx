import { useEffect, useState } from "react"
import { Check, Pencil, Trash2, X } from "lucide-react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { Badge } from "@/components/ui/badge"
import { Row, Rows, Stat } from "@/components/ui/rows"
import { AnimatedNumber } from "@/components/AnimatedNumber"
import { VelocityDots } from "@/components/VelocityDots"
import { ActorTag } from "@/components/ActorTag"
import { rupees } from "@/lib/utils"
import {
  adminUsername, clearAdminSession, isAdminSignedIn, setAdminSession,
} from "@/lib/adminAuth"
import type { Json } from "@/api"

export function Dashboard({
  mandate, stats, audit, sweep, agent, catalog, onChanged,
}: {
  mandate: Json | null; stats: Json | null; audit: Json[]; sweep: Json | null
  agent: string; catalog: Json[]; onChanged: () => void
}) {
  const denials = (stats?.denials ?? {}) as Record<string, number>
  const expiryHours = mandate ? Math.floor(mandate.seconds_until_expiry / 3600) : 0
  const expiryMins = mandate ? Math.floor((mandate.seconds_until_expiry % 3600) / 60) : 0
  const pending = (stats?.pending_verification ?? 0) as number

  // Read once at mount and otherwise driven entirely by this component's own
  // sign-in/sign-out actions -- not re-derived from adminUsername() on every
  // render, so a 401 from one control can flip this to false without every
  // other control's memo of "am I admin" briefly disagreeing about why.
  const [isAdmin, setIsAdmin] = useState(isAdminSignedIn)
  const [adminName, setAdminName] = useState(adminUsername)
  const onUnauthorized = () => { clearAdminSession(); setIsAdmin(false); setAdminName(null) }

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
            <AdminGate isAdmin={isAdmin} adminName={adminName}
                       onSignedIn={name => { setIsAdmin(true); setAdminName(name) }}
                       onSignOut={onUnauthorized} />
            {!mandate ? (
              <p className="text-sm text-muted-foreground">No mandate for this agent.</p>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-2">
                  <CapControl mandate={mandate} agent={agent} onChanged={onChanged}
                              isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
                  <ExpiryControl mandate={mandate} agent={agent} onChanged={onChanged}
                                 expiryHours={expiryHours} expiryMins={expiryMins}
                                 isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
                </div>

                <div className="border-t border-border pt-3.5">
                  <VelocityControl mandate={mandate} agent={agent} onChanged={onChanged}
                                   isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
                </div>

                <div className="border-t border-border pt-3.5">
                  <AllowlistControl mandate={mandate} agent={agent} catalog={catalog}
                                    onChanged={onChanged}
                                    isAdmin={isAdmin} onUnauthorized={onUnauthorized} />
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

      <CatalogManager catalog={catalog} onChanged={onChanged}
                       isAdmin={isAdmin} onUnauthorized={onUnauthorized} />

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


function slugifyItemName(name: string): string {
  return "SKU-" + name.trim().toUpperCase().replace(/[^A-Z0-9]+/g, "-").replace(/^-|-$/g, "")
}

/**
 * Stock, rename, reprice, or unstock a catalog item.
 *
 * Same merchant-only reasoning as the mandate controls above: the price set
 * here is what confirm-time re-validation trusts, so setting or changing it
 * is gated behind the admin login, not left open to whoever has the app
 * open. Every action goes through `setAllowlist`'s sibling routes
 * (`addItem` / `updateItem` / `deleteItem`) rather than any local state --
 * this table always shows what the server actually has.
 */
function CatalogManager({ catalog, onChanged, isAdmin, onUnauthorized }: {
  catalog: Json[]; onChanged: () => void
  isAdmin: boolean; onUnauthorized: () => void
}) {
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState("")
  const [newPrice, setNewPrice] = useState("")
  const [addBusy, setAddBusy] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)

  const [editingSku, setEditingSku] = useState<string | null>(null)
  const [editName, setEditName] = useState("")
  const [editPrice, setEditPrice] = useState("")
  const [rowBusy, setRowBusy] = useState<string | null>(null)
  const [rowError, setRowError] = useState<string | null>(null)

  async function addItem() {
    const rupeesTyped = Number(newPrice)
    if (!newName.trim()) { setAddError("Give the item a name."); return }
    if (!Number.isFinite(rupeesTyped) || rupeesTyped <= 0) {
      setAddError("Enter a price greater than zero."); return
    }
    setAddBusy(true); setAddError(null)
    const res = await api.addItem(
      slugifyItemName(newName), newName.trim(), Math.round(rupeesTyped * 100))
    setAddBusy(false)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setAddError(res.body?.detail?.reason ?? "Could not stock that item.")
      return
    }
    setNewName(""); setNewPrice(""); setAdding(false)
    onChanged()
  }

  function startEdit(item: Json) {
    setEditingSku(item.sku)
    setEditName(item.name)
    setEditPrice(String(item.price_paise / 100))
    setRowError(null)
  }

  async function saveEdit(sku: string) {
    const rupeesTyped = Number(editPrice)
    if (!editName.trim()) { setRowError("Give the item a name."); return }
    if (!Number.isFinite(rupeesTyped) || rupeesTyped <= 0) {
      setRowError("Enter a price greater than zero."); return
    }
    setRowBusy(sku); setRowError(null)
    const res = await api.updateItem(sku, {
      name: editName.trim(), price_paise: Math.round(rupeesTyped * 100),
    })
    setRowBusy(null)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setRowError(res.body?.detail?.reason ?? "Could not update that item.")
      return
    }
    setEditingSku(null)
    onChanged()
  }

  async function removeItem(sku: string) {
    setRowBusy(sku); setRowError(null)
    const res = await api.deleteItem(sku)
    setRowBusy(null)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setRowError(res.body?.detail?.reason ?? "Could not remove that item.")
      return
    }
    if (editingSku === sku) setEditingSku(null)
    onChanged()
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Catalog</CardTitle>
        <button
          onClick={() => { setAdding(a => !a); setAddError(null) }}
          disabled={!isAdmin}
          title={isAdmin ? undefined : "Sign in as admin to edit"}
          className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:text-faint disabled:no-underline disabled:cursor-not-allowed"
        >
          {adding ? "Cancel" : "+ Add item"}
        </button>
      </CardHeader>
      <CardContent className="grid gap-3">
        {adding && (
          <div className="grid gap-2 rounded-md border border-border p-3 sm:grid-cols-[1fr_140px_auto] sm:items-start">
            <input
              value={newName} onChange={e => setNewName(e.target.value)} autoFocus
              placeholder="Item name"
              className="rounded-md border border-border bg-background px-2 py-1 text-sm text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
            <input
              value={newPrice} onChange={e => setNewPrice(e.target.value)}
              type="number" min="1" placeholder="Price in ₹"
              onKeyDown={e => { if (e.key === "Enter") addItem() }}
              className="rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
            />
            <Button size="sm" onClick={addItem} disabled={addBusy}>
              {addBusy ? "Adding…" : "Add"}
            </Button>
            {addError && <p className="text-2xs text-danger sm:col-span-3">{addError}</p>}
          </div>
        )}

        <div className="max-h-80 overflow-y-auto rounded-md border border-border">
          <table className="w-full text-left text-xs">
            <thead className="sticky top-0 bg-card">
              <tr className="border-b border-border">
                <th className="label px-3 py-2 font-medium">SKU</th>
                <th className="label px-3 py-2 font-medium">Name</th>
                <th className="label w-24 px-3 py-2 font-medium">Price</th>
                <th className="w-16 px-3 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {catalog.map(item => {
                const isEditing = editingSku === item.sku
                const busy = rowBusy === item.sku
                return (
                  <tr key={item.sku}>
                    <td className="mono px-3 py-2 text-faint">{item.sku}</td>
                    <td className="px-3 py-2">
                      {isEditing ? (
                        <input
                          value={editName} onChange={e => setEditName(e.target.value)}
                          autoFocus
                          className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      ) : item.name}
                    </td>
                    <td className="px-3 py-2 tabular-nums">
                      {isEditing ? (
                        <input
                          value={editPrice} onChange={e => setEditPrice(e.target.value)}
                          type="number" min="1"
                          onKeyDown={e => { if (e.key === "Enter") saveEdit(item.sku) }}
                          className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
                        />
                      ) : rupees(item.price_paise)}
                    </td>
                    <td className="px-3 py-2">
                      {isEditing ? (
                        <div className="flex gap-2">
                          <button onClick={() => saveEdit(item.sku)} disabled={busy}
                                  title="Save" aria-label={`Save ${item.sku}`}
                                  className="text-faint hover:text-ok disabled:pointer-events-none disabled:opacity-50">
                            <Check className="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                          <button onClick={() => setEditingSku(null)} disabled={busy}
                                  title="Cancel" aria-label="Cancel edit"
                                  className="text-faint hover:text-foreground disabled:pointer-events-none disabled:opacity-50">
                            <X className="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                        </div>
                      ) : (
                        <div className="flex gap-2">
                          <button onClick={() => startEdit(item)} disabled={!isAdmin || busy}
                                  title={isAdmin ? `Edit ${item.sku}` : "Sign in as admin to edit"}
                                  aria-label={`Edit ${item.sku}`}
                                  className="text-faint hover:text-foreground disabled:pointer-events-none disabled:opacity-50">
                            <Pencil className="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                          <button onClick={() => removeItem(item.sku)} disabled={!isAdmin || busy}
                                  title={isAdmin ? `Remove ${item.sku}` : "Sign in as admin to edit"}
                                  aria-label={`Remove ${item.sku}`}
                                  className="text-faint hover:text-danger disabled:pointer-events-none disabled:opacity-50">
                            <Trash2 className="h-3.5 w-3.5" aria-hidden="true" />
                          </button>
                        </div>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        {rowError && <p className="text-2xs text-danger">{rowError}</p>}
      </CardContent>
    </Card>
  )
}


/**
 * The sign-in strip gating every mandate-edit control below.
 *
 * This is convenience, not the security boundary -- the server refuses every
 * edit route without a valid session regardless of what this component shows
 * or hides. What this buys is a merchant not discovering that the hard way:
 * the "Change" links below are disabled before a click ever reaches the
 * network, with a login right where the edit was attempted rather than a
 * separate page to find first.
 */
function AdminGate({ isAdmin, adminName, onSignedIn, onSignOut }: {
  isAdmin: boolean; adminName: string | null
  onSignedIn: (username: string) => void
  onSignOut: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [signingIn, setSigningIn] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function signIn() {
    if (!username || !password) {
      setError("Enter a username and password.")
      return
    }
    setSigningIn(true); setError(null)
    const res = await api.adminLogin(username, password)
    setSigningIn(false)
    if (!res.ok) {
      setError(res.body?.detail?.reason ?? "Sign-in failed.")
      return
    }
    setAdminSession(res.body.session_token, res.body.expires_in_seconds, username)
    setPassword("")
    setExpanded(false)
    onSignedIn(username)
  }

  if (isAdmin) {
    return (
      <div className="flex items-center justify-between rounded-md bg-ok/[0.06] px-3.5 py-2 text-xs">
        <span className="text-foreground">
          Signed in as admin <span className="mono text-muted-foreground">{adminName}</span>
        </span>
        <button onClick={onSignOut}
                className="text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring">
          Sign out
        </button>
      </div>
    )
  }

  if (!expanded) {
    return (
      <div className="flex items-center justify-between rounded-md bg-muted/60 px-3.5 py-2 text-xs">
        <span className="text-muted-foreground">
          Sign in as admin to edit the mandate below.
        </span>
        <button onClick={() => setExpanded(true)}
                className="text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring">
          Sign in
        </button>
      </div>
    )
  }

  return (
    <div className="rounded-md border border-border bg-muted/40 px-3.5 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor="admin-username" className="sr-only">Admin username</label>
        <input id="admin-username" value={username} autoFocus
               placeholder="username"
               onChange={e => setUsername(e.target.value)}
               onKeyDown={e => e.key === "Enter" && signIn()}
               className="h-8 w-32 rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring" />
        <label htmlFor="admin-password" className="sr-only">Admin password</label>
        <input id="admin-password" type="password" value={password}
               placeholder="password"
               onChange={e => setPassword(e.target.value)}
               onKeyDown={e => e.key === "Enter" && signIn()}
               className="h-8 w-32 rounded-md border border-border bg-background px-2 text-xs text-foreground focus-visible:ring-2 focus-visible:ring-ring" />
        <Button size="sm" onClick={signIn} disabled={signingIn}>
          {signingIn ? "Signing in…" : "Sign in"}
        </Button>
        <Button size="sm" variant="ghost"
                onClick={() => { setExpanded(false); setError(null) }}>
          Cancel
        </Button>
      </div>
      {error && <p className="mt-1.5 text-2xs text-danger">{error}</p>}
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
function CapControl({ mandate, agent, onChanged, isAdmin, onUnauthorized }: {
  mandate: Json; agent: string; onChanged: () => void
  isAdmin: boolean; onUnauthorized: () => void
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
      if (res.status === 401) onUnauthorized()
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
            disabled={!isAdmin}
            title={isAdmin ? undefined : "Sign in as admin to edit"}
            className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:text-faint disabled:no-underline disabled:cursor-not-allowed"
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

/**
 * When the mandate lapses, editable.
 *
 * Takes a DURATION from now rather than an absolute time, matching how a
 * mandate is issued in the first place (`run_ui.py`: "now plus a window") --
 * asking a merchant to type a Unix timestamp invites exactly the kind of
 * off-by-one-timezone mistake this project's audit story argues against
 * elsewhere.
 */
const EXPIRY_PRESETS = [
  { label: "1 hour", hours: 1 },
  { label: "24 hours", hours: 24 },
  { label: "7 days", hours: 24 * 7 },
]

function ExpiryControl({ mandate, agent, onChanged, expiryHours, expiryMins, isAdmin, onUnauthorized }: {
  mandate: Json; agent: string; onChanged: () => void
  expiryHours: number; expiryMins: number
  isAdmin: boolean; onUnauthorized: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [hoursValue, setHoursValue] = useState("24")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function save(hours: number) {
    if (!Number.isFinite(hours) || hours <= 0) {
      setError("Enter a number of hours greater than zero.")
      return
    }
    setSaving(true); setError(null)
    const res = await api.setExpiry(agent, hours * 3600)
    setSaving(false)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setError(res.body?.detail?.reason ?? "Could not update the expiry.")
      return
    }
    setEditing(false)
    onChanged()
  }

  if (!editing) {
    return (
      <div>
        <div className="label mb-1">Expires in</div>
        <div className="flex items-baseline gap-2">
          <span className="text-lg font-semibold tabular-nums text-foreground">
            {mandate.expired ? "Expired" : `${expiryHours}h ${expiryMins}m`}
          </span>
          <button
            onClick={() => { setEditing(true); setError(null) }}
            disabled={!isAdmin}
            title={isAdmin ? undefined : "Sign in as admin to edit"}
            className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:text-faint disabled:no-underline disabled:cursor-not-allowed"
          >
            Change
          </button>
        </div>
        <p className="mt-1 text-2xs text-faint">Extends from now, not from the old expiry.</p>
      </div>
    )
  }

  return (
    <div>
      <label htmlFor="expiry-input" className="label mb-1 block">Extend by (hours)</label>
      <div className="flex flex-wrap items-center gap-2">
        <input
          id="expiry-input" type="number" min="1" step="1" value={hoursValue}
          autoFocus
          onChange={e => setHoursValue(e.target.value)}
          onKeyDown={e => {
            if (e.key === "Enter") save(Number(hoursValue))
            if (e.key === "Escape") { setEditing(false); setError(null) }
          }}
          className="w-20 rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        <Button size="sm" onClick={() => save(Number(hoursValue))} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="outline"
                onClick={() => { setEditing(false); setError(null) }}>
          Cancel
        </Button>
      </div>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {EXPIRY_PRESETS.map(p => (
          <button key={p.label} onClick={() => { setHoursValue(String(p.hours)); save(p.hours) }}
                  disabled={saving}
                  className="rounded-full border border-border px-2 py-0.5 text-2xs text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40">
            +{p.label}
          </button>
        ))}
      </div>
      {error
        ? <p className="mt-1 text-2xs text-danger">{error}</p>
        : <p className="mt-1 text-2xs text-faint">
            Replaces the mandate; the old one is revoked, not edited.
          </p>}
    </div>
  )
}

/**
 * The spend-frequency limit and its window, editable.
 *
 * Displayed in minutes for editing (matching the cool-down display elsewhere
 * on this page) even though it is stored and enforced in seconds -- a
 * merchant thinking in "3 per hour" should not have to first convert to 3600.
 */
function VelocityControl({ mandate, agent, onChanged, isAdmin, onUnauthorized }: {
  mandate: Json; agent: string; onChanged: () => void
  isAdmin: boolean; onUnauthorized: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [limitValue, setLimitValue] = useState("")
  const [minutesValue, setMinutesValue] = useState("")
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setLimitValue(String(mandate.velocity_limit))
    setMinutesValue(String(Math.round(mandate.velocity_window_secs / 60)))
  }, [mandate.velocity_limit, mandate.velocity_window_secs])

  async function save() {
    const limit = Number(limitValue)
    const minutes = Number(minutesValue)
    if (!Number.isInteger(limit) || limit <= 0) {
      setError("Limit must be a whole number greater than zero.")
      return
    }
    if (!Number.isFinite(minutes) || minutes <= 0) {
      setError("Window must be a number of minutes greater than zero.")
      return
    }
    setSaving(true); setError(null)
    const res = await api.setVelocity(agent, limit, minutes * 60)
    setSaving(false)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setError(res.body?.detail?.reason ?? "Could not update the velocity limit.")
      return
    }
    setEditing(false)
    onChanged()
  }

  if (!editing) {
    return (
      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="label">Velocity</div>
          <button
            onClick={() => { setEditing(true); setError(null) }}
            disabled={!isAdmin}
            title={isAdmin ? undefined : "Sign in as admin to edit"}
            className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:text-faint disabled:no-underline disabled:cursor-not-allowed"
          >
            Change
          </button>
        </div>
        <VelocityDots used={mandate.velocity_used} limit={mandate.velocity_limit} />
        <p className="mt-1.5 text-2xs text-faint">
          {mandate.velocity_limit} per {Math.round(mandate.velocity_window_secs / 60)} minutes.
        </p>
      </div>
    )
  }

  return (
    <div>
      <div className="label mb-1">Velocity limit</div>
      <div className="flex flex-wrap items-center gap-2">
        <input
          type="number" min="1" step="1" value={limitValue} aria-label="Purchases allowed"
          autoFocus
          onChange={e => setLimitValue(e.target.value)}
          onKeyDown={e => e.key === "Escape" && setEditing(false)}
          className="w-16 rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="text-xs text-muted-foreground">per</span>
        <input
          type="number" min="1" step="1" value={minutesValue} aria-label="Window in minutes"
          onChange={e => setMinutesValue(e.target.value)}
          onKeyDown={e => e.key === "Escape" && setEditing(false)}
          className="w-20 rounded-md border border-border bg-background px-2 py-1 text-sm tabular-nums text-foreground focus-visible:ring-2 focus-visible:ring-ring"
        />
        <span className="text-xs text-muted-foreground">minutes</span>
      </div>
      <div className="mt-2 flex gap-2">
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
            Takes effect on the next request; the used count carries over.
          </p>}
    </div>
  )
}

/**
 * Which catalog items the agent may buy at all, editable.
 *
 * "Any item" is the same wildcard the backend already understands (ANY_SKU) --
 * choosing it does not remove the per-transaction cap, which is why it is not
 * treated here as "no limits" but as one specific, deliberate choice among
 * several.
 */
function AllowlistControl({ mandate, agent, catalog, onChanged, isAdmin, onUnauthorized }: {
  mandate: Json; agent: string; catalog: Json[]; onChanged: () => void
  isAdmin: boolean; onUnauthorized: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [allowAny, setAllowAny] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [quickBusy, setQuickBusy] = useState(false)
  const [addValue, setAddValue] = useState("")

  function startEditing() {
    setAllowAny(!!mandate.allows_any_sku)
    setSelected(new Set(mandate.allows_any_sku ? [] : mandate.allowed_skus))
    setError(null)
    setEditing(true)
  }

  function toggle(sku: string) {
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(sku)) next.delete(sku); else next.add(sku)
      return next
    })
  }

  async function save() {
    if (!allowAny && selected.size === 0) {
      setError("Pick at least one item, or choose “any item”.")
      return
    }
    setSaving(true); setError(null)
    const res = await api.setAllowlist(agent, [...selected], allowAny)
    setSaving(false)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setError(res.body?.detail?.reason ?? "Could not update the allowed items.")
      return
    }
    setEditing(false)
    onChanged()
  }

  /**
   * Add or remove a single item without opening the full editor. Still goes
   * through `setAllowlist` -- there is no partial-edit route on the backend,
   * only whole-list replacement -- so this just computes the next full list
   * from the current one and sends that, rather than duplicating validation
   * client-side.
   */
  async function quickChange(next: string[]) {
    if (next.length === 0) {
      setError("Pick at least one item, or choose “any item”.")
      return
    }
    setQuickBusy(true); setError(null)
    const res = await api.setAllowlist(agent, next, false)
    setQuickBusy(false)
    if (!res.ok) {
      if (res.status === 401) onUnauthorized()
      setError(res.body?.detail?.reason ?? "Could not update the allowed items.")
      return
    }
    onChanged()
  }

  // With the wildcard active there is no concrete list to remove FROM, but
  // there is still a meaningful "add" action: naming one specific item turns
  // the wildcard off and starts a list containing just that item. So the add
  // control has to render in both states; only the per-chip remove buttons
  // are wildcard-mode-specific, since there are no chips to attach them to.
  const removableSkus: string[] = mandate.allows_any_sku ? [] : mandate.allowed_skus
  const addableItems = mandate.allows_any_sku ? catalog : catalog.filter(
    item => !removableSkus.includes(item.sku))

  if (!editing) {
    return (
      <div>
        <div className="mb-2 flex items-center justify-between">
          <div className="label">Allowed items</div>
          <button
            onClick={startEditing}
            disabled={!isAdmin}
            title={isAdmin ? undefined : "Sign in as admin to edit"}
            className="text-xs text-primary underline-offset-2 hover:underline focus-visible:ring-2 focus-visible:ring-ring disabled:text-faint disabled:no-underline disabled:cursor-not-allowed"
          >
            Change
          </button>
        </div>
        {mandate.allows_any_sku ? (
          <p className="mb-2 text-xs text-muted-foreground">
            Any item in the catalog. The per-transaction limit is what refuses
            a purchase, not a fixed list — so anything stocked later is
            covered without reissuing the mandate.
          </p>
        ) : (
          <div className="mb-2 flex flex-wrap gap-1.5">
            {mandate.allowed_skus.map((s: string) => (
              <span key={s}
                    className="mono flex items-center gap-1 rounded bg-muted py-0.5 pl-1.5 pr-1 text-foreground">
                {s}
                {isAdmin && (
                  <button
                    onClick={() => quickChange(removableSkus.filter(sku => sku !== s))}
                    disabled={quickBusy}
                    title={`Remove ${s} from the allowed items`}
                    aria-label={`Remove ${s} from the allowed items`}
                    className="rounded-sm text-faint hover:text-danger disabled:pointer-events-none disabled:opacity-50"
                  >
                    <X className="h-3 w-3" aria-hidden="true" />
                  </button>
                )}
              </span>
            ))}
          </div>
        )}
        {isAdmin && addableItems.length > 0 && (
          <select
            value={addValue}
            disabled={quickBusy}
            title={mandate.allows_any_sku
              ? "Naming an item here switches off “any item” and starts a specific list."
              : undefined}
            onChange={e => {
              const sku = e.target.value
              setAddValue("")
              if (sku) quickChange([...removableSkus, sku])
            }}
            className="w-full rounded-md border border-border bg-background px-2 py-1 text-xs text-foreground disabled:opacity-50"
          >
            <option value="">+ Add an item…</option>
            {addableItems.map(item => (
              <option key={item.sku} value={item.sku}>
                {item.name} ({item.sku})
              </option>
            ))}
          </select>
        )}
        {error && <p className="mt-1 text-2xs text-danger">{error}</p>}
      </div>
    )
  }

  return (
    <div>
      <div className="label mb-1.5">Allowed items</div>
      <label className="mb-2 flex items-center gap-2 text-xs text-foreground">
        <input type="checkbox" checked={allowAny}
               onChange={e => setAllowAny(e.target.checked)} />
        Any item in the catalog
      </label>
      {!allowAny && (
        <div className="mb-2 grid max-h-40 gap-1 overflow-y-auto rounded-md border border-border p-2 sm:grid-cols-2">
          {catalog.map(item => (
            <label key={item.sku} className="flex items-center gap-1.5 text-xs text-foreground">
              <input type="checkbox" checked={selected.has(item.sku)}
                     onChange={() => toggle(item.sku)} />
              <span className="min-w-0 truncate">{item.name}</span>
              <span className="mono shrink-0 text-faint">{item.sku}</span>
            </label>
          ))}
        </div>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </Button>
        <Button size="sm" variant="outline" onClick={() => setEditing(false)}>
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
