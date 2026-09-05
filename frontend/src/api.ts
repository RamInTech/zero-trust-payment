/**
 * Every call in this file goes to an endpoint that already existed, or to a
 * /demo view that reads state. Nothing here decides anything: the frontend
 * renders verdicts, it does not reach them.
 */

import { seal } from "@/lib/e2e"
import { adminToken } from "@/lib/adminAuth"

export type Json = Record<string, any>

async function call(path: string, init?: RequestInit): Promise<{ status: number; ok: boolean; body: Json }> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  })
  let body: Json = {}
  try { body = await res.json() } catch { /* empty body is fine */ }
  return { status: res.status, ok: res.ok, body }
}

const post = (path: string, payload?: Json) =>
  call(path, { method: "POST", body: JSON.stringify(payload ?? {}) })

/**
 * A request carrying the admin session, for every mandate-edit and
 * catalog-write route.
 *
 * If there is no session (never signed in, or it expired), the request still
 * goes out with no Authorization header -- the server is what actually
 * enforces the login, by refusing with 401. This layer only saves the caller
 * from remembering to attach the header; it is not where the security lives.
 */
const adminCall = (method: string, path: string, payload?: Json) => {
  const token = adminToken()
  // `call()` spreads `init` over its own default headers rather than
  // deep-merging, so a bare `headers: {Authorization}` here would silently
  // drop Content-Type instead of adding to it.
  const headers: Record<string, string> = { "Content-Type": "application/json" }
  if (token) headers.Authorization = `Bearer ${token}`
  return call(path, { method, body: JSON.stringify(payload ?? {}), headers })
}
const adminPost = (path: string, payload?: Json) => adminCall("POST", path, payload)
const adminDelete = (path: string) => adminCall("DELETE", path)

export interface Pending {
  request_id: string
  agent_id: string
  sku: string
  item_name: string
  quantity: number
  displayed_amount_paise: number
  prompt: string
  status: string
  parser: string
  idempotency_key?: string
}

export interface ConfirmResult {
  request_id: string
  approved: boolean
  rule: string | null
  reason: string | null
  idempotency_outcome: string | null
  executed: boolean
  response: Json | null
}

export interface BasketLine extends Pending {}

export interface DemoConfig {
  agent_id: string
  parser: string
  llm_configured: boolean
  llm_providers: string[]
  /** "razorpay-test" | "simulated" | "unknown" — what the backend really does. */
  payments_mode: string
  conversational: boolean
}

export const api = {
  catalog: () => call("/api/catalog"),
  config: () => call("/demo/config"),
  mandate: (agent: string) => call(`/demo/mandate/${agent}`),
  stats: () => call("/demo/stats"),
  transactions: (limit = 25) => call(`/demo/transactions?limit=${limit}`),
  audit: (limit = 60) => call(`/demo/audit/recent?limit=${limit}`),
  auditFor: (requestId: string) => call(`/api/audit/${requestId}`),
  explain: (requestId: string) => call(`/api/explain/${requestId}`),
  sweep: () => call("/demo/sweep"),
  sweepNow: () => post("/demo/sweep/run"),
  layers: () => call("/demo/security/layers"),
  adversarial: () => call("/demo/adversarial"),
  pending: (requestId: string) => call(`/demo/pending/${requestId}`),

  intentFromText: (agent_id: string, text: string) => post("/api/intents", { agent_id, text }),
  e2ePublicKey: () => call("/api/e2e/public-key"),
  recommendations: (sku: string, limit = 1) =>
    call(`/api/recommendations/${sku}?limit=${limit}`),
  intentFromTextSealed: async (agent_id: string, text: string) => {
    const key = await call("/api/e2e/public-key")
    if (!key.ok) return key
    const sealed = seal(text, key.body.public_key_b64)
    return post("/api/intents", { agent_id, sealed })
  },
  /** Same call, but also returns the ciphertext that was actually sent --
   * for demonstrations that need to SHOW the bytes, not just use them. */
  intentFromTextSealedInspect: async (agent_id: string, text: string) => {
    const key = await call("/api/e2e/public-key")
    if (!key.ok) return { res: key, sealed: null }
    const sealed = seal(text, key.body.public_key_b64)
    const res = await post("/api/intents", { agent_id, sealed })
    return { res, sealed }
  },
  intentFromSku: (agent_id: string, sku: string, quantity = 1) =>
    post("/api/purchase-intents", { agent_id, sku, quantity }),
  confirm: (requestId: string, amount_paise?: number) =>
    post(`/api/intents/${requestId}/confirm`, amount_paise == null ? {} : { amount_paise }),
  decline: (requestId: string) => post(`/api/intents/${requestId}/decline`),

  freshAgent: () => post("/demo/agent"),
  // Admin-only from here: the server refuses every one of these without a
  // valid session, regardless of what this client sends.
  adminLogin: (username: string, password: string) =>
    post("/demo/admin/login", { username, password }),
  revokeMandate: (agent: string) => adminPost(`/demo/mandate/${agent}/revoke`),
  setCap: (agent: string, max_amount_paise: number) =>
    adminPost(`/demo/mandate/${agent}/cap`, { max_amount_paise }),
  setAllowlist: (agent: string, skus: string[], allow_any = false) =>
    adminPost(`/demo/mandate/${agent}/allowlist`, { skus, allow_any }),
  setExpiry: (agent: string, extends_seconds: number) =>
    adminPost(`/demo/mandate/${agent}/expiry`, { extends_seconds }),
  setVelocity: (agent: string, velocity_limit: number, velocity_window_secs: number) =>
    adminPost(`/demo/mandate/${agent}/velocity`, { velocity_limit, velocity_window_secs }),
  // Admin-only from here too: stocking, renaming, repricing, and unstocking
  // an item are all merchant decisions, same reasoning as the mandate routes.
  addItem: (sku: string, name: string, price_paise: number) =>
    adminPost("/demo/catalog", { sku, name, price_paise }),
  setPrice: (sku: string, price_paise: number) =>
    adminPost(`/demo/catalog/${sku}/price`, { price_paise }),
  updateItem: (sku: string, changes: { name?: string; price_paise?: number }) =>
    adminPost(`/demo/catalog/${sku}`, changes),
  deleteItem: (sku: string) => adminDelete(`/demo/catalog/${sku}`),
  tamper: () => post("/demo/tamper-audit"),
  chainBreakDemo: () => post("/demo/audit/chain-break"),
  auditWriteBlocksPaymentDemo: () => post("/demo/audit/write-blocks-payment"),
  armTimeout: () => post("/demo/fault/timeout"),
  compromiseParser: (enabled: boolean) =>
    post(`/demo/parser/compromise?enabled=${enabled}`),
  simulateWebhook: (tamper: boolean) =>
    post(`/demo/webhook/simulate?tamper=${tamper}`),
  /** No token at all -- proves the server refuses, not just the UI. */
  probeAdminWithoutAuth: (agent: string) =>
    call(`/demo/mandate/${agent}/cap`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_amount_paise: 1 }),
    }),
}
