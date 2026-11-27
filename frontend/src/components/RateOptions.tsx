import { useEffect, useRef, useState } from "react"
import { call, getCurrentProperty, type TariffReference } from "../lib/api"
import { onSettingsChanged, onTariffsChanged } from "../lib/tariffSync"
import { currencyText, formatFxRate, tariffDate } from "./ContractTariff"
import { InfoTip } from "./InfoTip"
import { fmtMoney, locale, moneyDigits, moneyLocale } from "../lib/money"
import { isOverride, maxPriceDigits, parsePrice, splitRoomTotal } from "../lib/nightPrices"
import { useT, t as translate } from "../lib/i18n"
import { cn } from "../lib/utils"


const homeText = (n: number) => currencyText(locale().currency, n, moneyLocale())

export interface NightRate {
  date: string
  bar: number
  after_season: number
  rate: number
  season: { label: string; change: string } | null
  demand: { premium_pct: number; min_rate: number; from_occupancy: number } | null
  occupancy: number
}

interface PlanRow extends TariffReference {
  is_default?: boolean
  is_negotiated?: boolean
  rate_category?: string | null
  name: string
  label: string
  code: string | null
  room_total: number
  vs_plain: number
  available?: boolean
  refusal?: string
  /** OPERA Exchange Type: when a foreign-currency contract converts to the home currency */
  fx_lock_policy?: string
  /** the base plan's name for a derived plan */
  derived_from?: string | null
}

interface Options {
  nights: NightRate[]
  plain: { room_total: number; amount_after_tax: number }
  plans: PlanRow[]
  voucher: { code: string; valid: boolean; discount?: number; kind?: string; value?: number; reason?: string; voucher_currency?: string; voucher_tx_amount?: number; voucher_fx_rate?: number } | null
  /** the figures above (and a typed night price) include tax */
  tax_inclusive?: boolean
  rate_plan_required?: boolean
  all_active_plans_tariff?: boolean
  /** the booking company's negotiated plan: it prices the stay while no plan is picked */
  negotiated_plan?: string | null
  company_plan?: string | null
}

/** The stay's rate, explained: each night's BAR and what moved it, the whole
 *  stay under each rate plan, and the voucher. Every figure is a real quote;
 *  picking a plan here picks it on the booking. */
