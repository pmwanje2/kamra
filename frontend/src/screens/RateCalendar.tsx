/** Rate calendar: what every room type costs, date by date, what moved it,
 *  and (for Revenue Managers / Hotel Admins) inline Editable BAR + OPERA
 *  Restrictions Matrix (MinLOS, MaxLOS, CTA, CTD, Stop-Sell) with OTA sync. */
import { useEffect, useRef, useState } from "react"
import { Link, useSearchParams } from "react-router-dom"
import { ChevronLeft, ChevronRight, SlidersHorizontal } from "lucide-react"
import { call, getCurrentProperty, type ContractTariff } from "../lib/api"
import { useAuth } from "../lib/auth"
import { listResource, serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { InfoTip } from "../components/InfoTip"
import { cur, moneyDigits, moneyLocale } from "../lib/money"
import { useT, t as translate, uiDateLocale } from "../lib/i18n"
import { cn } from "../lib/utils"
import { onSettingsChanged, onTariffsChanged } from "../lib/tariffSync"
import type { NightRate } from "../components/RateOptions"
import { currencyText, fxLockNote, roundingRule, TariffNight } from "../components/ContractTariff"
import RateRestrictionsDrawer from "../components/RateRestrictionsDrawer"
import {
  canEditBarForCalendar,
  canEditRates,
  normalizeDateRange,
  restrictionBadges,
  type BulkGridResult,
  type GridSelection,
  type NightRestriction,
} from "../lib/restrictions"

interface RatePlanPolicy {
  name: string
  label: string
  code: string | null
  currency: string | null
  fx_lock_policy: string
  rounding_step: number
  rounding_mode: string
  base_rate_plan: string | null
  is_tariff?: boolean
}

/** A night under a rate plan: a contract night (the agreed rate a booking locks), a card-priced one, or refused. */
interface CalendarNight extends Omit<NightRate, "rate">, NightRestriction {
  rate: number | null
  source?: "contract" | "card"
  contract?: ContractTariff | null
  available?: boolean
  refusal?: string
  card_rate?: number
}

interface CalendarRoomType {
  room_type: string
  code: string
  name: string
  base_price: number
  room_count?: number
  sellable?: boolean
  nights: CalendarNight[]
}

interface Calendar {
  from: string
  days: number
  tax_inclusive: boolean
  can_edit?: boolean
  room_types: CalendarRoomType[]
  rate_plans?: RatePlanPolicy[]
  default_plan?: string | null
  rate_plan_required?: boolean
  property_currency?: string
  rate_plan?: RatePlanPolicy | null
  company?: string | null
}

const shift = (iso: string, days: number) => {
  const d = new Date(`${iso}T00:00:00`)
  d.setDate(d.getDate() + days)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
}

function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(() =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia("(max-width: 639px)").matches
      : false,
  )
  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return
    const mq = window.matchMedia("(max-width: 639px)")
    const update = () => setMobile(mq.matches)
    update()
    mq.addEventListener("change", update)
    return () => mq.removeEventListener("change", update)
  }, [])
  return mobile
}

