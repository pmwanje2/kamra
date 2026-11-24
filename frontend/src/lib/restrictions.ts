/** Shared types and pure helpers for OPERA Restrictions Matrix (P12) & Channel Health (P43). */
import { moneyDigits } from "./money"

export interface NightRestriction {
  is_restricted?: boolean
  stop_sell?: boolean
  min_los?: number
  max_los?: number
  is_stay_through?: boolean
  cta?: boolean
  ctd?: boolean
  restriction_scope?: string | null
  restriction_reason?: string
}

export interface GridSelection {
  roomType: string
  roomTypeName: string
  startDate: string
  endDate: string
  dates: string[]
}

export interface RateUpdateInput {
  room_type: string
  start_date: string
  end_date: string
  days_of_week?: string[]
  rate: number
}

export interface RestrictionUpdateInput {
  room_type?: string | null
  rate_plan?: string | null
  start_date: string
  end_date: string
  days_of_week?: string[]
  stop_sell?: number
  min_los?: number
  max_los?: number
  is_stay_through?: number
  cta?: number
  ctd?: number
}

export interface BulkGridResult {
  ok: boolean
  property: string
  rates_updated: number
  restrictions_updated: number
  restrictions_deleted: number
  ari_queued: string[]
}

export interface OutboxCounts {
  pending: number
  sending: number
  sent: number
  failed: number
}

export interface ChannelConnectionHealth {
  name: string
  provider: string
  active: number
  external_property_id: string
  uses_outbox: boolean
  sync_days: number
  ari_calls_per_minute: number
  ari_batch_seconds: number
  last_push: string
  last_push_status: string
  last_full_sync: string
  health_status: "Online" | "Stale" | "Paused" | "Error" | "Inactive"
  paused: Record<string, number>
  outbox_counts: OutboxCounts
  mappings_count: number
}

export interface OutboxItem {
  name: string
  connection: string
  provider: string
  date_from: string
  date_to: string
  kinds: string
  full: number
  status: "Pending" | "Sending" | "Sent" | "Failed"
  attempts: number
  queued_at: string
  next_attempt_at: string | null
  sent_at: string | null
  reason: string
  detail: string
}

export interface SyncedRestrictions {
  stop_sell: boolean
  min_stay_arrival: number | null
  max_stay: number | null
  closed_to_arrival: boolean
  closed_to_departure: boolean
  active_days: number
}

export interface ChannelParityDay {
  date: string
  direct_rate: number | null
  ota_rate: number | null
  delta: number | null
  status: "OK" | "Undercut" | "Overpriced" | "Unmapped" | "Stale"
  stop_sell: boolean
  min_los: number
  max_los: number
  cta: boolean
  ctd: boolean
}

export interface ChannelParityRow {
  mapping_name: string
  connection: string
  provider: string
  room_type: string
  room_type_name: string
  rate_plan: string | null
  rate_plan_label: string
  external_room_id: string
  external_rate_id: string
  external_currency: string
  currency: string
  direct_rate: number | null
  ota_rate: number | null
  parity_delta: number | null
  parity_status: "OK" | "Undercut" | "Overpriced" | "Unmapped" | "Stale"
  last_synced_at: string
  last_error: string
  restrictions_synced: SyncedRestrictions
  days: ChannelParityDay[]
}

export interface ChannelHealthSnapshot {
  property: string
  property_currency: string
  checked_at: string
  can_manage: boolean
  connections: ChannelConnectionHealth[]
  outbox: OutboxItem[]
  parity: ChannelParityRow[]
  summary: {
    connections_total: number
    connections_active: number
    outbox_pending: number
    outbox_failed: number
    mappings_total: number
    parity_ok: number
    parity_issues: number
    unmapped_room_types: { name: string; room_type_name: string; room_count: number }[]
  }
}

export const WEEKDAYS = [
  { code: "mon", short: "M", label: "Monday" },
  { code: "tue", short: "T", label: "Tuesday" },
  { code: "wed", short: "W", label: "Wednesday" },
  { code: "thu", short: "T", label: "Thursday" },
  { code: "fri", short: "F", label: "Friday" },
  { code: "sat", short: "S", label: "Saturday" },
  { code: "sun", short: "S", label: "Sunday" },
] as const

const EDITOR_ROLES = new Set(["Revenue Manager", "Hotel Admin", "System Manager", "Administrator"])

export function canEditRates(roles: string[]): boolean {
  return roles.some((r) => EDITOR_ROLES.has(r))
}

export function normalizeDateRange(a: string, b: string, orderedDates: string[]): string[] {
  const ia = orderedDates.indexOf(a)
  const ib = orderedDates.indexOf(b)
  if (ia < 0 || ib < 0) return a <= b ? [a] : [b]
  const lo = Math.min(ia, ib)
  const hi = Math.max(ia, ib)
  return orderedDates.slice(lo, hi + 1)
}

export function rateStepForCurrency(currency?: string | null): string {
  const digits = moneyDigits(currency || undefined)
  if (digits <= 0) return "1"
  return `0.${"0".repeat(digits - 1)}1`
}

export function restrictionBadges(cell: Partial<NightRestriction>): {
  kind: "stop_sell" | "cta" | "ctd" | "min_los" | "max_los"
  label: string
}[] {
  const out: { kind: "stop_sell" | "cta" | "ctd" | "min_los" | "max_los"; label: string }[] = []
  if (cell.stop_sell) out.push({ kind: "stop_sell", label: "SS" })
  if (cell.cta) out.push({ kind: "cta", label: "CTA" })
  if (cell.ctd) out.push({ kind: "ctd", label: "CTD" })
  if (cell.min_los && cell.min_los > 0) out.push({ kind: "min_los", label: `${cell.min_los}+` })
  if (cell.max_los && cell.max_los > 0) out.push({ kind: "max_los", label: `≤${cell.max_los}` })
  return out
}

export function parityTone(status: string): "green" | "rose" | "amber" | "zinc" {
  if (status === "OK") return "green"
  if (status === "Undercut") return "rose"
  if (status === "Overpriced" || status === "Stale") return "amber"
  return "zinc"
}

export function healthTone(status: string): "green" | "rose" | "amber" | "zinc" {
  if (status === "Online") return "green"
  if (status === "Error") return "rose"
  if (status === "Paused" || status === "Stale") return "amber"
  return "zinc"
}
