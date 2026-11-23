/** Rate calendar: what every room type costs, date by date, and what moved
 *  it. BAR (the base price) is the starting point; seasons and busy-house
 *  rules move it. This screen explains the rate; the quote is what charges. */
import { useCallback, useEffect, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { ChevronLeft, ChevronRight } from "lucide-react"
import { call, getCurrentProperty, type ContractTariff } from "../lib/api"
import { listResource, serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { InfoTip } from "../components/InfoTip"
import { cur, moneyLocale } from "../lib/money"
import { useT, t as translate, uiDateLocale } from "../lib/i18n"
import { cn } from "../lib/utils"
import type { NightRate } from "../components/RateOptions"
import { currencyText, fxLockNote, roundingRule, TariffNight } from "../components/ContractTariff"


const n0 = (n: number) => Number(n ?? 0).toLocaleString(moneyLocale(), { maximumFractionDigits: 0 })

interface RatePlanPolicy {
  name: string
  label: string
  code: string | null
  currency: string | null
  fx_lock_policy: string
  rounding_step: number
  rounding_mode: string
  base_rate_plan: string | null
}

/** A night under a rate plan: a contract night (the agreed rate a booking locks), a card-priced one, or refused. */
interface CalendarNight extends Omit<NightRate, "rate"> {
  rate: number | null
  source?: "contract" | "card"
  contract?: ContractTariff | null
  available?: boolean
  refusal?: string
  card_rate?: number
}

interface Calendar {
  from: string
  days: number
  tax_inclusive: boolean
  room_types: { room_type: string; code: string; name: string; base_price: number; nights: CalendarNight[] }[]
  rate_plans?: RatePlanPolicy[]
  default_plan?: string | null
  property_currency?: string
  rate_plan?: RatePlanPolicy | null
  company?: string | null
}

const shift = (iso: string, days: number) => {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

export default function RateCalendar() {
  const { t } = useT()
  const [from, setFrom] = useState<string | undefined>(undefined)
  const [days, setDays] = useState(14)
  const [data, setData] = useState<Calendar | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [pick, setPick] = useState<{ rt: string; i: number } | null>(null)
  const [params, setParams] = useSearchParams()
  const ratePlan = params.get("rate_plan") ?? ""
  const company = params.get("company") ?? ""
  // customers are offered only to a user native DocPerm lets list Corporate Accounts
  const [customers, setCustomers] = useState<{ name: string; label: string }[] | null>(null)
  useEffect(() => {
    listResource("Corporate Account", { fields: ["name", "company_name"], filters: [["disabled", "=", 0]],
      orderBy: "company_name asc", limit: 500 })
      .then((rows) => setCustomers(rows.map((row) => ({ name: row.name, label: String(row.company_name ?? row.name) }))))
      .catch(() => setCustomers(null))
  }, [])

  const load = useCallback(() => {
    call<Calendar>("kamra.rates.rate_calendar", { property: getCurrentProperty(), from_date: from, days,
      ...(ratePlan ? { rate_plan: ratePlan } : {}), ...(company ? { company } : {}) })
      .then((d) => {
        setData(d)
        setErr(null)
      })
      .catch((e) => setErr(serverError(e)))
  }, [from, days, ratePlan, company])
  useEffect(load, [load])
  const choose = (key: "rate_plan" | "company", value: string) => {
    setPick(null)
    setParams((p) => {
      if (value) p.set(key, value)
      else p.delete(key)
      return p
    }, { replace: true })
  }

  const plan = data?.rate_plan ?? null
  const locale = moneyLocale()
  const contractCurrency = plan?.currency || data?.room_types.flatMap((r) => r.nights)
    .find((x) => x.contract)?.contract?.currency

  const picked = data && pick ? data.room_types.find((r) => r.room_type === pick.rt)?.nights[pick.i] : null
  const pickedType = data && pick ? data.room_types.find((r) => r.room_type === pick.rt) : null

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-zinc-900">
            {t("Rate calendar")}
            <InfoTip doc="revenue">
              {t("BAR (best available rate) is the base price of a room type. A season changes it for a date range, and a busy-house rule raises it when the house fills. This table shows the result per night. Rate plans and vouchers apply on top, per booking.")}
            </InfoTip>
          </h1>
          <p className="text-sm text-zinc-500">
            {t("What each room costs per night, and what moved it.")}{" "}
            {data && (data.tax_inclusive ? t("Prices include tax.") : t("Prices are before tax."))}
          </p>
          {plan && (
            <p data-rate-plan-policy className="mt-1 text-sm text-zinc-600">
              {t("Exchange rate lock: {policy}", { policy: t(plan.fx_lock_policy) })}
              {" · "}{roundingRule(plan.rounding_step, plan.rounding_mode, locale)}
              <span className="block text-xs text-zinc-500">{fxLockNote(plan.fx_lock_policy)}</span>
              {contractCurrency && contractCurrency !== data?.property_currency && (
                <span className="block text-xs text-zinc-500">
                  {t("{currency} contract tariff · local price at the published rate, rounded as the rate plan rounds", { currency: contractCurrency })}
                </span>
              )}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select
            className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm"
            value={ratePlan}
            onChange={(e) => choose("rate_plan", e.target.value)}
            aria-label={t("Rate plan")}
          >
            <option value="">{t("Card rate (no rate plan)")}</option>
            {(data?.rate_plans ?? []).map((p) => <option key={p.name} value={p.name}>{p.label || p.name}</option>)}
          </select>
          {customers && (
            <select
              className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm"
              value={company}
              onChange={(e) => choose("company", e.target.value)}
              aria-label={t("Customer")}
            >
              <option value="">{t("Public rate (no customer)")}</option>
              {customers.map((c) => <option key={c.name} value={c.name}>{c.label}</option>)}
            </select>
          )}
          <Button variant="outline" aria-label={t("Earlier")} onClick={() => setFrom(shift(data?.from ?? "", -days))} disabled={!data}>
            <ChevronLeft className="size-4" aria-hidden />
          </Button>
          <span className="text-sm tabular-nums text-zinc-600">{data?.from}</span>
          <Button variant="outline" aria-label={t("Later")} onClick={() => setFrom(shift(data?.from ?? "", days))} disabled={!data}>
            <ChevronRight className="size-4" aria-hidden />
          </Button>
          <select
            className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            aria-label={t("Days shown")}
          >
            {[7, 14, 21, 31].map((d) => (
              <option key={d} value={d}>{t("{n} days", { n: d })}</option>
            ))}
          </select>
        </div>
      </div>

      {err && <p className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">{translate(err)}</p>}

      {data && (
        <Card>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full min-w-max text-sm">
              <thead>
                <tr className="border-b border-zinc-200 text-xs text-zinc-500">
                  <th className="sticky start-0 bg-white px-3 py-2 text-start font-medium">{t("Room type")}</th>
                  {!plan && <th className="px-2 py-2 text-end font-medium">{t("BAR")}</th>}
                  {data.room_types[0]?.nights.map((x) => {
                    const d = new Date(`${x.date}T00:00:00`)
                    const wk = d.getDay() === 5 || d.getDay() === 6
                    return (
                      <th key={x.date} className={cn("px-2 py-2 text-center font-medium", wk && "text-zinc-800")}>
                        <div>{d.toLocaleDateString(uiDateLocale(undefined), { weekday: "short" })}</div>
                        <div className="tabular-nums">{x.date.slice(5)}</div>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {data.room_types.map((r) => (
                  <tr key={r.room_type} className="border-b border-zinc-100">
                    <td className="sticky start-0 bg-white px-3 py-2 font-medium">{r.name}</td>
                    {!plan && (
                      <td className="px-2 py-2 text-end tabular-nums text-zinc-500">
                        {cur()}{n0(r.base_price)}
                      </td>
                    )}
                    {r.nights.map((x, i) => {
                      const on = pick?.rt === r.room_type && pick.i === i
                      if (x.available === false || x.rate == null) {
                        return (
                          <td key={x.date} className="p-0.5 text-center">
                            <button
                              type="button"
                              onClick={() => setPick(on ? null : { rt: r.room_type, i })}
                              aria-pressed={on}
                              aria-label={t("Not available: {reason}", { reason: x.refusal ?? "" })}
                              title={x.refusal}
                              className={cn("w-full min-w-16 rounded-md px-2 py-1.5 text-zinc-400 hover:bg-zinc-50",
                                on && "ring-2 ring-brand-600")}
                            >—</button>
                          </td>
                        )
                      }
                      const up = !plan && x.rate > r.base_price
                      const down = !plan && x.rate < r.base_price
                      return (
                        <td key={x.date} className="p-0.5 text-center">
                          <button
                            type="button"
                            onClick={() => setPick(on ? null : { rt: r.room_type, i })}
                            aria-pressed={on}
                            className={cn(
                              "w-full min-w-16 rounded-md px-2 py-1.5 tabular-nums",
                              up && "bg-amber-50 text-amber-900",
                              down && "bg-sky-50 text-sky-900",
                              !up && !down && "text-zinc-700 hover:bg-zinc-50",
                              on && "ring-2 ring-brand-600",
                            )}
                          >
                            {x.contract && (
                              <span className="block text-xs text-zinc-500">{currencyText(x.contract.currency, x.contract.nightly, locale)}</span>
                            )}
                            {n0(x.rate)}
                            {(x.season || x.demand) && (
                              <span className="ms-1 inline-block size-1.5 rounded-full bg-current align-middle opacity-60" aria-hidden />
                            )}
                            {x.source === "card" && (
                              <span className="block text-[10px] uppercase tracking-wide text-zinc-400"
                                title={t("No contract tariff for this room type: priced from the card rate.")}>{t("Card rate")}</span>
                            )}
                          </button>
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {!plan && (
        <div className="flex flex-wrap gap-4 text-xs text-zinc-500">
          <span className="inline-flex items-center gap-1.5"><span className="size-3 rounded bg-amber-50 ring-1 ring-amber-200" aria-hidden />{t("Above BAR")}</span>
          <span className="inline-flex items-center gap-1.5"><span className="size-3 rounded bg-sky-50 ring-1 ring-sky-200" aria-hidden />{t("Below BAR")}</span>
          <span>{t("A dot means a season or busy-house rule moved it. Select a night to see which.")}</span>
        </div>
      )}

      {picked && pickedType && (picked.available === false || picked.rate == null || picked.contract) && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {pickedType.name} · {picked.date}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            {picked.contract && picked.rate != null ? (
              <p><TariffNight tariff={picked.contract} agreed_rate={picked.rate} /></p>
            ) : (
              <p className="text-rose-700">{t("Not available: {reason}", { reason: picked.refusal ?? "" })}</p>
            )}
          </CardContent>
        </Card>
      )}

      {picked && pickedType && picked.rate != null && !picked.contract && picked.available !== false && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {pickedType.name} · {picked.date}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            <p>{t("BAR")}: <span className="font-medium tabular-nums">{cur()}{n0(picked.bar)}</span></p>
            {picked.season ? (
              <p>
                {t("Season")}: <span className="font-medium">{picked.season.label}</span> ({picked.season.change}) {"->"}{" "}
                <span className="tabular-nums">{cur()}{n0(picked.after_season)}</span>
              </p>
            ) : (
              <p className="text-zinc-400">{t("No season covers this date.")}</p>
            )}
            {picked.demand ? (
              <p>
                {t("Busy house")}: {Math.round(picked.occupancy)}% {t("full")}, +{picked.demand.premium_pct}%
                {picked.demand.min_rate ? `, ${t("never below")} ${cur()}${n0(picked.demand.min_rate)}` : ""}
              </p>
            ) : (
              <p className="text-zinc-400">{t("No busy-house rule applies ({p}% full).", { p: Math.round(picked.occupancy) })}</p>
            )}
            {picked.source === "card" && (
              <p className="text-zinc-500">{t("No contract tariff for this room type: priced from the card rate.")}</p>
            )}
            <p className="font-semibold">{t("Rate")}: <span className="tabular-nums">{cur()}{n0(picked.rate)}</span></p>
          </CardContent>
        </Card>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">{t("Where to change it")}</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-2 text-sm sm:grid-cols-2">
          {[
            ["/room-types", "BAR (base price)", "Set each room type's base rate, extra adult and child prices."],
            ["/seasons", "Seasons", "Raise or lower rates for dates, weekends or festivals."],
            ["/settings/revenue", "Busy-house rules", "Raise rates automatically as the house fills (hurdle tiers)."],
            ["/rate-plans", "Rate plans", "Corporate, package or member deals applied to a whole stay."],
            ["/tariffs", "Contract Tariffs", "Price each rate plan per room type and occupancy, in the contract currency."],
            ["/vouchers", "Vouchers", "Discount codes with dates, minimum nights and use limits."],
            ["/long-stay", "Long-stay Discounts", "Take a percentage off stays of N nights or more."],
            ["/guardrails", "Guardrails", "Floors and ceilings no rate can cross."],
          ].map(([to, label, blurb]) => (
            <Link key={to} to={to} className="rounded-lg border border-zinc-200 px-3 py-2 hover:border-brand-600">
              <span className="font-medium text-brand-700">{t(label)} →</span>
              <span className="block text-xs text-zinc-500">{t(blurb)}</span>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
