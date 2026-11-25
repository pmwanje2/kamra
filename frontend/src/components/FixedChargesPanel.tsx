/*  P29: Recurring Fixed Charges panel inside ReservationDetail.
    Displays Daily / Weekly / Stay Day N / On Arrival / On Departure charges
    routed to Folio Windows W1..W8 with 0/2/3dp currency formatting and 44px
    mobile touch targets. All writes use governed kamra.fixed_charges endpoints. */

import { useCallback, useEffect, useState } from "react"
import { Pause, Play, Plus, Repeat, Trash2 } from "lucide-react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"
import { cur, fmtAmount, moneyDigits } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"
import { cn } from "../lib/utils"

const FREQUENCIES = ["Daily", "Weekly", "Stay Day N", "On Arrival", "On Departure"] as const
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const
const WINDOWS = [1, 2, 3, 4, 5, 6, 7, 8] as const

function weekdayLabel(day: string): string {
  const labels: Record<string, string> = {
    Monday: translate("Monday"),
    Tuesday: translate("Tuesday"),
    Wednesday: translate("Wednesday"),
    Thursday: translate("Thursday"),
    Friday: translate("Friday"),
    Saturday: translate("Saturday"),
    Sunday: translate("Sunday"),
  }
  return labels[day] ?? day
}

interface FixedChargeRow {
  name: string
  transaction_code: string
  charge_type?: string
  description: string
  qty: number
  unit_price: number
  frequency: string
  stay_day_n?: number
  day_of_week?: string
  start_date: string
  end_date: string
  window_no: number
  supplement?: string
  active: number
  last_posted_business_date?: string | null
  post_count: number
}

interface TxnCodeOption {
  code: string
  description: string
  charge_type?: string
}

interface ListResponse {
  reservation: string
  property: string
  currency?: string
  check_in_date: string
  check_out_date: string
  rows: FixedChargeRow[]
  transaction_codes: TxnCodeOption[]
}

