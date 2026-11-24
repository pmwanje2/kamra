/** Shared housekeeping status display tones, readiness priority weights,
 *  and toggle transitions so Tape Chart, Queue Dock, and P35 (Pickup / OS)
 *  stay synchronized from one source. */

export type HkStatus =
  | "Inspected"
  | "Ready"
  | "Clean"
  | "Pickup"
  | "Dirty"
  | "Out of Service"
  | "Out of Order"

export const HK_READINESS_ORDER: readonly HkStatus[] = [
  "Inspected",
  "Ready",
  "Clean",
  "Pickup",
  "Dirty",
  "Out of Service",
  "Out of Order",
] as const

export const HK_READINESS_WEIGHT: Record<string, number> = {
  Inspected: 100,
  Ready: 90,
  Clean: 50,
  Pickup: 25,
  Dirty: 0,
  "Out of Service": -100,
  OS: -100,
  "Out of Order": -200,
  OO: -200,
}

export interface HkToneStyle {
  badge: string
  dot: string
  text: string
}

export const HK_TONES: Record<string, HkToneStyle> = {
  Inspected: {
    badge: "border-sky-300 bg-sky-50 text-sky-900",
    dot: "bg-sky-500",
    text: "text-sky-700",
  },
  Ready: {
    badge: "border-teal-300 bg-teal-50 text-teal-900",
    dot: "bg-teal-500",
    text: "text-teal-700",
  },
  Clean: {
    badge: "border-emerald-300 bg-emerald-50 text-emerald-900",
    dot: "bg-emerald-500",
    text: "text-emerald-700",
  },
  Pickup: {
    badge: "border-indigo-300 bg-indigo-50 text-indigo-900",
    dot: "bg-indigo-500",
    text: "text-indigo-700",
  },
  Dirty: {
    badge: "border-amber-300 bg-amber-50 text-amber-900",
    dot: "bg-amber-500",
    text: "text-amber-700",
  },
  "Out of Service": {
    badge: "border-orange-300 bg-orange-50 text-orange-900",
    dot: "bg-orange-500",
    text: "text-orange-700",
  },
  "Out of Order": {
    badge: "border-rose-300 bg-rose-50 text-rose-900",
    dot: "bg-rose-500",
    text: "text-rose-700",
  },
}

const FALLBACK_TONE: HkToneStyle = {
  badge: "border-zinc-200 bg-zinc-50 text-zinc-700",
  dot: "bg-zinc-400",
  text: "text-zinc-500",
}

export interface HkToggleOption {
  status: "Inspected" | "Ready" | "Clean" | "Dirty" | "Out of Order"
  supervisorOnly: boolean
  needsReason?: boolean
}

export const HK_TOGGLE_OPTIONS: readonly HkToggleOption[] = [
  { status: "Inspected", supervisorOnly: true },
  { status: "Ready", supervisorOnly: true },
  { status: "Clean", supervisorOnly: false },
  { status: "Dirty", supervisorOnly: false },
  { status: "Out of Order", supervisorOnly: true, needsReason: true },
] as const

export function hkTone(status?: string | null): HkToneStyle {
  return (status && HK_TONES[status]) || FALLBACK_TONE
}

export function hkWeight(status?: string | null): number {
  return HK_READINESS_WEIGHT[(status ?? "").trim()] ?? 0
}

export function isAssignableHkStatus(status?: string | null): boolean {
  const s = (status ?? "").trim()
  return s !== "Out of Order" && s !== "Out of Service" && s !== "OO" && s !== "OS"
}
