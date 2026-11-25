/** Slide-over drawer for editing BAR rates (0/2/3dp) and OPERA stay restrictions over a selected date range. */
import { useEffect, useState } from "react"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { useT, t as translate } from "../lib/i18n"
import { moneyDigits } from "../lib/money"
import { cn } from "../lib/utils"
import {
  WEEKDAYS,
  rateStepForCurrency,
  type BulkGridResult,
  type GridSelection,
  type NightRestriction,
  type RateUpdateInput,
  type RestrictionUpdateInput,
} from "../lib/restrictions"
import { Button } from "./ui/button"
import { Sheet } from "./ui/sheet"

export interface RateRestrictionsDrawerProps {
  selection: GridSelection
  currency?: string | null
  ratePlan?: string | null
  ratePlanLabel?: string | null
  initialRate?: number | null
  initialRestriction?: Partial<NightRestriction> | null
  allowBarEdit: boolean
  onClose: () => void
  onSaved: (result: BulkGridResult) => void
}

export default function RateRestrictionsDrawer({
  selection,
  currency,
  ratePlan,
  ratePlanLabel,
  initialRate,
  initialRestriction,
  allowBarEdit,
  onClose,
  onSaved,
}: RateRestrictionsDrawerProps) {
  const { t } = useT()
  const digits = moneyDigits(currency || undefined)
  const step = rateStepForCurrency(currency)

  const [scopeAllRooms, setScopeAllRooms] = useState(false)
  const [applyToPlan, setApplyToPlan] = useState(Boolean(ratePlan))
  const [weekdays, setWeekdays] = useState<string[]>([])
  const [barRate, setBarRate] = useState(
    initialRate != null && Number.isFinite(initialRate) ? initialRate.toFixed(digits) : "",
  )
  const [barTouched, setBarTouched] = useState(false)
  const [restrictionsTouched, setRestrictionsTouched] = useState(false)

  const [minLos, setMinLos] = useState(
    initialRestriction?.min_los ? String(initialRestriction.min_los) : "",
  )
  const [maxLos, setMaxLos] = useState(
    initialRestriction?.max_los ? String(initialRestriction.max_los) : "",
  )
  const [stayThrough, setStayThrough] = useState(Boolean(initialRestriction?.is_stay_through))
  const [cta, setCta] = useState(Boolean(initialRestriction?.cta))
  const [ctd, setCtd] = useState(Boolean(initialRestriction?.ctd))
  const [stopSell, setStopSell] = useState(Boolean(initialRestriction?.stop_sell))
  const [reason, setReason] = useState(initialRestriction?.restriction_reason || "")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setBarRate(initialRate != null && Number.isFinite(initialRate) ? initialRate.toFixed(digits) : "")
    setBarTouched(false)
    setRestrictionsTouched(false)
    setMinLos(initialRestriction?.min_los ? String(initialRestriction.min_los) : "")
    setMaxLos(initialRestriction?.max_los ? String(initialRestriction.max_los) : "")
    setStayThrough(Boolean(initialRestriction?.is_stay_through))
    setCta(Boolean(initialRestriction?.cta))
    setCtd(Boolean(initialRestriction?.ctd))
    setStopSell(Boolean(initialRestriction?.stop_sell))
    setReason(initialRestriction?.restriction_reason || "")
    setErr(null)
  }, [selection.roomType, selection.startDate, selection.endDate, digits, initialRate, initialRestriction])

  const toggleDay = (code: string) => {
    setWeekdays((prev) => (prev.includes(code) ? prev.filter((d) => d !== code) : [...prev, code]))
  }

  async function submit(clearRestrictionsOnly = false) {
    setBusy(true)
    setErr(null)
    try {
      const rateUpdates: RateUpdateInput[] = []
      const restrictionUpdates: RestrictionUpdateInput[] = []
      const dayFilter = weekdays.length ? weekdays : undefined

      if (!clearRestrictionsOnly && allowBarEdit && !scopeAllRooms && barTouched && barRate.trim() !== "") {
        const parsed = Number(barRate)
        if (!Number.isFinite(parsed) || parsed <= 0) {
          setErr(t("BAR rate must be greater than zero."))
          setBusy(false)
          return
        }
        rateUpdates.push({
          room_type: selection.roomType,
          start_date: selection.startDate,
          end_date: selection.endDate,
          ...(dayFilter ? { days_of_week: dayFilter } : {}),
          rate: Number(parsed.toFixed(digits)),
        })
      }

      if (clearRestrictionsOnly) {
        restrictionUpdates.push({
          room_type: scopeAllRooms ? null : selection.roomType,
          rate_plan: applyToPlan && ratePlan ? ratePlan : null,
          start_date: selection.startDate,
          end_date: selection.endDate,
          ...(dayFilter ? { days_of_week: dayFilter } : {}),
          stop_sell: 0,
          min_los: 0,
          max_los: 0,
          is_stay_through: 0,
          cta: 0,
          ctd: 0,
        })
      } else if (restrictionsTouched || rateUpdates.length === 0) {
        const minVal = minLos.trim() === "" ? 0 : Math.max(0, Math.floor(Number(minLos) || 0))
        const maxVal = maxLos.trim() === "" ? 0 : Math.max(0, Math.floor(Number(maxLos) || 0))
        if (minVal > 0 && maxVal > 0 && maxVal < minVal) {
          setErr(t("Maximum stay cannot be shorter than minimum stay."))
          setBusy(false)
          return
        }
        restrictionUpdates.push({
          room_type: scopeAllRooms ? null : selection.roomType,
          rate_plan: applyToPlan && ratePlan ? ratePlan : null,
          start_date: selection.startDate,
          end_date: selection.endDate,
          ...(dayFilter ? { days_of_week: dayFilter } : {}),
          stop_sell: stopSell ? 1 : 0,
          min_los: minVal,
          max_los: maxVal,
          is_stay_through: stayThrough ? 1 : 0,
          cta: cta ? 1 : 0,
          ctd: ctd ? 1 : 0,
        })
      }

      const result = await call<BulkGridResult>("kamra.restrictions.bulk_grid_update", {
        property: getCurrentProperty(),
        rate_updates: rateUpdates,
        restriction_updates: restrictionUpdates,
        reason: reason.trim(),
      })
      onSaved(result)
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet
      title={t("Edit rates & restrictions")}
      description={`${selection.roomTypeName} · ${selection.startDate}${selection.endDate !== selection.startDate ? ` → ${selection.endDate}` : ""} (${selection.dates.length})`}
      onClose={onClose}
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2">
          <Button
            type="button"
            variant="ghost"
            disabled={busy}
            className="min-h-11 text-rose-700"
            onClick={() => { void submit(true) }}
          >
            {t("Clear restrictions")}
          </Button>
          <div className="flex items-center gap-2">
            <Button type="button" variant="outline" disabled={busy} className="min-h-11" onClick={onClose}>
              {t("Cancel")}
            </Button>
            <Button type="button" disabled={busy} className="min-h-11" onClick={() => { void submit(false) }}>
              {busy ? t("Saving…") : t("Save & sync OTAs")}
            </Button>
          </div>
        </div>
      }
    >
      <div className="space-y-5 text-sm">
        <div className="space-y-2 rounded-lg border border-zinc-200 bg-zinc-50 p-3">
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">{t("Scope")}</div>
          <label className="flex min-h-11 cursor-pointer items-center gap-2.5">
            <input
              type="checkbox"
              checked={scopeAllRooms}
              onChange={(e) => {
                setScopeAllRooms(e.target.checked)
                setRestrictionsTouched(true)
              }}
              className="size-4 rounded border-zinc-300"
            />
            <span>{t("Apply restrictions to all room types (house level)")}</span>
          </label>
          {ratePlan && (
            <label className="flex min-h-11 cursor-pointer items-center gap-2.5">
              <input
                type="checkbox"
                checked={applyToPlan}
                onChange={(e) => {
                  setApplyToPlan(e.target.checked)
                  setRestrictionsTouched(true)
                }}
                className="size-4 rounded border-zinc-300"
              />
              <span>
                {t("Limit to rate plan: {plan}", { plan: ratePlanLabel || ratePlan })}
              </span>
            </label>
          )}
        </div>

        <div className="space-y-2">
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
            {t("Days of week")}
          </div>
          <div className="flex flex-wrap gap-1.5">
            {WEEKDAYS.map((wd) => {
              const active = weekdays.includes(wd.code)
              return (
                <button
                  key={wd.code}
                  type="button"
                  aria-pressed={active}
                  aria-label={t(wd.label)}
                  onClick={() => toggleDay(wd.code)}
                  className={cn(
                    "min-h-11 min-w-11 rounded-lg border px-2 text-xs font-medium transition-colors",
                    active
                      ? "border-brand-600 bg-brand-600 text-white"
                      : "border-zinc-300 bg-white text-zinc-700 hover:bg-zinc-50",
                  )}
                >
                  {wd.short}
                </button>
              )
            })}
          </div>
          <p className="text-xs text-zinc-500">{t("Leave unselected to apply to every day in the range.")}</p>
        </div>

        {allowBarEdit && !scopeAllRooms && (
          <div className="space-y-1.5">
            <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-500" htmlFor="bar-rate-input">
              {t("BAR rate")} {currency ? `(${currency})` : ""}
            </label>
            <input
              id="bar-rate-input"
              type="number"
              inputMode="decimal"
              min="0"
              step={step}
              aria-label={t("BAR rate")}
              value={barRate}
              onChange={(e) => {
                setBarRate(e.target.value)
                setBarTouched(true)
              }}
              className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm tabular-nums text-zinc-900"
            />
            <p className="text-xs text-zinc-500">
              {t("Overrides the nightly base rate for this room type and pushes to connected channels.")}
            </p>
          </div>
        )}

        <div className="space-y-3 border-t border-zinc-200 pt-4">
          <div className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
            {t("Stay restrictions")}
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1">
              <label className="block text-xs font-medium text-zinc-700" htmlFor="min-los-input">
                {t("Min LOS")}
              </label>
              <input
                id="min-los-input"
                type="number"
                min="0"
                max="365"
                step="1"
                aria-label={t("Min LOS")}
                placeholder="0"
                value={minLos}
                onChange={(e) => {
                  setMinLos(e.target.value)
                  setRestrictionsTouched(true)
                }}
                className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm tabular-nums text-zinc-900"
              />
            </div>
            <div className="space-y-1">
              <label className="block text-xs font-medium text-zinc-700" htmlFor="max-los-input">
                {t("Max LOS")}
              </label>
              <input
                id="max-los-input"
                type="number"
                min="0"
                max="365"
                step="1"
                aria-label={t("Max LOS")}
                placeholder="0"
                value={maxLos}
                onChange={(e) => {
                  setMaxLos(e.target.value)
                  setRestrictionsTouched(true)
                }}
                className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm tabular-nums text-zinc-900"
              />
            </div>
          </div>

          <div className="space-y-1.5">
            <label className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg border border-zinc-200 px-3">
              <input
                type="checkbox"
                aria-label={t("Apply Min LOS stay-through")}
                checked={stayThrough}
                onChange={(e) => {
                  setStayThrough(e.target.checked)
                  setRestrictionsTouched(true)
                }}
                className="size-4 rounded border-zinc-300"
              />
              <span>{t("Apply Min LOS stay-through")}</span>
            </label>

            <label className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg border border-amber-200 bg-amber-50/40 px-3">
              <input
                type="checkbox"
                aria-label={t("Closed to arrival")}
                checked={cta}
                onChange={(e) => {
                  setCta(e.target.checked)
                  setRestrictionsTouched(true)
                }}
                className="size-4 rounded border-zinc-300"
              />
              <span className="font-medium text-amber-900">{t("Closed to arrival (CTA)")}</span>
            </label>

            <label className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg border border-amber-200 bg-amber-50/40 px-3">
              <input
                type="checkbox"
                aria-label={t("Closed to departure")}
                checked={ctd}
                onChange={(e) => {
                  setCtd(e.target.checked)
                  setRestrictionsTouched(true)
                }}
                className="size-4 rounded border-zinc-300"
              />
              <span className="font-medium text-amber-900">{t("Closed to departure (CTD)")}</span>
            </label>

            <label className="flex min-h-11 cursor-pointer items-center gap-2.5 rounded-lg border border-rose-200 bg-rose-50/50 px-3">
              <input
                type="checkbox"
                aria-label={t("Stop-sell")}
                checked={stopSell}
                onChange={(e) => {
                  setStopSell(e.target.checked)
                  setRestrictionsTouched(true)
                }}
                className="size-4 rounded border-zinc-300"
              />
              <span className="font-medium text-rose-800">{t("Stop-sell (closeout)")}</span>
            </label>
          </div>
        </div>

        <div className="space-y-1.5">
          <label className="block text-xs font-semibold uppercase tracking-wider text-zinc-500" htmlFor="restriction-reason-input">
            {t("Reason")}
          </label>
          <input
            id="restriction-reason-input"
            type="text"
            aria-label={t("Reason")}
            placeholder={t("Optional audit note (e.g. Peak weekend, Conference block)")}
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            className="min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm text-zinc-900"
          />
        </div>

        {err && (
          <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
            {translate(err)}
          </div>
        )}
      </div>
    </Sheet>
  )
}
