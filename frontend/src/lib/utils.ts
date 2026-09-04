import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/**
 * One easing curve for the whole app, so motion reads as a single system
 * rather than as each component's own idea of a nice transition.
 */
export const EASE = [0.22, 1, 0.36, 1] as const

/** Respect the OS setting. Both animation libraries are gated on this. */
export const reducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches

export const rupees = (paise: number | null | undefined) =>
  paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })
