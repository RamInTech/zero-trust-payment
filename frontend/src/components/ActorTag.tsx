import { cn } from "@/lib/utils"

/**
 * The actor is the trust boundary made visible: AGENT proposes, HUMAN
 * confirms, POLICY_ENGINE is the only one that ever authorises.
 */
const TONE: Record<string, string> = {
  AGENT: "text-warn",
  HUMAN: "text-primary",
  POLICY_ENGINE: "text-ok",
  SYSTEM: "text-muted-foreground",
  PROVIDER: "text-muted-foreground",
}

export function ActorTag({ actor, className }: { actor: string; className?: string }) {
  return (
    <span className={cn("mono text-[10.5px] font-semibold tracking-wide", TONE[actor] ?? "text-muted-foreground", className)}>
      {actor}
    </span>
  )
}
