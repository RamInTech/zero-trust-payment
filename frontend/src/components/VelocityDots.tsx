import { useEffect, useRef } from "react"
import { animate, stagger } from "animejs"
import { cn, reducedMotion } from "@/lib/utils"

/**
 * The invisible constraint, made visible before it bites.
 *
 * The segments animate only when a slot is actually consumed -- the budget
 * shrinking is the event worth showing. Idle motion here would be decoration
 * on a security control, which is the thing this project is careful not to do.
 */
export function VelocityDots({ used, limit }: { used: number; limit: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const previous = useRef(used)

  useEffect(() => {
    const el = ref.current
    if (el && used > previous.current && !reducedMotion()) {
      const filled = el.querySelectorAll("[data-filled='true']")
      if (filled.length) {
        animate(filled, {
          scaleY: [{ to: 2.2, duration: 160 }, { to: 1, duration: 280 }],
          ease: "outQuad",
          delay: stagger(40),
        })
      }
    }
    previous.current = used
  }, [used])

  const exhausted = used >= limit
  return (
    <div className="grid gap-1.5">
      <div ref={ref} className="flex max-w-[240px] items-center gap-1" aria-hidden="true">
        {Array.from({ length: limit }).map((_, i) => (
          <span
            key={i}
            data-filled={i < used}
            className={cn(
              "h-1.5 flex-1 rounded-full transition-colors",
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
