import { useEffect, useRef } from "react"
import { animate } from "animejs"
import { reducedMotion } from "@/lib/utils"

/**
 * Anime.js drives the micro-interactions -- counters, meters, pulses -- while
 * Framer Motion handles layout and state transitions. Two libraries with one
 * job each, rather than two doing the same job.
 *
 * Anime.js v4 exports named functions; the v3 default `anime({targets})` form
 * no longer exists.
 */
export function AnimatedNumber({
  value,
  format = (n: number) => Math.round(n).toString(),
  className,
}: {
  value: number
  format?: (n: number) => string
  className?: string
}) {
  const ref = useRef<HTMLSpanElement>(null)
  const current = useRef(0)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    if (reducedMotion()) {
      el.textContent = format(value)
      current.current = value
      return
    }
    const state = { n: current.current }
    const animation = animate(state, {
      n: value,
      duration: 620,
      ease: "outExpo",
      onUpdate: () => { el.textContent = format(state.n) },
      onComplete: () => { current.current = value },
    })
    return () => { animation.pause() }
  }, [value, format])

  return <span ref={ref} className={className}>{format(value)}</span>
}
