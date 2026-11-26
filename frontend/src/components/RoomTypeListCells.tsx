import { Badge } from "./ui/badge"
import { currencyText } from "./ContractTariff"
import { t } from "../lib/i18n"
import { moneyLocale } from "../lib/money"

export interface RoomTypeSummaryRow {
  room_type: string
  room_count: number
  priced: boolean
  source: "contract" | "card" | null
  currency: string | null
  amount: number | null
  local_amount: number | null
  refusal: string | null
}

export interface RoomTypesSummary {
  property_currency: string
  has_card_rate_plans: boolean
  rate_plan_required?: boolean
  all_active_plans_tariff?: boolean
  erp_tax_managed?: boolean
  default_plan?: {
    name: string
    label?: string | null
    code?: string | null
    currency?: string | null
    is_tariff?: boolean
  } | null
  room_types: RoomTypeSummaryRow[]
}

export function renderRoomCount(entry?: RoomTypeSummaryRow) {
  if (!entry) return "-"
  if (entry.room_count === 0) return <Badge tone="amber">{t("No rooms")}</Badge>
  return <span className="tabular-nums">{entry.room_count}</span>
}

export function formatSellingRateText(
  entry: RoomTypeSummaryRow | undefined,
  home: string,
) {
  if (!entry || !entry.priced || entry.amount == null) return ""
  const loc = moneyLocale()
  const rateCur = entry.currency || home
  const primary = currencyText(rateCur, entry.amount, loc)
  if (entry.currency && home && entry.currency !== home && entry.local_amount != null) {
    return `${primary} · ${currencyText(home, entry.local_amount, loc)}`
  }
  return primary
}

export function renderSellingRate(
  entry: RoomTypeSummaryRow | undefined,
  home: string,
  planLabel: string,
) {
  if (!entry) return "-"
  if (!entry.priced || entry.amount == null) {
    return <Badge tone="amber">{t("No rate set")}</Badge>
  }
  const amountLine = formatSellingRateText(entry, home)
  return (
    <div className="leading-snug">
      <div className="font-medium tabular-nums text-ink">{amountLine}</div>
      {planLabel && (
        <div className="text-xs text-muted-ink">
          {t("{plan} · 2 adults", { plan: planLabel })}
        </div>
      )}
    </div>
  )
}
