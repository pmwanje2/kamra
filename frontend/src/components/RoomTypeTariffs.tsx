import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { useLocale } from "../lib/money"
import { currencyText, formatFxRate, tariffDate } from "./ContractTariff"
import RoomTypeMedia from "./RoomTypeMedia"

export default function RoomTypeExtras(props: Parameters<typeof RoomTypeMedia>[0]) {
  return <>
    <RoomTypeTariffs key={props.row.name} roomType={String(props.row.name)} />
    <RoomTypeMedia {...props} />
  </>
}

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
  }[]
  all_active_plans_tariff?: boolean
}

function RoomTypeTariffs({ roomType }: { roomType: string }) {
  const { t } = useT()
  const { currency, locale } = useLocale()
  const [data, setData] = useState<TariffList | null>(null)
  const [error, setError] = useState(false)
  useEffect(() => {
    let live = true
    setData(null)
    setError(false)
    call<TariffList | null>("kamra.rates.room_type_tariffs", { room_type: roomType })
      .then(result => { if (live) setData(result) })
      .catch(() => { if (live) setError(true) })
    return () => { live = false }
  }, [roomType])

  if (!data && !error) return null
  return <section aria-label={t("Contract tariffs")} className="space-y-3 rounded-xl border border-zinc-200 p-4">
    <h3 className="text-sm font-semibold">{t("Contract tariffs")}</h3>
    {error && <p className="text-sm text-rose-700">{t("Could not load contract tariffs.")}</p>}
    {data?.all_active_plans_tariff && <p className="text-sm text-amber-800">
      {t("The card rate is not used by any active rate plan.")}
    </p>}
    {data && !data.tariffs.length && <p className="text-sm text-zinc-600">{t("No approved contract tariffs.")}</p>}
    {!!data?.tariffs.length && <div className="overflow-x-auto">
      <table className="w-full text-left text-sm tabular-nums">
        <caption className="caption-bottom pt-2 text-start text-xs text-zinc-600">
          {[...new Set(data.tariffs.flatMap(row => currency && row.fx_rate && row.fx_date ? [
            t("Published selling rate: 1 {foreign} = {rate} {home} · {date}", {
              foreign: row.currency, rate: formatFxRate(row.fx_rate, locale), home: currency, date: tariffDate(row.fx_date, locale),
            }),
          ] : []))].map(ref => <div key={ref}>{ref}</div>)}
        </caption>
        <thead><tr>{["Rate plan", "Single", "Double", "Extra bed", "Status", "Local equiv."].map(h =>
          <th scope="col" className="whitespace-nowrap px-2 py-2" key={h}>{t(h)}</th>)}</tr></thead>
        <tbody>{data.tariffs.map((row, i) => <tr className="border-t border-zinc-200" key={i}>
          <th scope="row" className="px-2 py-2">{row.label || row.rate_plan}
            <small className="block font-normal text-zinc-500">{t("Valid from")} {tariffDate(row.valid_from, locale)}</small>
          </th>
          {[row.single, row.room, row.extra].map((amount, idx) => <td className="whitespace-nowrap px-2" key={idx}>
            {amount == null ? "—" : currencyText(row.currency, amount, locale)}
          </td>)}
          <td className="px-2"><span className="rounded-full bg-emerald-50 px-2 py-1 text-xs text-emerald-700">{t(row.approval_state)}</span></td>
          <td className="whitespace-nowrap px-2">{currency && row.fx_rate ? currencyText(currency, row.room * row.fx_rate, locale) : "—"}</td>
        </tr>)}</tbody>
      </table>
    </div>}
  </section>
}
