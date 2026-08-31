import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

/** Respect the OS setting. Both animation libraries are gated on this. */
export const reducedMotion = () =>
  typeof window !== "undefined" &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches

export const rupees = (paise: number | null | undefined) =>
  paise == null ? "—" : "₹" + (paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2 })
