import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { useLocale } from "../lib/money"
import type { Row } from "../lib/resource"
import { onSettingsChanged, onTariffsChanged } from "../lib/tariffSync"
import { currencyText, formatFxRate, roundingRule, tariffDate, TariffNight } from "./ContractTariff"
import { ManageTariffsLink } from "./ManageTariffsLink"

interface TariffList {
  tariffs: {
    rate_plan: string
    label?: string
    currency: string
    room: number
    single?: number | null
    extra?: number | null
    approval_state: string
    valid_from: string
    fx_rate?: number | null
    fx_date?: string | null
    /** the server's local amount: converted and rounded as the rate plan rounds */
    local_room?: number | null
    local_single?: number | null
    fx_lock_policy?: string
    rounding_step?: number
    rounding_mode?: string
    derived_from?: string | null
    derived_from_label?: string | null
  }[]
  all_active_plans_tariff?: boolean | number
  rate_plan_required?: boolean
  property_currency?: string
  /** the default active plan's tariff, else the only plan's */
  primary_rate_plan?: string | null
  /** active plans priced by a tariff (base or derived) */
  tariff_plans?: { name: string; label?: string | null }[]
  /** active plans no tariff prices, on themselves or a base plan */
  card_rate_plans?: { name: string; label?: string | null }[]
  /** active tariff-backed plans that have no approved tariff for this room type */
  unpriced_plans?: { name: string; label?: string | null }[]
}

/** A room type's contract tariffs: the price its rate plans sell at (OPERA Rate Details, SAP PR00). */
export default function RoomTypeTariffs({ row }: { row: Row; reload?: () => void }) {
  const roomType = String(row.name)
  const { t } = useT()
  const { currency, locale } = useLocale()
  const [data, setData] = useState<TariffList | null>(null)
  const [error, setError] = useState(false)
  const [rev, setRev] = useState(0)
  useEffect(() => {
    const bump = () => setRev((v) => v + 1)
    const offTariffs = onTariffsChanged(bump)
    const offSettings = onSettingsChanged(bump)
    return () => { offTariffs(); offSettings() }
  }, [])
  useEffect(() => {
    let live = true
    setData(null)
    setError(false)
    call<TariffList | null>("kamra.rates.room_type_tariffs", { room_type: roomType })
      .then(result => { if (live) setData(result) })
      .catch(() => { if (live) setError(true) })
    return () => { live = false }
  }, [roomType, rev])

  const home = data?.property_currency || currency
  const primary = data?.tariffs.find(tariff => tariff.rate_plan === data.primary_rate_plan)
  return <section aria-label={t("Contract tariffs")} className="space-y-3 rounded-xl border border-zinc-200 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <h3 className="text-sm font-semibold">{t("Contract tariffs")}</h3>
      <ManageTariffsLink roomType={roomType} />
    </div>
    {error && <p className="text-sm text-rose-700">{t("Could not load contract tariffs.")}</p>}
    {primary && <div data-primary-tariff className="space-y-0.5 rounded-lg bg-zinc-50 p-3 text-sm">
      <p className="font-medium">{primary.label || primary.rate_plan}</p>
      {primary.local_room != null && primary.fx_rate && primary.fx_date && home
        ? <TariffNight tariff={{ currency: primary.currency, nightly: primary.room, fx_rate: primary.fx_rate,
            fx_date: primary.fx_date, property_currency: home }} agreed_rate={primary.local_room} />
        : <span className="tabular-nums">{currencyText(primary.currency, primary.room, locale)}</span>}
      <p className="text-xs text-zinc-600">
        {t("Exchange rate lock: {policy}", { policy: t(primary.fx_lock_policy || "Fixed at Reservation") })}
        {" · "}{roundingRule(primary.rounding_step, primary.rounding_mode, locale)}
      </p>
    </div>}
    {!!data?.all_active_plans_tariff && <p className="text-sm text-amber-800">
      {t("The card rate is not used by any active rate plan.")}
    </p>}
    {data && !data.tariffs.length && <p className="text-sm text-zinc-600">{t("No approved contract tariffs.")}</p>}
    {!!data?.tariffs.length && <div className="overflow-x-auto">
      <table className="w-full text-left text-sm tabular-nums">
        <caption className="caption-bottom pt-2 text-start text-xs text-zinc-600">
          {[...new Set(data.tariffs.flatMap(row => home && row.fx_rate && row.fx_date ? [
            t("Published selling rate: 1 {foreign} = {rate} {home} · {date}", {
              foreign: row.currency, rate: formatFxRate(row.fx_rate, locale), home, date: tariffDate(row.fx_date, locale),
            }),
          ] : []))].map(ref => <div key={ref}>{ref}</div>)}
        </caption>
        <thead><tr>{["Rate plan", "Single", "Double", "Extra bed", "Status", "Exchange rate lock", "Local equiv."].map(h =>
          <th scope="col" className="whitespace-nowrap px-2 py-2" key={h}>{t(h)}</th>)}</tr></thead>
        <tbody>{data.tariffs.map((row, i) => <tr className="border-t border-zinc-200" key={i}>
          <th scope="row" className="px-2 py-2">{row.label || row.rate_plan}
            {row.derived_from && (
              <small className="block font-normal text-zinc-500">
                {t("Derived from {base}", { base: row.derived_from_label || row.derived_from })}
              </small>
            )}
            <small className="block font-normal text-zinc-500">{t("Valid from")} {tariffDate(row.valid_from, locale)}</small>
            <Link to={{ pathname: "/rate-calendar", search: `?${new URLSearchParams({ rate_plan: row.rate_plan })}` }}
              className="text-xs font-normal text-brand-700 hover:underline">{t("Rate calendar")}</Link>
          </th>
          {[row.single, row.room, row.extra].map((amount, idx) => <td className="whitespace-nowrap px-2" key={idx}>
            {amount == null ? "—" : currencyText(row.currency, amount, locale)}
          </td>)}
          <td className="px-2"><span className="rounded-full bg-emerald-50 px-2 py-1 text-xs text-emerald-700">{t(row.approval_state)}</span></td>
          <td className="whitespace-nowrap px-2">{t(row.fx_lock_policy || "Fixed at Reservation")}</td>
          <td className="whitespace-nowrap px-2">{home && row.local_room != null ? currencyText(home, row.local_room, locale) : "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>}
    {!!data?.unpriced_plans?.length && <p className="text-sm text-amber-800">
      {t("No tariff set for: {plans}", { plans: data.unpriced_plans.map(plan => plan.label || plan.name).join(", ") })}
    </p>}
    {data && (!data.tariffs.length || !!data.unpriced_plans?.length) && <p className="text-sm">
      <Link to="/tariffs" className="font-medium text-brand-700 hover:underline">
        {t("Set its tariff in Contract Tariffs")}
      </Link>
    </p>}
    {!!data?.card_rate_plans?.length && <p className="text-sm text-zinc-600">
      {t("Priced from the card rate: {plans}", { plans: data.card_rate_plans.map(plan => plan.label || plan.name).join(", ") })}
    </p>}
    {data && (
      <p className="text-xs text-zinc-500">
        {data.rate_plan_required
          ? t("Rate plans set the price for all bookings at this property.")
          : t("Stays booked without a rate plan still use the card rate.")}
      </p>
    )}
  </section>
}
