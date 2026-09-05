import { useState } from "react"
import { motion } from "framer-motion"
import { Ban, KeyRound } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"

/**
 * Hits a real mandate-edit route with no session at all -- not through
 * `adminPost` (which would attach whatever token this browser tab happens to
 * hold), but a bare request, so the refusal proves the server enforces the
 * login rather than this page merely choosing not to send one.
 */
export function AdminAuthProbe({ onChanged }: { freshAgent?: () => Promise<string>; onChanged?: () => void }) {
  const [status, setStatus] = useState<number | null>(null)
  const [reason, setReason] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true)
    const res = await api.probeAdminWithoutAuth("agent_alpha")
    setStatus(res.status)
    setReason(typeof res.body?.detail === "string" ? res.body.detail
      : res.body?.detail?.reason ?? res.body?.detail ?? null)
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Sends a raw cap-edit request with no Authorization header at all.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <KeyRound className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Sending…" : "Try to edit with no login"}
        </Button>
      </div>

      {status != null && (
        <motion.p initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                  className="mono flex items-center gap-1.5 text-2xs text-danger">
          <Ban className="h-3.5 w-3.5" aria-hidden="true" />
          {status} {reason ?? "refused"} — cap unchanged
        </motion.p>
      )}
    </div>
  )
}
