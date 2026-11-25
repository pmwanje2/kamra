/** Shared housekeeping status display tones, readiness priority weights,
 *  and toggle transitions so Tape Chart, Queue Dock, and P35 (Pickup / OS)
 *  stay synchronized from one source. */

import { SERVICE_ROLE } from "./brand"

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
    badge: "border-info-line-strong bg-info-subtle text-info-ink",
    dot: "bg-sky-500",
    text: "text-info-ink",
  },
  Ready: {
    badge: "border-teal-line-strong bg-teal-subtle text-teal-ink",
    dot: "bg-teal-500",
    text: "text-teal-ink",
  },
  Clean: {
    badge: "border-success-line-strong bg-success-subtle text-success-ink",
    dot: "bg-emerald-500",
    text: "text-success-ink",
  },
  Pickup: {
    badge: "border-indigo-line-strong bg-indigo-subtle text-indigo-ink",
    dot: "bg-indigo-500",
    text: "text-indigo-ink",
  },
  Dirty: {
    badge: "border-warning-line-strong bg-warning-subtle text-warning-ink",
    dot: "bg-amber-500",
    text: "text-warning-ink",
  },
  "Out of Service": {
    badge: "border-notice-line-strong bg-notice-subtle text-notice-ink",
    dot: "bg-orange-500",
    text: "text-notice-ink",
  },
  "Out of Order": {
    badge: "border-danger-line-strong bg-danger-subtle text-danger-ink",
    dot: "bg-rose-500",
    text: "text-danger-ink",
  },
}

const FALLBACK_TONE: HkToneStyle = {
  badge: "border-line bg-canvas text-secondary-ink",
  dot: "bg-fill-prominent",
  text: "text-muted-ink",
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

export const HK_SUPERVISOR_ROLES: readonly string[] = [
  "Housekeeping Supervisor",
  "Front Desk",
  "Hotel Admin",
  "System Manager",
  "Administrator",
  SERVICE_ROLE,
]

export function isHkSupervisor(roles: readonly string[]): boolean {
  return HK_SUPERVISOR_ROLES.some((role) => roles.includes(role))
}

export function visibleHkToggleOptions(roles: readonly string[]): readonly HkToggleOption[] {
  const supervisor = isHkSupervisor(roles)
  return HK_TOGGLE_OPTIONS.filter((opt) => !opt.supervisorOnly || supervisor)
}

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