export default function RateCalendar() {
  const { t } = useT()
  const { roles } = useAuth()
  const isMobile = useIsMobile()
  const [from, setFrom] = useState<string | undefined>(undefined)
  const [days, setDays] = useState(14)
  const [data, setData] = useState<Calendar | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [pick, setPick] = useState<{ rt: string; i: number } | null>(null)
  const [syncRev, setSyncRev] = useState(0)

  // Editable BAR + Restrictions state
  const [mode, setMode] = useState<"view" | "edit">("view")
  const [selection, setSelection] = useState<GridSelection | null>(null)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const dragRef = useRef<{
    roomType: string
    roomTypeName: string
    anchorDate: string
    moved: boolean
    alreadySingle: boolean
  } | null>(null)
  const suppressClickRef = useRef(false)

  useEffect(() => {
    const bump = () => setSyncRev((v) => v + 1)
    const offTariffs = onTariffsChanged(bump)
    const offSettings = onSettingsChanged(bump)
    return () => { offTariffs(); offSettings() }
  }, [])

  useEffect(() => {
    const onPointerUp = () => {
      const drag = dragRef.current
      if (!drag) return
      dragRef.current = null
      if (drag.moved || drag.alreadySingle) {
        suppressClickRef.current = true
        setTimeout(() => { suppressClickRef.current = false }, 0)
        setDrawerOpen(true)
      }
    }
    window.addEventListener("pointerup", onPointerUp)
    return () => window.removeEventListener("pointerup", onPointerUp)
  }, [])

  const [params, setParams] = useSearchParams()
  // ?rate_plan absent: the customer's negotiated plan, else the card rate; ?rate_plan= (empty): the card
  // rate, chosen; else that plan. The card is every customer's price, so it is asked for with no customer.
  const rateParam = params.get("rate_plan")
  const ratePlan = rateParam ?? ""
  const company = params.get("company") ?? ""
  const cardChosen = rateParam === ""
  // the property's active plans, kept from the last answer so a refused query still offers a way back
  const [plans, setPlans] = useState<RatePlanPolicy[]>([])
  // customers are offered only to a user native DocPerm lets list Corporate Accounts
  const [customers, setCustomers] = useState<{ name: string; label: string }[] | null>(null)
  useEffect(() => {
    listResource("Corporate Account", { fields: ["name", "company_name"], filters: [["disabled", "=", 0]],
      orderBy: "company_name asc", limit: 500 })
      .then((rows) => setCustomers(rows.map((row) => ({ name: row.name, label: String(row.company_name ?? row.name) }))))
      .catch(() => setCustomers(null))
  }, [])

  useEffect(() => {
    // a superseded query (a slower plan, an earlier date) never reaches the grid
    let live = true
    setData(null)
    setErr(null)
    call<Calendar>("kamra.rates.rate_calendar", { property: getCurrentProperty(), from_date: from, days,
      ...(ratePlan ? { rate_plan: ratePlan } : cardChosen ? { rate_plan: "" } : {}),
      ...(company && !cardChosen ? { company } : {}) })
      .then((d) => {
        if (!live) return
        setData(d)
        setPlans(d.rate_plans ?? [])
      })
      .catch((e) => { if (live) setErr(serverError(e)) })
    return () => { live = false }
  }, [from, days, ratePlan, company, cardChosen, syncRev])

  const defaultIsTariff = !!plans.find((p) => p.name === data?.default_plan)?.is_tariff
    || (rateParam === null && !company && !!data?.rate_plan)
  const choose = (key: "rate_plan" | "company", value: string) => {
    setPick(null)
    setSelection(null)
    setDrawerOpen(false)
    setParams((p) => {
      // Customer controls Rate plan (SAP Fiori IN/OUT parameter): a plan chosen holds only for the customer it
      // was chosen with, so a new or cleared customer derives it again (its negotiated plan, else the default/card rate)
      if (key === "company") p.delete("rate_plan")
      // with a customer or a tariff default plan, the card rate stays chosen as ?rate_plan= so the default does not replace it
      if (value || (key === "rate_plan" && (p.get("company") || defaultIsTariff))) p.set(key, value)
      else p.delete(key)
      return p
    }, { replace: true })
  }
  const reset = () => {
    setPick(null)
    setSelection(null)
    setDrawerOpen(false)
    setParams((p) => {
      p.delete("rate_plan")
      p.delete("company")
      return p
    }, { replace: true })
  }

  const plan = data?.rate_plan ?? null
  // the server priced the customer's negotiated plan: no plan was chosen
  const negotiated = !!plan && rateParam === null && !!company
  const customerLabel = customers?.find((c) => c.name === company)?.label ?? company
  const locale = moneyLocale()
  const digits = moneyDigits(data?.property_currency || undefined)
  const amount = (n: number) => Number(n ?? 0).toLocaleString(locale,
    { minimumFractionDigits: digits, maximumFractionDigits: digits })
  const contractCurrency = plan?.currency || data?.room_types.flatMap((r) => r.nights)
    .find((x) => x.contract)?.contract?.currency
  const canEdit = Boolean((data?.can_edit ?? true) && canEditRates(roles))
  const orderedDates = data?.room_types[0]?.nights.map((n) => n.date) ?? []

  useEffect(() => {
    if (!canEdit && mode === "edit") {
      setMode("view")
      setSelection(null)
      setDrawerOpen(false)
    }
  }, [canEdit, mode])

  const picked = data && pick ? data.room_types.find((r) => r.room_type === pick.rt)?.nights[pick.i] : null
  const pickedType = data && pick ? data.room_types.find((r) => r.room_type === pick.rt) : null

  const startCellDrag = (r: CalendarRoomType, date: string, shiftKey = false) => {
    if (mode !== "edit" || !canEdit) return
    if (shiftKey && selection && selection.roomType === r.room_type && !drawerOpen) {
      // Preserve existing selection anchor so the following Shift+Click extends the range
      return
    }
    const alreadySingle =
      !drawerOpen &&
      selection?.roomType === r.room_type &&
      selection.dates.length === 1 &&
      selection.startDate === date
    dragRef.current = {
      roomType: r.room_type,
      roomTypeName: r.name,
      anchorDate: date,
      moved: false,
      alreadySingle,
    }
    setSelection({
      roomType: r.room_type,
      roomTypeName: r.name,
      startDate: date,
      endDate: date,
      dates: [date],
    })
  }

  const extendCellDrag = (r: CalendarRoomType, date: string) => {
    const drag = dragRef.current
    if (!drag || drag.roomType !== r.room_type) return
    if (date !== drag.anchorDate) drag.moved = true
    const dates = normalizeDateRange(drag.anchorDate, date, orderedDates)
    setSelection({
      roomType: drag.roomType,
      roomTypeName: drag.roomTypeName,
      startDate: dates[0],
      endDate: dates[dates.length - 1],
      dates,
    })
  }

  const handleEditTap = (r: CalendarRoomType, date: string, shiftKey = false) => {
    if (mode !== "edit" || !canEdit) return
    if (suppressClickRef.current) {
      suppressClickRef.current = false
      return
    }
    if ((shiftKey || isMobile) && selection && selection.roomType === r.room_type && !drawerOpen) {
      const dates = normalizeDateRange(selection.startDate, date, orderedDates)
      setSelection({
        roomType: r.room_type,
        roomTypeName: r.name,
        startDate: dates[0],
        endDate: dates[dates.length - 1],
        dates,
      })
      return
    }
    setSelection({
      roomType: r.room_type,
      roomTypeName: r.name,
      startDate: date,
      endDate: date,
      dates: [date],
    })
  }

  const handleCellKeyDown = (e: React.KeyboardEvent, r: CalendarRoomType, idx: number, date: string) => {
    if (mode !== "edit" || !canEdit) return
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault()
      handleEditTap(r, date, e.shiftKey)
      if (e.key === "Enter") setDrawerOpen(true)
      return
    }
    if (e.shiftKey && (e.key === "ArrowRight" || e.key === "ArrowLeft")) {
      const nextIdx = e.key === "ArrowRight" ? idx + 1 : idx - 1
      if (nextIdx >= 0 && nextIdx < r.nights.length) {
        e.preventDefault()
        const anchor = selection?.roomType === r.room_type ? selection.startDate : date
        const nextDate = r.nights[nextIdx].date
        const dates = normalizeDateRange(anchor, nextDate, orderedDates)
        setSelection({
          roomType: r.room_type,
          roomTypeName: r.name,
          startDate: dates[0],
          endDate: dates[dates.length - 1],
          dates,
        })
      }
    }
  }

  const isCellSelected = (rt: string, date: string) =>
    mode === "edit" && selection?.roomType === rt && selection.dates.includes(date)

  const selectedFirstNight = selection && data
    ? data.room_types
        .find((r) => r.room_type === selection.roomType)
        ?.nights.find((n) => n.date === selection.startDate) ?? null
    : null

  const handleSaved = (result: BulkGridResult) => {
    setDrawerOpen(false)
    setSelection(null)
    setNotice(
      t("Saved {rates} rate(s) and {restrictions} restriction(s). OTA sync queued.", {
        rates: result.rates_updated,
        restrictions: result.restrictions_updated + result.restrictions_deleted,
      }),
    )
    setSyncRev((v) => v + 1)
  }

  const renderBadges = (x: CalendarNight) => {
    const badges = restrictionBadges(x)
    if (!badges.length) return null
    return (
      <span className="mt-0.5 flex flex-wrap justify-center gap-0.5">
        {badges.map((b) => (
          <span
            key={b.kind}
            data-badge={b.kind}
            className={cn(
              "inline-flex items-center rounded px-1 py-0 text-[10px] font-semibold leading-tight",
              b.kind === "stop_sell" && "bg-danger-fill text-on-solid",
              (b.kind === "cta" || b.kind === "ctd") && "bg-warning-soft text-warning-ink",
              (b.kind === "min_los" || b.kind === "max_los") && "bg-info-soft text-info-ink",
            )}
          >
            {b.label}
          </span>
        ))}
      </span>
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-lg font-semibold text-ink">
            {t("Rate calendar")}
            <InfoTip doc="revenue">
              {t("BAR (best available rate) is the base price of a room type. A season changes it for a date range, and a busy-house rule raises it when the house fills. This table shows the result per night. Rate plans and vouchers apply on top, per booking.")}
            </InfoTip>
          </h1>
          <p className="text-sm text-muted-ink">
            {t("What each room costs per night, and what moved it.")}{" "}
            {data && (data.tax_inclusive ? t("Prices include tax.") : t("Prices are before tax."))}
          </p>
          {plan && (
            <p data-rate-plan-policy className="mt-1 text-sm text-secondary-ink">
              <span className="block font-medium text-ink">
                {t("Rate plan: {plan}", { plan: plan.code && plan.code !== plan.label
                  ? `${plan.label || plan.name} (${plan.code})` : plan.label || plan.name })}
                {negotiated && <>{" · "}{t("Negotiated rate plan for {customer}", { customer: customerLabel })}</>}
              </span>
              {t("Exchange rate lock: {policy}", { policy: t(plan.fx_lock_policy) })}
              {" · "}{roundingRule(plan.rounding_step, plan.rounding_mode, locale)}
              <span className="block text-xs text-muted-ink">{fxLockNote(plan.fx_lock_policy)}</span>
              {contractCurrency && contractCurrency !== data?.property_currency && (
                <span className="block text-xs text-muted-ink">
                  {t("{currency} contract tariff · local price at the published rate, rounded as the rate plan rounds", { currency: contractCurrency })}
                </span>
              )}
            </p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canEdit && (
            <div className="inline-flex rounded-lg border border-control bg-canvas p-0.5 text-xs">
              <button
                type="button"
                aria-pressed={mode === "view"}
                onClick={() => {
                  setMode("view")
                  setSelection(null)
                  setDrawerOpen(false)
                }}
                className={cn(
                  "min-h-9 rounded-md px-2.5 font-medium transition-colors",
                  mode === "view" ? "bg-surface text-ink shadow-xs" : "text-secondary-ink hover:text-ink",
                )}
              >
                {t("View mode")}
              </button>
              <button
                type="button"
                aria-pressed={mode === "edit"}
                onClick={() => {
                  setMode("edit")
                  setPick(null)
                }}
                className={cn(
                  "inline-flex min-h-9 items-center gap-1 rounded-md px-2.5 font-medium transition-colors",
                  mode === "edit" ? "bg-accent-fill text-on-solid shadow-xs" : "text-secondary-ink hover:text-ink",
                )}
              >
                <SlidersHorizontal className="size-3.5" aria-hidden />
                {t("Edit rates & restrictions")}
              </button>
            </div>
          )}
          <select
            className="rounded-lg border border-control bg-surface px-2 py-1.5 text-sm"
            value={plan ? plan.name : ratePlan}
            onChange={(e) => choose("rate_plan", e.target.value)}
            aria-label={t("Rate plan")}
          >
            {!data?.rate_plan_required && <option value="">{t("Card rate (no rate plan)")}</option>}
            {data?.rate_plan_required && !plans.length && (
              <option value="">{t("Set a default rate plan in Rate Plans.")}</option>
            )}
            {plans.map((p) => <option key={p.name} value={p.name}>{p.label || p.name}</option>)}
            {/* a linked plan that is not (or not yet) one of the active plans: named as asked, never as the card */}
            {ratePlan && !plans.some((p) => p.name === ratePlan) && <option value={ratePlan}>{ratePlan}</option>}
          </select>
          {customers && (
            <select
              className="rounded-lg border border-control bg-surface px-2 py-1.5 text-sm"
              value={company}
              onChange={(e) => choose("company", e.target.value)}
              aria-label={t("Customer")}
            >
              <option value="">{t("Public rate (no customer)")}</option>
              {customers.map((c) => <option key={c.name} value={c.name}>{c.label}</option>)}
              {/* a linked customer that is not an active Corporate Account: named and flagged, never as the public rate */}
              {company && !customers.some((c) => c.name === company) && (
                <option value={company}>{t("Not available: {reason}", { reason: company })}</option>
              )}
            </select>
          )}
          <Button variant="outline" aria-label={t("Earlier")} onClick={() => setFrom(shift(data?.from ?? "", -days))} disabled={!data}>
            <ChevronLeft className="size-4" aria-hidden />
          </Button>
          <span className="text-sm tabular-nums text-secondary-ink">{data?.from}</span>
          <Button variant="outline" aria-label={t("Later")} onClick={() => setFrom(shift(data?.from ?? "", days))} disabled={!data}>
            <ChevronRight className="size-4" aria-hidden />
          </Button>
          <select
            className="rounded-lg border border-control bg-surface px-2 py-1.5 text-sm"
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

      {mode === "edit" && canEdit && (
        <div className="flex min-h-14 flex-wrap items-center justify-between gap-2 rounded-lg border border-accent-line bg-accent-subtle px-3 py-2 text-xs text-accent-ink">
          <span>
            {selection
              ? t("{room}: {start} to {end} ({n} night(s) selected)", {
                  room: selection.roomTypeName,
                  start: selection.startDate,
                  end: selection.endDate,
                  n: selection.dates.length,
                })
              : t("Drag across dates (or tap start and end dates) to edit BAR rates, MinLOS, MaxLOS, CTA, CTD, and Stop-Sell.")}
          </span>
          {selection && (
            <div className="flex items-center gap-2">
              <Button type="button" className="min-h-9 px-3 text-xs" onClick={() => setDrawerOpen(true)}>
                {t("Edit selected")}
              </Button>
              <Button
                type="button"
                variant="ghost"
                className="min-h-9 px-2 text-xs"
                onClick={() => {
                  setSelection(null)
                  setDrawerOpen(false)
                }}
              >
                {t("Clear")}
              </Button>
            </div>
          )}
        </div>
      )}

      {notice && (
        <div className="rounded-lg border border-success-line bg-success-subtle px-3 py-2 text-sm text-success-ink">
          {notice}
        </div>
      )}

      {err && (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
          <p>{translate(err)}</p>
          {(rateParam !== null || company) && (
            <Button variant="outline" onClick={reset}>{t("Clear rate plan and customer")}</Button>
          )}
        </div>
      )}

      {/* Desktop & Tablet Matrix */}
      {data && !isMobile && (
        <Card>
          <CardContent className="overflow-x-auto p-0">
            <table className="w-full min-w-max select-none text-sm">
              <thead>
                <tr className="border-b border-line text-xs text-muted-ink">
                  <th className="sticky start-0 bg-surface px-3 py-2 text-start font-medium">{t("Room type")}</th>
                  {!plan && !data.rate_plan_required && <th className="px-2 py-2 text-end font-medium">{t("BAR")}</th>}
                  {data.room_types[0]?.nights.map((x) => {
                    const d = new Date(`${x.date}T00:00:00`)
                    const wk = d.getDay() === 5 || d.getDay() === 6
                    return (
                      <th key={x.date} className={cn("px-2 py-2 text-center font-medium", wk && "text-ink")}>
                        <div>{d.toLocaleDateString(uiDateLocale(undefined), { weekday: "short" })}</div>
                        <div className="tabular-nums">{x.date.slice(5)}</div>
                      </th>
                    )
                  })}
                </tr>
              </thead>
              <tbody>
                {data.room_types.map((r) => {
                  const noRooms = r.room_count === 0
                  return (
                    <tr key={r.room_type} className="border-b border-line-subtle">
                      <td className="sticky start-0 bg-surface px-3 py-2 font-medium">
                        <span>{r.name}</span>
                        {noRooms && (
                          <span className="ms-2 inline-block rounded-full bg-warning-subtle px-2 py-0.5 text-xs font-medium text-warning-ink">
                            {t("No rooms")}
                          </span>
                        )}
                      </td>
                      {!plan && !data.rate_plan_required && (
                        <td className="px-2 py-2 text-end tabular-nums text-muted-ink">
                          {noRooms ? "—" : `${cur()}${amount(r.base_price)}`}
                        </td>
                      )}
                      {r.nights.map((x, i) => {
                        const on = mode === "view" && pick?.rt === r.room_type && pick.i === i
                        const selected = isCellSelected(r.room_type, x.date)
                        if (noRooms) {
                          return (
                            <td key={x.date} className="p-0.5 text-center">
                              <button
                                type="button"
                                onClick={() => setPick(on ? null : { rt: r.room_type, i })}
                                aria-pressed={on}
                                aria-label={t("No rooms")}
                                title={t("No rooms configured for this room type.")}
                                className={cn("w-full min-w-16 rounded-md px-2 py-1.5 text-muted-ink hover:bg-canvas",
                                  on && "ring-2 ring-accent-control")}
                              >—</button>
                            </td>
                          )
                        }
                        if (x.available === false || x.rate == null) {
                          return (
                            <td key={x.date} className="p-0.5 text-center">
                              <button
                                type="button"
                                data-cell-rt={r.room_type}
                                data-cell-date={x.date}
                                onPointerDown={(e) => startCellDrag(r, x.date, e.shiftKey)}
                                onPointerEnter={() => extendCellDrag(r, x.date)}
                                onPointerOver={() => extendCellDrag(r, x.date)}
                                onClick={(e) => {
                                  if (mode === "edit") handleEditTap(r, x.date, e.shiftKey)
                                  else setPick(on ? null : { rt: r.room_type, i })
                                }}
                                onKeyDown={(e) => handleCellKeyDown(e, r, i, x.date)}
                                aria-pressed={mode === "edit" ? selected : on}
                                aria-label={t("Not available: {reason}", { reason: x.refusal ?? "" })}
                                title={x.refusal}
                                className={cn(
                                  "w-full min-w-16 rounded-md px-2 py-1.5 text-muted-ink hover:bg-canvas",
                                  (on || selected) && "ring-2 ring-accent-control",
                                )}
                              >
                                —
                                {renderBadges(x)}
                              </button>
                            </td>
                          )
                        }
                        const up = !plan && x.rate > r.base_price
                        const down = !plan && x.rate < r.base_price
                        return (
                          <td key={x.date} className="p-0.5 text-center">
                            <button
                              type="button"
                              data-cell-rt={r.room_type}
                              data-cell-date={x.date}
                              onPointerDown={(e) => startCellDrag(r, x.date, e.shiftKey)}
                              onPointerEnter={() => extendCellDrag(r, x.date)}
                              onPointerOver={() => extendCellDrag(r, x.date)}
                              onClick={(e) => {
                                if (mode === "edit") handleEditTap(r, x.date, e.shiftKey)
                                else setPick(on ? null : { rt: r.room_type, i })
                              }}
                              onKeyDown={(e) => handleCellKeyDown(e, r, i, x.date)}
                              aria-pressed={mode === "edit" ? selected : on}
                              className={cn(
                                "w-full min-w-16 rounded-md px-2 py-1.5 tabular-nums transition-colors",
                                x.stop_sell && "bg-danger-subtle text-danger-ink",
                                !x.stop_sell && up && "bg-warning-subtle text-warning-ink",
                                !x.stop_sell && down && "bg-info-subtle text-info-ink",
                                !x.stop_sell && !up && !down && "text-secondary-ink hover:bg-canvas",
                                x.cta && "border-s-2 border-amber-500",
                                x.ctd && "border-e-2 border-amber-500",
                                (on || selected) && "bg-accent-subtle ring-2 ring-accent-control",
                              )}
                            >
                              {/* a home-currency contract's figure is the rate below: shown once */}
                              {x.contract && x.contract.currency !== data.property_currency && (
                                <span className="block text-xs text-muted-ink">
                                  {currencyText(x.contract.currency, x.contract.nightly, locale)}
                                </span>
                              )}
                              <span>{amount(x.rate)}</span>
                              {(x.season || x.demand) && (
                                <span className="ms-1 inline-block size-1.5 rounded-full bg-current align-middle opacity-60" aria-hidden />
                              )}
                              {x.source === "card" && (
                                <span
                                  className="block text-[10px] uppercase tracking-wide text-muted-ink"
                                  title={t("No contract tariff for this room type: priced from the card rate.")}
                                >
                                  {t("Card rate")}
                                </span>
                              )}
                              {renderBadges(x)}
                            </button>
                          </td>
                        )
                      })}
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </CardContent>
        </Card>
      )}

      {/* Mobile Room-Type Cards (< 640px: zero horizontal overflow, 44px touch targets) */}
      {data && isMobile && (
        <div className="space-y-3">
          {data.room_types.map((r) => {
            const noRooms = r.room_count === 0
            return (
              <Card key={r.room_type}>
                <CardHeader className="flex flex-row items-center justify-between gap-2 pb-2">
                  <div>
                    <CardTitle className="text-base">{r.name}</CardTitle>
                    {!plan && !data.rate_plan_required && !noRooms && (
                      <p className="text-xs tabular-nums text-muted-ink">
                        {t("BAR")}: {cur()}{amount(r.base_price)}
                      </p>
                    )}
                  </div>
                  {noRooms && (
                    <span className="rounded-full bg-warning-subtle px-2 py-0.5 text-xs font-medium text-warning-ink">
                      {t("No rooms")}
                    </span>
                  )}
                </CardHeader>
                <CardContent className="grid grid-cols-2 gap-2">
                  {r.nights.map((x, i) => {
                    const on = mode === "view" && pick?.rt === r.room_type && pick.i === i
                    const selected = isCellSelected(r.room_type, x.date)
                    return (
                      <button
                        key={x.date}
                        type="button"
                        data-cell-rt={r.room_type}
                        data-cell-date={x.date}
                        disabled={noRooms}
                        onClick={() => {
                          if (mode === "edit") handleEditTap(r, x.date, true)
                          else setPick(on ? null : { rt: r.room_type, i })
                        }}
                        className={cn(
                          "flex min-h-14 flex-col items-start justify-between rounded-lg border border-line p-2 text-start text-xs tabular-nums",
                          x.stop_sell && "border-danger-line bg-danger-subtle text-danger-ink",
                          (on || selected) && "border-accent-control bg-accent-subtle ring-2 ring-accent-control",
                        )}
                      >
                        <span className="text-[11px] text-muted-ink">{x.date.slice(5)}</span>
                        <span className="text-sm font-semibold">
                          {noRooms || x.available === false || x.rate == null ? "—" : amount(x.rate)}
                        </span>
                        {renderBadges(x)}
                      </button>
                    )
                  })}
                </CardContent>
              </Card>
            )
          })}
        </div>
      )}

      {!plan && !data?.rate_plan_required && (
        <div className="flex flex-wrap gap-4 text-xs text-muted-ink">
          <span className="inline-flex items-center gap-1.5"><span className="size-3 rounded bg-warning-subtle ring-1 ring-amber-200" aria-hidden />{t("Above BAR")}</span>
          <span className="inline-flex items-center gap-1.5"><span className="size-3 rounded bg-info-subtle ring-1 ring-sky-200" aria-hidden />{t("Below BAR")}</span>
          <span className="inline-flex items-center gap-1.5"><span className="rounded bg-danger-fill px-1 text-[10px] font-semibold text-on-solid">SS</span>{t("Stop-sell")}</span>
          <span className="inline-flex items-center gap-1.5"><span className="rounded bg-warning-soft px-1 text-[10px] font-semibold text-warning-ink">CTA/CTD</span>{t("Arrival / departure closed")}</span>
          <span>{t("A dot means a season or busy-house rule moved it. Select a night to see which.")}</span>
        </div>
      )}

      {picked && pickedType && pickedType.room_count === 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {pickedType.name} · {picked.date}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            <p className="text-warning-ink">{t("No rooms configured for this room type.")}</p>
          </CardContent>
        </Card>
      )}

      {picked && pickedType && pickedType.room_count !== 0 && (picked.available === false || picked.rate == null || picked.contract) && (
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
              <p className="text-danger-ink">{t("Not available: {reason}", { reason: picked.refusal ?? "" })}</p>
            )}
          </CardContent>
        </Card>
      )}

      {picked && pickedType && pickedType.room_count !== 0 && picked.rate != null && !picked.contract && picked.available !== false && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              {pickedType.name} · {picked.date}
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1.5 text-sm">
            <p>{t("BAR")}: <span className="font-medium tabular-nums">{cur()}{amount(picked.bar)}</span></p>
            {picked.season ? (
              <p>
                {t("Season")}: <span className="font-medium">{picked.season.label}</span> ({picked.season.change}) {"->"}{" "}
                <span className="tabular-nums">{cur()}{amount(picked.after_season)}</span>
              </p>
            ) : (
              <p className="text-muted-ink">{t("No season covers this date.")}</p>
            )}
            {picked.demand ? (
              <p>
                {t("Busy house")}: {Math.round(picked.occupancy)}% {t("full")}, +{picked.demand.premium_pct}%
                {picked.demand.min_rate ? `, ${t("never below")} ${cur()}${amount(picked.demand.min_rate)}` : ""}
              </p>
            ) : (
              <p className="text-muted-ink">{t("No busy-house rule applies ({p}% full).", { p: Math.round(picked.occupancy) })}</p>
            )}
            {picked.source === "card" && (
              <p className="text-muted-ink">{t("No contract tariff for this room type: priced from the card rate.")}</p>
            )}
            <p className="font-semibold">{t("Rate")}: <span className="tabular-nums">{cur()}{amount(picked.rate)}</span></p>
            {picked.is_restricted && (
              <div className="mt-2 rounded-lg border border-warning-line bg-warning-subtle p-2.5 text-xs text-warning-ink">
                <div className="font-semibold">{t("Active stay restrictions")}</div>
                <div className="mt-1 flex flex-wrap gap-2">
                  {picked.stop_sell && <span className="font-medium text-danger-ink">{t("Stop-sell")}</span>}
                  {picked.min_los ? <span>{t("Min LOS")}: {picked.min_los}{picked.is_stay_through ? ` (${t("Stay-through")})` : ""}</span> : null}
                  {picked.max_los ? <span>{t("Max LOS")}: {picked.max_los}</span> : null}
                  {picked.cta && <span>{t("Closed to arrival")}</span>}
                  {picked.ctd && <span>{t("Closed to departure")}</span>}
                </div>
                {picked.restriction_reason && (
                  <p className="mt-1 text-secondary-ink">{picked.restriction_reason}</p>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      )}

      {drawerOpen && selection && canEdit && (
        <RateRestrictionsDrawer
          selection={selection}
          currency={data?.property_currency}
          ratePlan={plan?.name ?? null}
          ratePlanLabel={plan?.label ?? null}
          initialRate={selectedFirstNight?.card_rate ?? selectedFirstNight?.rate ?? null}
          initialRestriction={selectedFirstNight}
          allowBarEdit={canEditBarForCalendar({
            company,
            ratePlanRequired: data?.rate_plan_required,
            defaultPlan: data?.default_plan,
            plan,
            propertyCurrency: data?.property_currency,
            selectedNight: selectedFirstNight,
          })}
          onClose={() => setDrawerOpen(false)}
          onSaved={handleSaved}
        />
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
            ["/channel-health", "Channel Health", "Monitor OTA mapping parity, outbox sync, and pushed stay restrictions."],
            ["/vouchers", "Vouchers", "Discount codes with dates, minimum nights and use limits."],
            ["/long-stay", "Long-stay Discounts", "Take a percentage off stays of N nights or more."],
            ["/guardrails", "Guardrails", "Floors and ceilings no rate can cross."],
          ].map(([to, label, blurb]) => (
            <Link key={to} to={to} className="rounded-lg border border-line px-3 py-2 hover:border-accent-control">
              <span className="font-medium text-accent-ink">{t(label)} →</span>
              <span className="block text-xs text-muted-ink">{t(blurb)}</span>
            </Link>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
