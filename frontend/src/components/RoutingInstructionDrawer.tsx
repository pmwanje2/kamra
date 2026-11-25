import { useCallback, useEffect, useMemo, useState } from "react"
import { Trash2, Pencil } from "lucide-react"
import { call } from "../lib/api"
import { useT, t as translate } from "../lib/i18n"
import { fmtMoney, type MoneyContext, moneyDigits, moneyStep } from "../lib/money"
import { serverError } from "../lib/resource"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"
import { Sheet } from "./ui/sheet"

export interface RoutingCodeItem {
  charge_type?: string | null
  transaction_code?: string | null
  description?: string | null
}

export interface RoutingInstructionRow {
  name: string
  property: string
  reservation?: string | null
  group_booking?: string | null
  target_type: "Window" | "Room" | "Group Master"
  target_window: number
  target_reservation?: string | null
  target_room_number?: string | null
  target_guest_name?: string | null
  disabled: number
  all_codes: number
  codes: RoutingCodeItem[]
  from_date?: string | null
  to_date?: string | null
  monday: number
  tuesday: number
  wednesday: number
  thursday: number
  friday: number
  saturday: number
  sunday: number
  limit_type: "None" | "Amount" | "Percent"
  limit_value: number
  per_day: number
  used_amount: number
}

interface TransactionCodeOption {
  code: string
  description: string
  charge_type?: string | null
  is_alcohol?: number
}

interface InHouseOption {
  name: string
  room_number?: string | null
  guest_name?: string | null
}

const WEEKDAYS = [
  { key: "monday", label: "Monday" },
  { key: "tuesday", label: "Tuesday" },
  { key: "wednesday", label: "Wednesday" },
  { key: "thursday", label: "Thursday" },
  { key: "friday", label: "Friday" },
  { key: "saturday", label: "Saturday" },
  { key: "sunday", label: "Sunday" },
] as const

type WeekdayKey = (typeof WEEKDAYS)[number]["key"]

const inputCls =
  "min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

const DEFAULT_DAYS: Record<WeekdayKey, boolean> = {
  monday: true,
  tuesday: true,
  wednesday: true,
  thursday: true,
  friday: true,
  saturday: true,
  sunday: true,
}

