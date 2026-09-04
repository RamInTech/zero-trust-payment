import { useEffect, useRef } from "react"
import { animate } from "animejs"
import { reducedMotion } from "@/lib/utils"

/**
 * Counts from the previous value to the new one when a figure changes.
 *
 * The motion is doing a job here rather than decorating: these numbers move in
 * response to a purchase the viewer just made, and a figure that slides is a
 * figure they can see change. A number that simply swaps is a number they miss.
 *
 * Anime.js v4 exports named functions; the v3 default `anime({targets})` form
 * no longer exists.
 */
/** Module-level so the default does not change identity on every render and
 *  retrigger the effect. */
const asInteger = (n: number) => Math.round(n).toString()

export function AnimatedNumber({
  value,
  format = asInteger,
  className,
}: {
  value: number
  format?: (n: number) => string
  className?: string
}) {
  const ref = useRef<HTMLSpanElement>(null)
  const current = useRef(value)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (reducedMotion() || current.current === value) {
      el.textContent = format(value)
      current.current = value
      return
    }
    const state = { n: current.current }
    const animation = animate(state, {
      n: value,
      duration: 520,
      ease: "outExpo",
      onUpdate: () => { el.textContent = format(state.n) },
      onComplete: () => { current.current = value },
    })
    return () => { animation.pause() }
  }, [value, format])

  return <span ref={ref} className={className}>{format(value)}</span>
}
