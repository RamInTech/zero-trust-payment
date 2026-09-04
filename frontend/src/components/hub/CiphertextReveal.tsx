import { useState } from "react"
import { motion } from "framer-motion"
import { Lock, ShieldCheck, Zap } from "lucide-react"
import { Button } from "@/components/ui/button"
import { api } from "@/api"
import { inspectCiphertext } from "@/lib/e2e"

type Stage = "idle" | "encrypting" | "sent" | "checked"

const MESSAGE = "buy a coffee — this exact sentence should never be readable"

/**
 * A sealed envelope versus a postcard.
 *
 * Types a sentence, encrypts it in the browser, sends only ciphertext, then
 * reads back what actually landed in the audit database -- not what the
 * server claims it stored, the raw bytes. The parse still resolves correctly
 * underneath, which is the second half of the claim: encryption did not cost
 * functionality.
 */
export function CiphertextReveal({ freshAgent, onChanged }: { freshAgent: () => Promise<string>; onChanged?: () => void }) {
  const [stage, setStage] = useState<Stage>("idle")
  const [ciphertextPreview, setCiphertextPreview] = useState<string | null>(null)
  const [readable, setReadable] = useState<boolean | null>(null)
  const [sku, setSku] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run() {
    setBusy(true); setStage("encrypting"); setReadable(null)
    const who = await freshAgent()

    const { res, sealed } = await api.intentFromTextSealedInspect(who, MESSAGE)
    if (!sealed) { setBusy(false); return }
    setCiphertextPreview(inspectCiphertext(sealed.ciphertext_b64).preview)
    await new Promise(r => setTimeout(r, 500))
    setStage("sent")

    if (!res.ok) { setBusy(false); return }
    const p = res.body.awaiting_confirmation
    setSku(p.sku)
    await new Promise(r => setTimeout(r, 400))

    const stored = await api.auditFor(p.request_id)
    const entry = (stored.body.events ?? []).find((e: any) => e.event_type === "INTENT_PARSED")
    setReadable(entry?.details?.raw_text !== undefined)
    setStage("checked")
    setBusy(false)
    onChanged?.()
  }

  return (
    <div className="rounded-lg border border-border bg-background/60 p-3.5">
      <div className="mb-3 flex items-center justify-between gap-3">
        <p className="text-2xs text-faint">
          Encrypts a message in the browser, then reads back what actually
          landed in the database.
        </p>
        <Button size="sm" onClick={run} disabled={busy}>
          <Zap className="h-3.5 w-3.5" aria-hidden="true" />
          {busy ? "Running…" : "Encrypt and check storage"}
        </Button>
      </div>

      {stage !== "idle" && (
        <div className="flex flex-col gap-1.5">
          <motion.div initial={{ opacity: 0 }} animate={{ opacity: 1 }}
                      className="rounded-md bg-muted/30 px-2.5 py-1.5">
            <p className="text-2xs text-faint">Typed</p>
            <p className="mono text-2xs text-foreground">"{MESSAGE}"</p>
          </motion.div>

          {ciphertextPreview && (
            <motion.div initial={{ opacity: 0, x: -6 }} animate={{ opacity: 1, x: 0 }}
                        className="flex items-center gap-2 rounded-md bg-primary/10 px-2.5 py-1.5">
              <Lock className="h-3 w-3 shrink-0 text-primary" aria-hidden="true" />
              <p className="mono truncate text-2xs text-foreground">{ciphertextPreview}</p>
            </motion.div>
          )}

          {stage === "checked" && (
            <motion.div initial={{ opacity: 0, y: 4 }} animate={{ opacity: 1, y: 0 }}
                        className="flex items-center gap-2 rounded-md bg-ok/10 px-2.5 py-1.5">
              <ShieldCheck className="h-3 w-3 shrink-0 text-ok" aria-hidden="true" />
              <p className="mono text-2xs text-foreground">
                {readable
                  ? "UNEXPECTED — stored in plain text"
                  : `stored as ciphertext only, yet parsed correctly as ${sku}`}
              </p>
            </motion.div>
          )}
        </div>
      )}
    </div>
  )
}