export default function RoutingInstructionDrawer({
  open,
  onClose,
  property,
  currency,
  money,
  reservation,
  groupBooking,
  onChanged,
}: {
  open: boolean
  onClose: () => void
  property: string
  currency: string
  money?: MoneyContext
  reservation: string | null
  groupBooking: string | null
  onChanged: () => void
}) {
  const { t, lang } = useT()
  const moneyContext = money ?? { currency }
  const weekday = new Intl.DateTimeFormat(lang, { weekday: "narrow", timeZone: "UTC" })
  const [rules, setRules] = useState<RoutingInstructionRow[]>([])
  const [codes, setCodes] = useState<TransactionCodeOption[]>([])
  const [rooms, setRooms] = useState<InHouseOption[]>([])
  const [windowCap, setWindowCap] = useState(8)
  const [loading, setLoading] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [editingName, setEditingName] = useState<string | null>(null)
  const [targetType, setTargetType] = useState<"Window" | "Room" | "Group Master">("Window")
  const [targetWindow, setTargetWindow] = useState(2)
  const [targetReservation, setTargetReservation] = useState("")
  const [allCodes, setAllCodes] = useState(false)
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set())
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set())
  const [codeQuery, setCodeQuery] = useState("")
  const [fromDate, setFromDate] = useState("")
  const [toDate, setToDate] = useState("")
  const [days, setDays] = useState<Record<WeekdayKey, boolean>>(DEFAULT_DAYS)
  const [limitType, setLimitType] = useState<"None" | "Amount" | "Percent">("None")
  const [limitValue, setLimitValue] = useState("")
  const [perDay, setPerDay] = useState(false)

  const resetForm = useCallback(() => {
    setEditingName(null)
    setTargetType("Window")
    setTargetWindow(2)
    setTargetReservation("")
    setAllCodes(false)
    setSelectedCodes(new Set())
    setSelectedTypes(new Set())
    setCodeQuery("")
    setFromDate("")
    setToDate("")
    setDays(DEFAULT_DAYS)
    setLimitType("None")
    setLimitValue("")
    setPerDay(false)
  }, [])

  const load = useCallback(async () => {
    if (!open || (!reservation && !groupBooking)) return
    setLoading(true)
    setError(null)
    try {
      const [list, opts] = await Promise.all([
        call<RoutingInstructionRow[]>("kamra.routing.list_routing_instructions", {
          reservation: reservation || undefined,
          group_booking: !reservation ? groupBooking || undefined : undefined,
        }),
        call<{
          window_cap?: number
          transaction_codes?: TransactionCodeOption[]
          in_house_reservations?: InHouseOption[]
        }>("kamra.routing.routing_options", {
          property,
          reservation: reservation || undefined,
        }),
      ])
      setRules(Array.isArray(list) ? list : [])
      setWindowCap(Math.min(Math.max(opts?.window_cap ?? 8, 1), 8))
      setCodes(Array.isArray(opts?.transaction_codes) ? opts.transaction_codes : [])
      setRooms(Array.isArray(opts?.in_house_reservations) ? opts.in_house_reservations : [])
    } catch (e) {
      setError(serverError(e))
    } finally {
      setLoading(false)
    }
  }, [open, property, reservation, groupBooking])

  useEffect(() => {
    if (open) void load()
  }, [open, load])

  const filteredCodes = useMemo(() => {
    const q = codeQuery.trim().toLowerCase()
    if (!q) return codes
    return codes.filter(
      (c) =>
        c.code.toLowerCase().includes(q) ||
        (c.description || "").toLowerCase().includes(q) ||
        (c.charge_type || "").toLowerCase().includes(q),
    )
  }, [codes, codeQuery])

  if (!open) return null

  function startEdit(r: RoutingInstructionRow) {
    setEditingName(r.name)
    setTargetType(r.target_type)
    setTargetWindow(r.target_window || 1)
    setTargetReservation(r.target_reservation || "")
    setAllCodes(Boolean(r.all_codes))
    setSelectedCodes(
      new Set(
        (r.codes || [])
          .map((c) => c.transaction_code || "")
          .filter(Boolean),
      ),
    )
    setSelectedTypes(new Set((r.codes || []).flatMap((c) => !c.transaction_code && c.charge_type ? [c.charge_type] : [])))
    setFromDate(r.from_date || "")
    setToDate(r.to_date || "")
    setDays({
      monday: Boolean(r.monday),
      tuesday: Boolean(r.tuesday),
      wednesday: Boolean(r.wednesday),
      thursday: Boolean(r.thursday),
      friday: Boolean(r.friday),
      saturday: Boolean(r.saturday),
      sunday: Boolean(r.sunday),
    })
    setLimitType(r.limit_type || "None")
    setLimitValue(r.limit_value ? String(r.limit_value) : "")
    setPerDay(Boolean(r.per_day))
  }

  async function saveRule() {
    setBusy(true)
    setError(null)
    try {
      const data: Record<string, unknown> = {
        ...(editingName ? { name: editingName } : {}),
        reservation: reservation || undefined,
        group_booking: !reservation ? groupBooking || undefined : undefined,
        target_type: targetType,
        target_window: targetWindow,
        target_reservation: targetType === "Room" ? targetReservation || undefined : undefined,
        all_codes: allCodes ? 1 : 0,
        codes: allCodes ? [] : [
          ...[...selectedCodes].map((transaction_code) => ({ transaction_code })),
          ...[...selectedTypes].map((charge_type) => ({ charge_type })),
        ],
        from_date: fromDate || null,
        to_date: toDate || null,
        monday: days.monday ? 1 : 0,
        tuesday: days.tuesday ? 1 : 0,
        wednesday: days.wednesday ? 1 : 0,
        thursday: days.thursday ? 1 : 0,
        friday: days.friday ? 1 : 0,
        saturday: days.saturday ? 1 : 0,
        sunday: days.sunday ? 1 : 0,
        limit_type: allCodes ? "None" : limitType,
        limit_value: !allCodes && limitType !== "None" ? Number(limitValue || 0) : 0,
        per_day: !allCodes && limitType === "Amount" && perDay ? 1 : 0,
      }
      await call("kamra.routing.save_routing_instruction", { data })
      resetForm()
      await load()
      onChanged()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function removeRule(name: string) {
    setBusy(true)
    setError(null)
    try {
      await call("kamra.routing.delete_routing_instruction", { name })
      if (editingName === name) resetForm()
      await load()
      onChanged()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const canSave =
    (allCodes || selectedCodes.size > 0 || selectedTypes.size > 0) &&
    Object.values(days).some(Boolean) &&
    (targetType !== "Room" || Boolean(targetReservation)) &&
    (allCodes || limitType === "None" || Number(limitValue) > 0)

  return (
    <Sheet
      wide
      title={t("Routing instructions")}
      description={t("Route charges automatically to another window, room, or group master.")}
      onClose={onClose}
    >
      <div data-testid="routing-drawer" className="space-y-6 text-sm">
        {error && (
          <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-rose-700">
            {translate(error)}
          </div>
        )}

        <section className="space-y-2.5">
          <h3 className="text-xs font-semibold uppercase tracking-wider text-zinc-500">
            {t("Active & saved rules")}
          </h3>
          {loading ? (
            <p className="text-xs text-zinc-400">{t("Loading…")}</p>
          ) : rules.length === 0 ? (
            <p className="rounded-lg border border-dashed border-zinc-200 p-4 text-xs text-zinc-500">
              {t("No routing instructions yet. Create one below to route charges automatically.")}
            </p>
          ) : (
            <ul className="space-y-2.5">
              {rules.map((r) => {
                const limitVal = Number(r.limit_value || 0)
                const usedVal = Number(r.used_amount || 0)
                const pct = limitVal > 0 ? Math.min(100, Math.round((usedVal / limitVal) * 100)) : 0
                return (
                  <li
                    key={r.name}
                    className="rounded-xl border border-zinc-200 bg-zinc-50/70 p-3.5"
                  >
                    <div className="flex flex-wrap items-start justify-between gap-2">
                      <div className="space-y-1">
                        <div className="flex flex-wrap items-center gap-1.5">
                          <Badge tone={r.disabled ? "zinc" : "brand"}>
                            {r.target_type === "Window"
                              ? t("Window W{n}", { n: r.target_window })
                              : r.target_type === "Room"
                                ? t("Room {room} · W{n}", {
                                    room: r.target_room_number || r.target_reservation || "",
                                    n: r.target_window,
                                  })
                                : t("Group Master · W{n}", { n: r.target_window })}
                          </Badge>
                          {r.disabled ? <Badge tone="zinc">{t("Disabled")}</Badge> : null}
                          {(r.from_date || r.to_date) && (
                            <span className="text-xs text-zinc-500">
                              {r.from_date || "…"} → {r.to_date || "…"}
                            </span>
                          )}
                        </div>
                        <div className="flex flex-wrap gap-1 text-xs">
                          {r.all_codes ? (
                            <span className="rounded bg-white px-2 py-0.5 text-zinc-700 ring-1 ring-zinc-200">
                              {t("All Transaction Codes")}
                            </span>
                          ) : (
                            (r.codes || []).map((c, idx) => (
                              <span
                                key={`${r.name}-${idx}`}
                                className="rounded bg-white px-2 py-0.5 text-zinc-700 ring-1 ring-zinc-200"
                              >
                                {c.transaction_code
                                  ? `${c.transaction_code}${c.description ? ` · ${translate(c.description)}` : ""}`
                                  : translate(c.charge_type || "")}
                              </span>
                            ))
                          )}
                        </div>
                        <div className="flex flex-wrap items-center gap-1 pt-0.5">
                          {WEEKDAYS.map((d, index) => (
                            <span
                              key={d.key}
                              className={
                                "inline-flex size-5 items-center justify-center rounded text-[10px] font-semibold " +
                                (r[d.key]
                                  ? "bg-brand-100 text-brand-700"
                                  : "bg-zinc-100 text-zinc-400")
                              }
                            >
                              {weekday.format(Date.UTC(2024, 0, 1 + index))}
                            </span>
                          ))}
                        </div>
                      </div>
                      <div className="flex items-center gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          className="min-h-11 min-w-11 px-2 py-1"
                          disabled={busy}
                          aria-label={t("Edit routing rule")}
                          onClick={() => startEdit(r)}
                        >
                          <Pencil className="size-3.5" aria-hidden />
                        </Button>
                        <Button
                          type="button"
                          variant="ghost"
                          className="min-h-11 min-w-11 px-2 py-1"
                          disabled={busy}
                          data-testid={`delete-routing-${r.name}`}
                          aria-label={t("Remove routing rule")}
                          onClick={() => void removeRule(r.name)}
                        >
                          <Trash2 className="size-3.5 text-rose-600" aria-hidden />
                        </Button>
                      </div>
                    </div>

                    {r.limit_type !== "Amount" && (usedVal > 0 || r.limit_type === "None") && (
                      <div className="mt-2.5 text-xs text-zinc-600">
                        {t("Used")} <span dir="ltr" className="inline-block tabular-nums">{fmtMoney(usedVal, moneyContext)}</span>
                        {r.limit_type === "None" && <> · {t("No limit")}</>}
                      </div>
                    )}
                    {r.limit_type !== "None" && limitVal > 0 && (
                      <div className="mt-2.5 space-y-1">
                        <div className="flex items-center justify-between text-xs text-zinc-600">
                          <span>
                            {r.limit_type === "Percent"
                              ? t("Limit: {pct}%", { pct: limitVal })
                              : t("Used {used} / {limit}", {
                                  used: fmtMoney(usedVal, moneyContext),
                                  limit: fmtMoney(limitVal, moneyContext),
                                })}
                            {r.per_day ? ` · ${t("Per Day")}` : ""}
                          </span>
                          {r.limit_type === "Amount" && (
                            <span className="tabular-nums font-medium">{pct}%</span>
                          )}
                        </div>
                        {r.limit_type === "Amount" && (
                          <div className="h-2 w-full overflow-hidden rounded-full bg-zinc-200">
                            <div
                              className={
                                "h-full rounded-full transition-all " +
                                (pct >= 100 ? "bg-amber-600" : "bg-brand-600")
                              }
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        )}
                      </div>
                    )}
                  </li>
                )
              })}
            </ul>
          )}
        </section>

        <section className="space-y-4 rounded-xl border border-zinc-200 bg-white p-4">
          <div className="flex items-center justify-between">
            <h3 className="font-semibold text-zinc-900">
              {editingName ? t("Edit routing rule") : t("New routing instruction")}
            </h3>
            {editingName && (
              <Button className="min-h-11 sm:min-h-0" type="button" variant="ghost" onClick={resetForm}>
                {t("New rule")}
              </Button>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <label className="space-y-1 text-xs text-zinc-600">
              <span>{t("Route to")}</span>
              <select
                aria-label={t("Route to")}
                className={inputCls}
                value={targetType}
                onChange={(e) => {
                  const next = e.target.value as "Window" | "Room" | "Group Master"
                  setTargetType(next)
                  setTargetWindow(next === "Window" ? 2 : 1)
                }}
              >
                <option value="Window">{t("This Stay Window")}</option>
                <option value="Room">{t("Another In-House Room")}</option>
                {groupBooking && <option value="Group Master">{t("Group Master")}</option>}
              </select>
            </label>

            <label className="space-y-1 text-xs text-zinc-600">
              <span>{t("Target window")}</span>
              <select
                aria-label={t("Target window")}
                className={inputCls}
                value={targetWindow}
                onChange={(e) => setTargetWindow(Number(e.target.value))}
              >
                {Array.from({ length: windowCap }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>
                    W{n}
                  </option>
                ))}
              </select>
            </label>

            {targetType === "Room" && (
              <label className="space-y-1 text-xs text-zinc-600">
                <span>{t("Target room")}</span>
                <select
                  aria-label={t("Target room")}
                  className={inputCls}
                  value={targetReservation}
                  onChange={(e) => setTargetReservation(e.target.value)}
                >
                  <option value="">{t("Select in-house room")}</option>
                  {rooms.map((rm) => (
                    <option key={rm.name} value={rm.name}>
                      {rm.room_number ? `${rm.room_number} · ` : ""}
                      {rm.guest_name || rm.name}
                    </option>
                  ))}
                </select>
              </label>
            )}
          </div>

          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-zinc-700">
              <input
                type="checkbox"
                className="size-4 accent-brand-600"
                checked={allCodes}
                onChange={(e) => {
                  setAllCodes(e.target.checked)
                  if (e.target.checked) setLimitType("None")
                }}
              />
              {t("All Transaction Codes")}
            </label>

            {!allCodes && (
              <div className="space-y-2 rounded-lg border border-zinc-200 p-2.5">
                <input
                  className={inputCls}
                  placeholder={t("Filter transaction codes (e.g. 2010 Beverage, Laundry)")}
                  value={codeQuery}
                  onChange={(e) => setCodeQuery(e.target.value)}
                />
                <div className="max-h-44 space-y-1 overflow-y-auto pe-1">
                  {[...new Set([...selectedTypes, ...codes.flatMap((code) => code.charge_type ? [code.charge_type] : [])])].sort().map((chargeType) => (
                    <label key={chargeType} className="flex min-h-11 cursor-pointer items-center gap-2 rounded-md px-2 py-1 text-xs hover:bg-zinc-50">
                      <input
                        type="checkbox"
                        checked={selectedTypes.has(chargeType)}
                        onChange={(event) => {
                          const next = new Set(selectedTypes)
                          if (event.target.checked) next.add(chargeType)
                          else next.delete(chargeType)
                          setSelectedTypes(next)
                        }}
                      />
                      {translate(chargeType)}
                    </label>
                  ))}
                  {filteredCodes.map((tc) => {
                    const checked = selectedCodes.has(tc.code)
                    return (
                      <label
                        key={tc.code}
                        className="flex min-h-11 cursor-pointer items-center justify-between gap-2 rounded-md px-2 py-1 text-xs hover:bg-zinc-50"
                      >
                        <span className="flex items-center gap-2">
                          <input
                            type="checkbox"
                            data-code={tc.code}
                            className="size-4 accent-brand-600"
                            checked={checked}
                            onChange={(e) =>
                              setSelectedCodes((prev) => {
                                const next = new Set(prev)
                                if (e.target.checked) next.add(tc.code)
                                else next.delete(tc.code)
                                return next
                              })
                            }
                          />
                          <span className="font-mono font-semibold text-zinc-700">{tc.code}</span>
                          <span className="text-zinc-700">{translate(tc.description)}</span>
                        </span>
                        {tc.charge_type && (
                          <span className="text-[11px] text-zinc-400">{translate(tc.charge_type)}</span>
                        )}
                      </label>
                    )
                  })}
                </div>
              </div>
            )}
          </div>

          <div className="grid gap-3 sm:grid-cols-2">
            <label className="space-y-1 text-xs text-zinc-600">
              <span>{t("From date")}</span>
              <input
                type="date"
                aria-label={t("From date")}
                className={inputCls}
                value={fromDate}
                onChange={(e) => setFromDate(e.target.value)}
              />
            </label>
            <label className="space-y-1 text-xs text-zinc-600">
              <span>{t("To date")}</span>
              <input
                type="date"
                aria-label={t("To date")}
                className={inputCls}
                value={toDate}
                min={fromDate || undefined}
                onChange={(e) => setToDate(e.target.value)}
              />
            </label>
          </div>

          <div className="space-y-1.5">
            <span className="text-xs text-zinc-600">{t("Active weekdays")}</span>
            <div className="flex flex-wrap gap-1.5">
              {WEEKDAYS.map((d, index) => {
                const on = days[d.key]
                return (
                  <button
                    key={d.key}
                    type="button"
                    aria-pressed={on}
                    aria-label={t(d.label)}
                    onClick={() => setDays((prev) => ({ ...prev, [d.key]: !prev[d.key] }))}
                    className={
                      "min-h-10 min-w-10 rounded-lg border px-2.5 text-xs font-semibold transition-colors " +
                      (on
                        ? "border-brand-600 bg-brand-600 text-white"
                        : "border-zinc-200 bg-white text-zinc-500 hover:border-zinc-300")
                    }
                  >
                    {weekday.format(Date.UTC(2024, 0, 1 + index))}
                  </button>
                )
              })}
            </div>
          </div>

          {!allCodes && (
            <div className="grid gap-3 sm:grid-cols-3">
              <label className="space-y-1 text-xs text-zinc-600">
                <span>{t("Limit type")}</span>
                <select
                  aria-label={t("Limit type")}
                  className={inputCls}
                  value={limitType}
                  onChange={(e) => setLimitType(e.target.value as "None" | "Amount" | "Percent")}
                >
                  <option value="None">{t("No limit")}</option>
                  <option value="Amount">{t("Max amount")}</option>
                  <option value="Percent">{t("Percent")}</option>
                </select>
              </label>

              {limitType !== "None" && (
                <label className="space-y-1 text-xs text-zinc-600">
                  <span>{limitType === "Percent" ? t("Percent (1-100)") : t("Limit amount")}</span>
                  <input
                    type="number"
                    min="0"
                    max={limitType === "Percent" ? 100 : undefined}
                    step={limitType === "Percent" ? "0.01" : moneyStep(currency, money?.decimals ?? undefined)}
                    inputMode={limitType === "Amount" && moneyDigits(currency, money?.decimals ?? undefined) === 0 ? "numeric" : "decimal"}
                    aria-label={t("Limit value")}
                    className={inputCls}
                    value={limitValue}
                    onChange={(e) => setLimitValue(e.target.value)}
                  />
                </label>
              )}

              {limitType === "Amount" && (
                <label className="flex items-end gap-2 pb-2 text-xs text-zinc-700">
                  <input
                    type="checkbox"
                    className="size-4 accent-brand-600"
                    checked={perDay}
                    onChange={(e) => setPerDay(e.target.checked)}
                  />
                  {t("Per Day")}
                </label>
              )}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button className="min-h-11 sm:min-h-0" type="button" variant="outline" onClick={onClose}>
              {t("Close")}
            </Button>
            <Button className="min-h-11 sm:min-h-0"
              type="button"
              data-testid="save-routing-btn"
              disabled={busy || !canSave}
              onClick={() => void saveRule()}
            >
              {busy ? t("Saving…") : editingName ? t("Update rule") : t("Save routing rule")}
            </Button>
          </div>
        </section>
      </div>
    </Sheet>
  )
}
