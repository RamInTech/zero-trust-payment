import { Download } from "lucide-react"
import { Button } from "@/components/ui/button"
import { rupees } from "@/lib/utils"

/**
 * The bill for a completed purchase.
 *
 * Everything shown is read back from the transaction and its audit trail --
 * nothing is reconstructed from what the UI happened to have on screen. The
 * two lines a normal receipt would not carry (who authorised it, and whether
 * money actually moved) are the two that matter most here, so they are on the
 * face of it rather than in a footnote.
 */

export interface ReceiptData {
  request_id: string
  idempotency_key?: string
  sku: string
  item_name: string
  quantity: number
  amount_paise: number
  order_id?: string
  outcome: string
  executed: boolean
  issued_at: number
  agent_id: string
}

const stamp = (t: number) => new Date(t).toLocaleString("en-GB", {
  day: "2-digit", month: "short", year: "numeric",
  hour: "2-digit", minute: "2-digit", second: "2-digit",
})

/** Plain text, so the saved file is readable without this app. */
export function receiptText(r: ReceiptData): string {
  const unit = r.quantity > 0 ? r.amount_paise / r.quantity : r.amount_paise
  return [
    "ZERO-TRUST PAYMENT AUTHORIZATION",
    "Receipt",
    "",
    `Issued        ${stamp(r.issued_at)}`,
    `Request       ${r.request_id}`,
    `Idempotency   ${r.idempotency_key ?? "—"}`,
    `Order         ${r.order_id ?? "—"}`,
    "",
    "ITEM",
    `  ${r.item_name} (${r.sku})`,
    `  ${r.quantity} x ${rupees(unit)}`,
    "",
    `TOTAL         ${rupees(r.amount_paise)}`,
    "",
    "AUTHORISATION",
    `  Proposed by   ${r.agent_id} (untrusted agent)`,
    "  Confirmed by  a human",
    "  Decided by    POLICY_ENGINE",
    `  Execution     ${r.outcome}`,
    `  Money moved   ${r.executed ? "yes, once" : "no — replayed, already charged"}`,
    "",
    "Razorpay test mode. Order creation is real; capture is simulated.",
    "Not an official Razorpay product.",
    "",
  ].join("\n")
}

function download(r: ReceiptData) {
  const blob = new Blob([receiptText(r)], { type: "text/plain;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = `receipt-${r.request_id}.txt`
  document.body.appendChild(a)
  a.click()
  a.remove()
  // Revoked on the next tick; revoking synchronously can cancel the download
  // in some browsers before it has read the blob.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function Receipt({ data, compact }: { data: ReceiptData; compact?: boolean }) {
  const unit = data.quantity > 0 ? data.amount_paise / data.quantity : data.amount_paise
  return (
    <div className="rounded-lg border border-border bg-card">
      <div className="flex items-start justify-between gap-4 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <p className="text-sm font-semibold text-foreground">Receipt</p>
          <p className="mono text-faint">{data.request_id}</p>
        </div>
        <Button size="sm" variant="outline" onClick={() => download(data)}>
          <Download className="h-3.5 w-3.5" aria-hidden="true" />
          Download
        </Button>
      </div>

      <div className="px-4 py-3">
        <div className="flex items-baseline justify-between gap-4">
          <span className="min-w-0 text-sm text-foreground">
            {data.item_name}
            <span className="mono ml-1.5 text-faint">{data.sku}</span>
          </span>
          <span className="mono shrink-0 text-muted-foreground">
            {data.quantity} × {rupees(unit)}
          </span>
        </div>

        <div className="mt-2.5 flex items-baseline justify-between gap-4 border-t border-border pt-2.5">
          <span className="text-sm font-medium text-foreground">Total</span>
          <span className="text-md font-semibold tabular-nums text-foreground">
            {rupees(data.amount_paise)}
          </span>
        </div>
      </div>

      {!compact && (
        <dl className="divide-rows border-t border-border px-4 py-3">
          <Line label="Proposed by" value={`${data.agent_id} (untrusted)`} />
          <Line label="Confirmed by" value="a human" />
          <Line label="Decided by" value="POLICY_ENGINE" tone="ok" />
          <Line label="Execution" value={data.outcome} />
          <Line
            label="Money moved"
            value={data.executed ? "yes, once" : "no — replayed"}
            tone={data.executed ? undefined : "warn"}
          />
          <Line label="Order" value={data.order_id ?? "—"} />
          <Line label="Idempotency key" value={data.idempotency_key ?? "—"} />
          <Line label="Issued" value={stamp(data.issued_at)} />
        </dl>
      )}

      <p className="border-t border-border px-4 py-2.5 text-2xs text-faint">
        Razorpay test mode — order creation is real, capture is simulated.
      </p>
    </div>
  )
}

function Line({ label, value, tone }: {
  label: string; value: string; tone?: "ok" | "warn"
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5 first:pt-0 last:pb-0">
      <dt className="shrink-0 text-xs text-muted-foreground">{label}</dt>
      <dd className={`mono min-w-0 truncate text-right ${
        tone === "ok" ? "text-ok" : tone === "warn" ? "text-warn" : "text-foreground"
      }`}>
        {value}
      </dd>
    </div>
  )
}
