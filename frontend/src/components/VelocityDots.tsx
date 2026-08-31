import { useEffect, useRef } from "react"
import { animate, stagger } from "animejs"
import { cn, reducedMotion } from "@/lib/utils"

/** The invisible constraint, made visible before it bites. */
export function VelocityDots({ used, limit }: { used: number; limit: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const previous = useRef(used)

  useEffect(() => {
    const el = ref.current
    if (el && used > previous.current && !reducedMotion()) {
      const filled = el.querySelectorAll("[data-filled='true']")
      if (filled.length) {
        animate(filled, {
          scale: [{ to: 1.45, duration: 180 }, { to: 1, duration: 260 }],
          ease: "outQuad",
          delay: stagger(45),
        })
      }
    }
    previous.current = used
  }, [used])

  const exhausted = used >= limit
  return (
    <div className="flex items-center gap-2">
      <div ref={ref} className="flex items-center gap-1.5" aria-hidden="true">
        {Array.from({ length: limit }).map((_, i) => (
          <span
            key={i}
            data-filled={i < used}
            className={cn(
              "h-2.5 w-2.5 rounded-full border transition-colors",
              i < used
                ? exhausted ? "border-danger bg-danger" : "border-primary bg-primary"
                : "border-border bg-transparent"
            )}
          />
        ))}
      </div>
      {/* Colour is never the only signal. */}
      <span className={cn("mono", exhausted ? "text-danger" : "text-muted-foreground")}>
        {used} of {limit} this hour{exhausted ? " — exhausted" : ""}
      </span>
    </div>
  )
}
