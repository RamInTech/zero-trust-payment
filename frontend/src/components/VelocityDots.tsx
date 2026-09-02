import { cn } from "@/lib/utils"

/** The invisible constraint, made visible before it bites. */
export function VelocityDots({ used, limit }: { used: number; limit: number }) {
  const exhausted = used >= limit
  return (
    <div className="grid gap-1.5">
      <div className="flex max-w-[240px] items-center gap-1" aria-hidden="true">
        {Array.from({ length: limit }).map((_, i) => (
          <span
            key={i}
            className={cn(
              "h-1.5 flex-1 rounded-full",
              i < used
                ? exhausted ? "bg-danger" : "bg-primary"
                : "bg-border"
            )}
          />
        ))}
      </div>
      {/* Colour is never the only signal. */}
      <span className={cn("text-xs", exhausted ? "text-danger" : "text-muted-foreground")}>
        {used} of {limit} used this hour{exhausted ? " — exhausted" : ""}
      </span>
    </div>
  )
}