export default function FixedChargesPanel({
  reservation,
  status,
}: {
  reservation: string
  status?: string
}) {
  useUILanguage()
  const canEdit = !["Cancelled", "Checked Out", "No Show"].includes(status ?? "")
  const [data, setData] = useState<ListResponse | null>(null)
  const [rows, setRows] = useState<FixedChargeRow[]>([])
  const [adding, setAdding] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const [code, setCode] = useState("3300")
  const [desc, setDesc] = useState("")
  const [freq, setFreq] = useState<string>("Daily")
  const [stayDayN, setStayDayN] = useState(1)
  const [dow, setDow] = useState("")
  const [qty, setQty] = useState(1)
  const [unitPrice, setUnitPrice] = useState("")
  const [windowNo, setWindowNo] = useState(1)
  const [startDate, setStartDate] = useState("")
  const [endDate, setEndDate] = useState("")
  const [supplement, setSupplement] = useState("")

  const load = useCallback(() => {
    call<ListResponse | null>("kamra.fixed_charges.list_fixed_charges", { reservation })
      .then((res) => {
        if (!res) return
        setData(res)
        setRows(res.rows ?? [])
        setStartDate((prev) => prev || res.check_in_date || "")
        setEndDate((prev) => prev || res.check_out_date || "")
        if (res.transaction_codes?.length) {
          setCode((prev) =>
            res.transaction_codes.some((c) => c.code === prev) ? prev : res.transaction_codes[0].code,
          )
        }
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [reservation])

  useEffect(load, [load])

  const currency = data?.currency || ""
  const digits = moneyDigits(currency || undefined)
  const step = digits > 0 ? Number((10 ** -digits).toFixed(digits)) : 1
  const sym = cur()
  const activeCount = rows.filter((r) => Number(r.active)).length

  async function submitNew() {
    setBusy(true)
    setError(null)
    try {
      const selectedTc = data?.transaction_codes?.find((c) => c.code === code)
      const res = await call<{ row: FixedChargeRow; rows: FixedChargeRow[] }>(
        "kamra.fixed_charges.save_fixed_charge",
        {
          reservation,
          row_data: {
            transaction_code: code,
            charge_type: selectedTc?.charge_type || "Misc",
            description: desc.trim() || selectedTc?.description || "",
            qty: Number(qty || 1),
            unit_price: Number(unitPrice || 0),
            frequency: freq,
            stay_day_n: freq === "Stay Day N" ? Number(stayDayN || 1) : 1,
            day_of_week: freq === "Weekly" ? dow : "",
            start_date: startDate || data?.check_in_date,
            end_date: endDate || data?.check_out_date,
            window_no: Number(windowNo || 1),
            supplement: supplement.trim(),
            active: 1,
          },
        },
      )
      setRows(res.rows ?? [])
      setDesc("")
      setUnitPrice("")
      setSupplement("")
      setAdding(false)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function toggleActive(row: FixedChargeRow) {
    setBusy(true)
    setError(null)
    try {
      const res = await call<{ rows: FixedChargeRow[] }>("kamra.fixed_charges.save_fixed_charge", {
        reservation,
        row_data: { ...row, active: Number(row.active) ? 0 : 1 },
      })
      setRows(res.rows ?? [])
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function removeRow(row: FixedChargeRow) {
    setBusy(true)
    setError(null)
    try {
      const res = await call<{ rows: FixedChargeRow[] }>("kamra.fixed_charges.delete_fixed_charge", {
        reservation,
        row_name: row.name,
      })
      setRows(res.rows ?? [])
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      aria-label={translate("Fixed charges")}
      className="rounded-xl border border-zinc-200 bg-white p-4"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-zinc-700">
          <Repeat className="size-4 text-zinc-400" aria-hidden />
          <span>{translate("Fixed charges")}</span>
          {activeCount > 0 && (
            <Badge tone="sky">{translate("{n} active", { n: activeCount })}</Badge>
          )}
        </div>
        {canEdit && (
          <Button
            type="button"
            variant="outline"
            className="min-h-11 sm:min-h-8 text-xs"
            onClick={() => setAdding((v) => !v)}
          >
            <Plus className="size-3.5 me-1" aria-hidden />
            {adding ? translate("Close") : translate("Add fixed charge")}
          </Button>
        )}
      </div>

      {rows.length === 0 && !adding && (
        <p className="text-sm text-zinc-400">{translate("No recurring fixed charges on this stay.")}</p>
      )}

      {rows.length > 0 && (
        <ul className="divide-y divide-zinc-100">
          {rows.map((r) => {
            const isActive = Boolean(Number(r.active))
            const lineTotal = Number(r.qty || 1) * Number(r.unit_price || 0)
            return (
              <li
                key={r.name}
                className={cn(
                  "flex flex-col gap-2 py-2.5 sm:flex-row sm:items-center sm:justify-between",
                  !isActive && "opacity-60",
                )}
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] font-medium text-zinc-700">
                      {r.transaction_code}
                    </span>
                    <span className="text-sm font-medium text-zinc-900">{r.description}</span>
                    <Badge tone={isActive ? "green" : "zinc"}>
                      {translate(r.frequency)}
                      {r.frequency === "Stay Day N" && r.stay_day_n ? ` (${r.stay_day_n})` : ""}
                      {r.frequency === "Weekly" && r.day_of_week ? ` · ${weekdayLabel(r.day_of_week)}` : ""}
                    </Badge>
                    <Badge tone="sky">W{r.window_no || 1}</Badge>
                  </div>
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs text-zinc-500">
                    <span className="tabular-nums">
                      {r.start_date} → {r.end_date}
                    </span>
                    <span className="tabular-nums font-medium text-zinc-700">
                      {r.qty} × {sym}{fmtAmount(r.unit_price, currency || undefined)} = {sym}{fmtAmount(lineTotal, currency || undefined)}
                    </span>
                    <span>
                      {translate("{n} posted", { n: Number(r.post_count || 0) })}
                    </span>
                  </div>
                </div>

                {canEdit && (
                  <div className="flex shrink-0 items-center gap-1.5">
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => toggleActive(r)}
                      aria-label={isActive ? translate("Pause") : translate("Resume")}
                      className="inline-flex min-h-11 sm:min-h-8 items-center gap-1 rounded-md border border-zinc-200 px-2.5 py-1 text-xs text-zinc-600 hover:bg-zinc-50"
                    >
                      {isActive ? (
                        <>
                          <Pause className="size-3" aria-hidden />
                          {translate("Pause")}
                        </>
                      ) : (
                        <>
                          <Play className="size-3" aria-hidden />
                          {translate("Resume")}
                        </>
                      )}
                    </button>
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => removeRow(r)}
                      aria-label={translate("Remove")}
                      className="inline-flex min-h-11 sm:min-h-8 items-center gap-1 rounded-md border border-zinc-200 px-2.5 py-1 text-xs text-rose-600 hover:bg-rose-50"
                    >
                      <Trash2 className="size-3" aria-hidden />
                      {Number(r.post_count || 0) > 0 ? translate("Stop") : translate("Delete")}
                    </button>
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {adding && canEdit && (
        <div className="mt-3 rounded-lg border border-zinc-200 bg-zinc-50 p-3 space-y-3">
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Transaction code")}</span>
              <select
                name="fc_transaction_code"
                value={code}
                onChange={(e) => {
                  const next = e.target.value
                  setCode(next)
                  const found = data?.transaction_codes?.find((c) => c.code === next)
                  if (found && !desc.trim()) setDesc(found.description)
                }}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              >
                {(data?.transaction_codes ?? []).map((tc) => (
                  <option key={tc.code} value={tc.code}>
                    {tc.code} · {tc.description}
                  </option>
                ))}
              </select>
            </label>
            <label className="block sm:col-span-2">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Description")}</span>
              <input
                name="fc_description"
                value={desc}
                onChange={(e) => setDesc(e.target.value)}
                placeholder={translate("e.g. Daily Valet Parking, Resort Fee")}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              />
            </label>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-4">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Frequency")}</span>
              <select
                name="fc_frequency"
                value={freq}
                onChange={(e) => setFreq(e.target.value)}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              >
                {FREQUENCIES.map((f) => (
                  <option key={f} value={f}>{translate(f)}</option>
                ))}
              </select>
            </label>
            {freq === "Stay Day N" && (
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Stay day")}</span>
                <input
                  type="number"
                  min={1}
                  name="fc_stay_day_n"
                  value={stayDayN}
                  onChange={(e) => setStayDayN(Number(e.target.value || 1))}
                  className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums text-zinc-900"
                />
              </label>
            )}
            {freq === "Weekly" && (
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Day of week")}</span>
                <select
                  name="fc_day_of_week"
                  value={dow}
                  onChange={(e) => setDow(e.target.value)}
                  className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                >
                  <option value="">{translate("Every 7 days from start")}</option>
                  {WEEKDAYS.map((w) => (
                    <option key={w} value={w}>{weekdayLabel(w)}</option>
                  ))}
                </select>
              </label>
            )}
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Quantity")}</span>
              <input
                type="number"
                min={0.01}
                step="any"
                name="fc_qty"
                value={qty}
                onChange={(e) => setQty(Number(e.target.value || 1))}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums text-zinc-900"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Unit price")}</span>
              <input
                type="number"
                min={0}
                step={step}
                name="fc_unit_price"
                value={unitPrice}
                onChange={(e) => setUnitPrice(e.target.value)}
                placeholder="0"
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums text-zinc-900"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Window")}</span>
              <select
                name="fc_window_no"
                value={windowNo}
                onChange={(e) => setWindowNo(Number(e.target.value || 1))}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              >
                {WINDOWS.map((w) => (
                  <option key={w} value={w}>W{w}</option>
                ))}
              </select>
            </label>
          </div>

          <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Start date")}</span>
              <input
                type="date"
                name="fc_start_date"
                value={startDate}
                onChange={(e) => setStartDate(e.target.value)}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("End date")}</span>
              <input
                type="date"
                name="fc_end_date"
                value={endDate}
                onChange={(e) => setEndDate(e.target.value)}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              />
            </label>
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Supplement")}</span>
              <input
                name="fc_supplement"
                value={supplement}
                onChange={(e) => setSupplement(e.target.value)}
                placeholder={translate("Optional reference")}
                className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
              />
            </label>
          </div>

          <div className="flex flex-wrap justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              className="min-h-11 sm:min-h-9"
              onClick={() => setAdding(false)}
              disabled={busy}
            >
              {translate("Cancel")}
            </Button>
            <Button
              type="button"
              className="min-h-11 sm:min-h-9"
              onClick={submitNew}
              disabled={busy || !desc.trim()}
            >
              {busy ? translate("Saving…") : translate("Save charge")}
            </Button>
          </div>
        </div>
      )}

      {error && <p className="mt-2 text-sm text-rose-700">{translate(error)}</p>}
    </section>
  )
}
