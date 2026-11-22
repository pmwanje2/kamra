import { useEffect, useState } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { useLocale } from "../lib/money"
import { currencyText, tariffDate } from "./ContractTariff"
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
    {data?.tariffs.map((tariff, i) => <div key={`${tariff.rate_plan}-${tariff.valid_from}-${i}`} className="space-y-1 rounded-lg bg-zinc-50 p-3 text-sm">
      <p className="font-medium">{tariff.label || tariff.rate_plan}</p>
      <p className="text-zinc-600">{t("Room: {amount}", { amount: currencyText(tariff.currency, tariff.room, locale) })}</p>
      {tariff.single != null && <p className="text-zinc-600">{t("Single: {amount}", { amount: currencyText(tariff.currency, tariff.single, locale) })}</p>}
      {tariff.extra != null && <p className="text-zinc-600">{t("Extra: {amount}", { amount: currencyText(tariff.currency, tariff.extra, locale) })}</p>}
      <p className="text-zinc-600">{t("{state} · Valid from {date}", { state: t(tariff.approval_state), date: tariffDate(tariff.valid_from, locale) })}</p>
      {currency && tariff.fx_rate != null && tariff.fx_rate > 0 && tariff.fx_date && <p className="text-zinc-600">
        {t("{amount} at the selling rate on {date}", {
          amount: currencyText(currency, tariff.room * tariff.fx_rate, locale),
          date: tariffDate(tariff.fx_date, locale),
        })}
      </p>}
    </div>)}
  </section>
}
