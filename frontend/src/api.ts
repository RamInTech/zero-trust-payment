/**
 * Every call in this file goes to an endpoint that already existed, or to a
 * /demo view that reads state. Nothing here decides anything: the frontend
 * renders verdicts, it does not reach them.
 */

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

export interface DemoConfig {
  agent_id: string
  parser: string
  llm_configured: boolean
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
  intentFromSku: (agent_id: string, sku: string, quantity = 1) =>
    post("/api/purchase-intents", { agent_id, sku, quantity }),
  confirm: (requestId: string, amount_paise?: number) =>
    post(`/api/intents/${requestId}/confirm`, amount_paise == null ? {} : { amount_paise }),
  decline: (requestId: string) => post(`/api/intents/${requestId}/decline`),

  freshAgent: () => post("/demo/agent"),
  setPrice: (sku: string, price_paise: number) =>
    post(`/demo/catalog/${sku}/price`, { price_paise }),
  tamper: () => post("/demo/tamper-audit"),
  armTimeout: () => post("/demo/fault/timeout"),
  compromiseParser: (enabled: boolean) =>
    post(`/demo/parser/compromise?enabled=${enabled}`),
}