export function RateOptions({
  roomType,
  checkIn,
  checkOut,
  adults,
  children,
  extraBeds,
  mealPlan,
  voucher,
  ratePlan,
  onPickPlan,
  nightPrices,
  onNightPrices,
  canSetPrice,
  lockedNote,
  planRequired,
  company, travelAgent,
}: {
  roomType: string
  checkIn: string
  checkOut: string
  adults: number
  children: number
  extraBeds?: number
  mealPlan?: string
  voucher?: string
  ratePlan: string
  onPickPlan: (plan: string) => void
  /** nights the desk priced by hand: date -> price, in the basis shown here */
  nightPrices: Record<string, number>
  onNightPrices: (prices: Record<string, number>) => void
  /** the desk may type a night's price (right to set rates, not an OTA total) */
  canSetPrice: boolean
  /** why the prices cannot be typed, when it is not a missing right */
  lockedNote?: string
  planRequired?: boolean
  /** the booking's company: each plan is priced at its own tariff (OPERA Rate Query with a Company) */
  company?: string
  travelAgent?: string
}) {
  const { t } = useT()
  const [data, setData] = useState<Options | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // what is being typed, before it is a valid price (a night's text, or the room total)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [syncRev, setSyncRev] = useState(0)
  const inputs = useRef<Record<string, HTMLInputElement | null>>({})

  useEffect(() => {
    const bump = () => setSyncRev((v) => v + 1)
    const offTariffs = onTariffsChanged(bump)
    const offSettings = onSettingsChanged(bump)
    return () => { offTariffs(); offSettings() }
  }, [])

  useEffect(() => {
    if (!roomType || !checkIn || !checkOut) return
    let live = true
    const h = setTimeout(() => {
      call<Options>("kamra.rates.rate_options", {
        property: getCurrentProperty(),
        room_type: roomType,
        check_in_date: checkIn,
        check_out_date: checkOut,
        adults,
        children,
        extra_beds: extraBeds || undefined,
        rate_plan: ratePlan || undefined,
        meal_plan: mealPlan || undefined,
        voucher_code: voucher || undefined,
        ...(company ? { company } : {}),
        ...(travelAgent ? { travel_agent: travelAgent } : {}),
      })
        .then((d) => {
          if (live) {
            setData(d)
            setErr(null)
          }
        })
        .catch(() => live && setErr(t("Could not load the rates.")))
    }, 350)
    return () => {
      live = false
      clearTimeout(h)
    }
  }, [roomType, checkIn, checkOut, adults, children, extraBeds, mealPlan, voucher, ratePlan, company, travelAgent, t, syncRev])

  if (err) return <p className="text-xs text-muted-ink">{translate(err)}</p>
  if (!data || !Array.isArray(data.plans) || !Array.isArray(data.nights)) return null
  // no plan picked: the company's negotiated plan prices the stay, as the quote does
  const required = planRequired || data.rate_plan_required || (data.all_active_plans_tariff && data.plans.length > 0)
  const negotiated = data.negotiated_plan || data.company_plan
  const picked = ratePlan || negotiated || (required && data.plans.find(p => p.is_default)?.name) || ""
  const selected = data.plans.find((plan) => plan.name === picked)
  const references = [...new Map(data.plans.flatMap(({ tariff }) => tariff ? [[
    JSON.stringify([tariff.currency, tariff.property_currency, tariff.fx_rate, tariff.fx_date]), tariff,
  ] as const] : [])).values()]
  const moved = data.nights.some((x) => x.season || x.demand)
  const digits = maxPriceDigits(moneyDigits())
  const plain = (n: number) => String(Number(n.toFixed(digits)))
  const priceOf = (x: NightRate) => nightPrices[x.date] ?? x.rate
  const hasTyped = data.nights.some((x) => isOverride(nightPrices[x.date], x.rate, digits))
  const roomTotal = data.nights.reduce((sum, x) => sum + priceOf(x), 0)
  const basis = data.tax_inclusive ? t("Prices include tax.") : t("Prices are before tax.")
  /** How the plan fixes a foreign contract's exchange rate (OPERA Exchange Type) and what it derives from. */
  const planNotes = (p: PlanRow) => [
    p.tariff && p.tariff.currency !== p.tariff.property_currency && p.fx_lock_policy ? t(p.fx_lock_policy) : null,
    p.derived_from ? t("Derived from {base}", { base: p.derived_from }) : null,
  ].filter((note): note is string => !!note)

  /** Set one night; a price equal to what Kamra charges, or none, is not an override. */
  function setNight(x: NightRate, price: number | null) {
    const next = { ...nightPrices }
    if (price === null || !isOverride(price, x.rate, digits)) delete next[x.date]
    else next[x.date] = price
    onNightPrices(next)
  }
  const draft = (key: string, text: string | null) =>
    setDrafts((d) => {
      const { [key]: _gone, ...rest } = d
      return text === null ? rest : { ...rest, [key]: text }
    })
  function typeNight(x: NightRate, text: string) {
    draft(x.date, text)
    if (text.trim() === "") return setNight(x, null)
    const price = parsePrice(text, digits)
    if (price !== null) setNight(x, price)
  }
  function typeTotal(text: string) {
    draft("total", text)
    const total = parsePrice(text, digits)
    if (total === null || !data) return
    const parts = splitRoomTotal(total, data.nights.length, digits)
    const next: Record<string, number> = {}
    data.nights.forEach((x, i) => {
      if (isOverride(parts[i], x.rate, digits)) next[x.date] = parts[i]
    })
    onNightPrices(next)
  }
  function onNightKey(e: React.KeyboardEvent<HTMLInputElement>, i: number, x: NightRate) {
    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      const other = data?.nights[i + (e.key === "ArrowUp" ? -1 : 1)]
      if (other) {
        e.preventDefault()
        inputs.current[other.date]?.focus()
        inputs.current[other.date]?.select()
      }
    } else if (e.key === "Backspace" && e.altKey) {
      e.preventDefault()
      draft(x.date, null)
      setNight(x, null)
    }
  }
  return (
    <div className="space-y-3 rounded-xl border border-line p-4">
      <div className="flex items-center gap-1.5">
        <h3 className="text-sm font-semibold text-ink">{t("Rates for these dates")}</h3>
        {!selected?.tariff && <InfoTip doc="revenue">
          {t("BAR is the best available rate: the room type's base price. A season or a busy-house (demand) rule can move it for a date. A rate plan then adjusts the whole stay, and a voucher comes off the total.")}
        </InfoTip>}
      </div>

      {selected?.tariff || selected?.available === false || (required && !ratePlan) ? null : <>
      {canSetPrice && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <label htmlFor="night-room-total" className="text-secondary-ink">
            {t("Room total")}
          </label>
          <span className="inline-flex items-center gap-1">
            <span className="text-muted-ink">{locale().currency}</span>
            <input
              id="night-room-total"
              inputMode="decimal"
              className={cn(
                "w-28 rounded-md border px-2 py-1 text-right tabular-nums",
                drafts.total !== undefined && parsePrice(drafts.total, digits) === null
                  ? "border-rose-400"
                  : "border-control",
              )}
              value={drafts.total ?? plain(roomTotal)}
              aria-invalid={drafts.total !== undefined && parsePrice(drafts.total, digits) === null}
              onChange={(e) => typeTotal(e.target.value)}
              onBlur={() => draft("total", null)}
            />
          </span>
          <span className="text-xs text-muted-ink">{basis}</span>
        </div>
      )}
      {!canSetPrice && lockedNote && <p className="text-xs text-muted-ink">{lockedNote}</p>}

      <ul className="divide-y divide-line-subtle text-sm">
        {data.nights.map((x, i) => {
          const typed = isOverride(nightPrices[x.date], x.rate, digits)
          const bad = drafts[x.date] !== undefined && drafts[x.date].trim() !== "" && parsePrice(drafts[x.date], digits) === null
          return (
            <li
              key={x.date}
              className={cn(
                "flex flex-wrap items-center gap-x-3 gap-y-1 py-1.5",
                typed && "-mx-2 rounded-md bg-accent-subtle px-2",
              )}
            >
              <span className="w-24 text-muted-ink">{x.date.slice(5)}</span>
              {canSetPrice ? (
                <span className="inline-flex items-center gap-1">
                  <span className="text-muted-ink">{locale().currency}</span>
                  <input
                    ref={(el) => {
                      inputs.current[x.date] = el
                    }}
                    inputMode="decimal"
                    aria-label={t("Price for {d}", { d: x.date })}
                    aria-invalid={bad}
                    className={cn(
                      "w-28 rounded-md border px-2 py-1 text-right font-medium tabular-nums",
                      bad ? "border-rose-400" : typed ? "border-accent-control" : "border-control",
                    )}
                    value={drafts[x.date] ?? plain(priceOf(x))}
                    onChange={(e) => typeNight(x, e.target.value)}
                    onKeyDown={(e) => onNightKey(e, i, x)}
                    onBlur={() => draft(x.date, null)}
                  />
                </span>
              ) : (
                <span className="font-medium tabular-nums">
                  {homeText(priceOf(x))}
                </span>
              )}
              {typed ? (
                <>
                  <span className="text-xs text-muted-ink tabular-nums line-through">
                    {t("BAR {r}", { r: homeText(x.rate) })}
                  </span>
                  <button
                    type="button"
                    className="text-xs text-accent-ink underline"
                    onClick={() => {
                      draft(x.date, null)
                      setNight(x, null)
                    }}
                  >
                    {t("Back to BAR")}
                  </button>
                </>
              ) : (
                x.rate !== x.bar && (
                  <span className="text-xs text-muted-ink tabular-nums">
                    {t("BAR {r}", { r: homeText(x.bar) })}
                  </span>
                )
              )}
              {x.season && (
                <span className="rounded-full bg-warning-subtle px-2 py-0.5 text-xs text-warning-ink">
                  {x.season.label} {x.season.change}
                </span>
              )}
              {x.demand && !typed && (
                <span className="rounded-full bg-danger-subtle px-2 py-0.5 text-xs text-danger-ink">
                  {t("Busy house {p}%", { p: Math.round(x.occupancy) })}
                  {x.demand.premium_pct ? ` +${x.demand.premium_pct}%` : ""}
                </span>
              )}
            </li>
          )
        })}
      </ul>
      {canSetPrice && (
        <p className="text-xs text-muted-ink">
          {t("Type a price to set a night by hand. Up and down arrows move between nights; Alt+Backspace returns a night to BAR.")}
        </p>
      )}
      {hasTyped && (
        <p className="text-xs text-muted-ink">
          {t("Prices set by hand cannot be combined with a rate plan or a voucher.")}
        </p>
      )}
      {!moved && (
        <p className="text-xs text-muted-ink">{t("No season or demand rule applies: every night is at BAR.")}</p>
      )}
      </>}

      {data.plans.length > 0 && (
        <div>
          <p className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-ink">{t("Rate plans")}</p>
          <div className="space-y-1">
            {!required && !negotiated && <button
              type="button"
              onClick={() => onPickPlan("")}
              className={cn(
                "flex w-full items-center justify-between gap-3 rounded-xl border px-3.5 py-2.5 transition-colors text-sm",
                !picked ? "border-accent-control bg-accent-subtle" : "border-line hover:bg-canvas",
              )}
            >
              <span>{t("Standard rate")}</span>
              <span className="tabular-nums">
                {homeText(data.plain.room_total)}
              </span>
            </button>}
            {data.plans.map((p) => (
              <button
                key={p.name}
                type="button"
                onClick={() => onPickPlan(p.name)}
                disabled={hasTyped || p.available === false}
                aria-pressed={picked === p.name}
                className={cn(
                  "flex w-full items-center justify-between gap-3 text-start rounded-xl border px-3.5 py-2.5 transition-colors text-sm disabled:cursor-not-allowed disabled:opacity-50",
                  picked === p.name ? "border-accent-control bg-accent-subtle" : "border-line hover:bg-canvas",
                )}
              >
                <span className="font-semibold text-ink">
                  {p.label}
                  {(p.is_negotiated || p.name === data.company_plan) && <span className="ms-1 rounded bg-accent-subtle px-1.5 text-xs text-accent-ink">{t("Negotiated")}</span>}
                  {p.is_default && <span className="ms-1 rounded bg-subtle px-1.5 text-xs text-secondary-ink">{t("Default")}</span>}
                  {p.rate_category && p.rate_category !== "Standard" && <span className="ms-1 rounded bg-info-subtle px-1.5 text-xs text-info-ink">{t(p.rate_category)}</span>}
                  {p.code ? <span className="ms-1 rounded bg-subtle px-1.5 py-0.5 text-[11px] font-medium uppercase tracking-wide text-secondary-ink">{p.code}</span> : null}
                  {planNotes(p).length > 0 && <span className="mt-0.5 block text-xs font-normal text-muted-ink">
                    {planNotes(p).join(" · ")}
                  </span>}
                </span>
                {p.available === false ? <span className="text-danger-ink">{p.refusal}</span> : p.tariff ? (
                  <span className="text-right font-semibold tabular-nums">
                    {currencyText(p.tariff.currency, p.tariff.nightly, moneyLocale())}{" "}
                    <span className="text-xs font-normal text-muted-ink">/ {t("night")}</span>
                    <span className="block text-xs font-normal text-muted-ink">≈ {currencyText(p.tariff.property_currency, p.agreed_rate ?? p.room_total / Math.max(1, data.nights.length), moneyLocale())}</span>
                  </span>
                ) : <span className="tabular-nums">
                  {homeText(p.room_total)}
                  {p.vs_plain !== 0 && (
                    <span className={cn("ms-2 text-xs", p.vs_plain < 0 ? "text-success-ink" : "text-danger-ink")}>
                      {p.vs_plain > 0 ? "+" : ""}
                      {homeText(p.vs_plain)}
                    </span>
                  )}
                </span>}
              </button>
            ))}
          </div>
          {references.map(r => <p key={JSON.stringify(r)} className="mt-2 text-xs text-secondary-ink tabular-nums">
            {t("Published rate: 1 {foreign} = {rate} {home} · Effective {date}", {
              foreign: r.currency, rate: formatFxRate(r.fx_rate, moneyLocale()), home: r.property_currency, date: tariffDate(r.fx_date, moneyLocale()),
            })}
          </p>)}
        </div>
      )}

      {data.voucher && (
        <p className={cn("text-sm", data.voucher.valid ? "text-success-ink" : "text-danger-ink")}>
          {data.voucher.valid
            ? data.voucher.voucher_currency && data.voucher.voucher_currency !== locale().currency
              ? t("Voucher {code} takes {txAmount} ({homeAmount}) off.", {
                code: data.voucher.code,
                txAmount: fmtMoney(data.voucher.voucher_tx_amount, data.voucher.voucher_currency),
                homeAmount: homeText(data.voucher.discount ?? 0),
              })
              : t("Voucher {code} takes {amt} off.", { code: data.voucher.code, amt: homeText(data.voucher.discount ?? 0) })
            : data.voucher.reason}
        </p>
      )}
    </div>
  )
}
