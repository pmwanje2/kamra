/*  P13: Itemised Package Elements & Allowances editor embedded inside the
    Holiday Package drawer. Lets Revenue Managers configure component splits
    (Meal, Spa, Transport, F&B Allowance, Utility/Amenity), toggle Inclusive
    vs Separate Add-on folio display, and inspect live Net Room Revenue per
    occupancy tier at the property's ISO 4217 currency precision (0/2/3dp). */

import { useCallback, useEffect, useMemo, useState } from "react"
import { Layers, Plus, Save, Trash2 } from "lucide-react"
import { call } from "../lib/api"
import { serverError, type Row } from "../lib/resource"
import { Button } from "./ui/button"
import { Badge } from "./ui/badge"
import { cur, fmtAmount, moneyDigits } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"
import { cn } from "../lib/utils"

const ELEMENT_TYPES = ["Meal", "Spa", "Transport", "F&B Allowance", "Utility/Amenity"] as const
const ALLOWANCE_BASES = [
  "Per Adult Night",
  "Per Child Night",
  "Per Person Night",
  "Per Room Night",
  "Fixed Per Stay",
] as const
const POSTING_RHYTHMS = ["Every Night", "First Night", "Last Night"] as const
const DISPLAY_MODES = ["Inclusive", "Separate Add-on"] as const

interface PackageElementRow {
  name?: string
  element_name: string
  element_type: string
  transaction_code: string
  charge_type?: string
  allowance_basis: string
  unit_allowance: number
  posting_rhythm: string
  included_in_rate: number
  folio_display_mode?: string
  tax_inclusive?: number
  gst_rate?: number
}

interface TxnCodeOption {
  code: string
  description: string
  charge_type?: string
}

interface PackageElementsResponse {
  holiday_package: string
  package_name: string
  currency?: string
  package_nights: number
  folio_display_mode: string
  price_1_pax: number
  price_2_pax: number
  price_3_pax: number
  price_4_pax: number
  elements: PackageElementRow[]
  transaction_codes: TxnCodeOption[]
}

function perNightDeduction(el: PackageElementRow, adults: number, nights: number): number {
  if (!Number(el.included_in_rate)) return 0
  const unit = Number(el.unit_allowance || 0)
  let mult = 1
  if (el.allowance_basis === "Per Adult Night" || el.allowance_basis === "Per Person Night") {
    mult = Math.max(1, adults)
  } else if (el.allowance_basis === "Per Child Night") {
    mult = 0
  }
  const amount = unit * mult
  if (el.allowance_basis === "Fixed Per Stay" || el.posting_rhythm === "First Night" || el.posting_rhythm === "Last Night") {
    return amount / Math.max(1, nights)
  }
  return amount
}

export default function PackageElementsEditor({
  row,
  reload,
}: {
  row: Row
  reload: () => void
}) {
  useUILanguage()
  const pkgName = String(row.name ?? "")
  const [data, setData] = useState<PackageElementsResponse | null>(null)
  const [elements, setElements] = useState<PackageElementRow[]>([])
  const [displayMode, setDisplayMode] = useState<string>("Inclusive")
  const [busy, setBusy] = useState(false)
  const [savedMsg, setSavedMsg] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    if (!pkgName) return
    call<PackageElementsResponse | null>("kamra.packages.list_package_elements", {
      holiday_package: pkgName,
    })
      .then((res) => {
        if (!res) return
        setData(res)
        setElements(res.elements ?? [])
        setDisplayMode(res.folio_display_mode || "Inclusive")
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [pkgName])

  useEffect(load, [load])

  const currency = data?.currency || ""
  const digits = moneyDigits(currency || undefined)
  const step = digits > 0 ? Number((10 ** -digits).toFixed(digits)) : 1
  const nights = Math.max(1, Number(data?.package_nights ?? row.package_nights ?? 1))

  const tiers = useMemo(() => {
    const p2 = Number(data?.price_2_pax ?? row.price_2_pax ?? 0)
    const p1 = Number(data?.price_1_pax || p2)
    const p3 = Number(data?.price_3_pax || 0)
    const p4 = Number(data?.price_4_pax || 0)
    const list: { pax: number; total: number }[] = [
      { pax: 1, total: p1 },
      { pax: 2, total: p2 },
    ]
    if (p3 > 0) list.push({ pax: 3, total: p3 })
    if (p4 > 0) list.push({ pax: 4, total: p4 })
    return list.map(({ pax, total }) => {
      const nightly = total / nights
      const deducted = elements.reduce((acc, el) => acc + perNightDeduction(el, pax, nights), 0)
      const roomNet = Math.max(0, nightly - deducted)
      return { pax, nightly, deducted, roomNet }
    })
  }, [data, row, nights, elements])

  function addRow() {
    const defaultCode = data?.transaction_codes?.[0]?.code ?? "1100"
    const defaultCt = data?.transaction_codes?.[0]?.charge_type ?? "Meal Plan"
    setElements((prev) => [
      ...prev,
      {
        element_name: "",
        element_type: "Meal",
        transaction_code: defaultCode,
        charge_type: defaultCt,
        allowance_basis: "Per Adult Night",
        unit_allowance: 0,
        posting_rhythm: "Every Night",
        included_in_rate: displayMode === "Inclusive" ? 1 : 0,
        folio_display_mode: displayMode,
      },
    ])
    setSavedMsg(null)
  }

  function updateRow(idx: number, patch: Partial<PackageElementRow>) {
    setElements((prev) => prev.map((el, i) => (i === idx ? { ...el, ...patch } : el)))
    setSavedMsg(null)
  }

  function removeRow(idx: number) {
    setElements((prev) => prev.filter((_, i) => i !== idx))
    setSavedMsg(null)
  }

  async function save() {
    setBusy(true)
    setError(null)
    setSavedMsg(null)
    try {
      const res = await call<PackageElementsResponse>("kamra.packages.save_package_elements", {
        holiday_package: pkgName,
        folio_display_mode: displayMode,
        elements,
      })
      if (res?.elements) setElements(res.elements)
      if (res?.folio_display_mode) setDisplayMode(res.folio_display_mode)
      setSavedMsg(translate("Package elements saved."))
      reload()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const sym = cur()

  return (
    <section
      aria-label={translate("Package elements")}
      className="rounded-xl border border-zinc-200 bg-white p-4 space-y-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Layers className="size-4 text-zinc-500" aria-hidden />
          <h3 className="text-sm font-semibold text-zinc-800">{translate("Package elements")}</h3>
          <Badge tone="zinc">{elements.length}</Badge>
        </div>
        <div className="flex flex-wrap items-center gap-1.5" role="group" aria-label={translate("Folio display")}>
          {DISPLAY_MODES.map((mode) => (
            <button
              key={mode}
              type="button"
              onClick={() => {
                setDisplayMode(mode)
                setSavedMsg(null)
              }}
              className={cn(
                "min-h-11 sm:min-h-8 rounded-lg border px-3 py-1 text-xs font-medium transition-colors",
                displayMode === mode
                  ? "border-brand-600 bg-brand-50 text-brand-700"
                  : "border-zinc-200 bg-white text-zinc-600 hover:bg-zinc-50",
              )}
            >
              {translate(mode)}
            </button>
          ))}
        </div>
      </div>

      {/* Live Net Room Revenue preview per occupancy tier */}
      {tiers.length > 0 && (
        <div className="rounded-lg border border-zinc-200 bg-zinc-50 p-3">
          <p className="mb-2 text-xs font-medium text-zinc-600">
            {translate("Net room revenue preview (per night)")}
          </p>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4">
            {tiers.map((tier) => (
              <div key={tier.pax} className="rounded-md border border-zinc-200 bg-white px-2.5 py-2">
                <div className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">
                  {translate("{n} pax", { n: tier.pax })}
                </div>
                <div className="mt-0.5 text-sm font-semibold tabular-nums text-zinc-900">
                  {sym}{fmtAmount(tier.roomNet, currency || undefined)}
                </div>
                <div className="text-[11px] tabular-nums text-zinc-500">
                  {translate("Allowances")}: {sym}{fmtAmount(tier.deducted, currency || undefined)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Element rows */}
      {elements.length === 0 ? (
        <p className="text-sm text-zinc-500">
          {translate("No package elements yet. Add elements to split room revenue from meal, spa, or transport allowances.")}
        </p>
      ) : (
        <div className="space-y-3">
          {elements.map((el, idx) => (
            <div
              key={el.name ?? `new-${idx}`}
              className="rounded-lg border border-zinc-200 bg-zinc-50/60 p-3 space-y-2.5"
            >
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Element name")}</span>
                  <input
                    value={el.element_name}
                    onChange={(e) => updateRow(idx, { element_name: e.target.value })}
                    placeholder={translate("e.g. Breakfast, Spa Credit")}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Type")}</span>
                  <select
                    value={el.element_type}
                    onChange={(e) => updateRow(idx, { element_type: e.target.value })}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                  >
                    {ELEMENT_TYPES.map((et) => (
                      <option key={et} value={et}>{translate(et)}</option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Transaction code")}</span>
                  <select
                    value={el.transaction_code}
                    onChange={(e) => {
                      const code = e.target.value
                      const found = data?.transaction_codes?.find((c) => c.code === code)
                      updateRow(idx, { transaction_code: code, charge_type: found?.charge_type || el.charge_type })
                    }}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                  >
                    {(data?.transaction_codes ?? []).map((tc) => (
                      <option key={tc.code} value={tc.code}>
                        {tc.code} · {tc.description}
                      </option>
                    ))}
                    {el.transaction_code &&
                      !(data?.transaction_codes ?? []).some((tc) => tc.code === el.transaction_code) && (
                        <option value={el.transaction_code}>{el.transaction_code}</option>
                      )}
                  </select>
                </label>
              </div>

              <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Allowance basis")}</span>
                  <select
                    value={el.allowance_basis}
                    onChange={(e) => updateRow(idx, { allowance_basis: e.target.value })}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                  >
                    {ALLOWANCE_BASES.map((b) => (
                      <option key={b} value={b}>{translate(b)}</option>
                    ))}
                  </select>
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Unit allowance")}</span>
                  <input
                    type="number"
                    min={0}
                    step={step}
                    value={el.unit_allowance}
                    onChange={(e) => updateRow(idx, { unit_allowance: Number(e.target.value || 0) })}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm tabular-nums text-zinc-900"
                  />
                </label>
                <label className="block">
                  <span className="mb-1 block text-xs font-medium text-zinc-600">{translate("Posting rhythm")}</span>
                  <select
                    value={el.posting_rhythm}
                    onChange={(e) => updateRow(idx, { posting_rhythm: e.target.value })}
                    className="w-full min-h-11 sm:min-h-9 rounded-lg border border-zinc-300 bg-white px-2.5 py-1.5 text-sm text-zinc-900"
                  >
                    {POSTING_RHYTHMS.map((r) => (
                      <option key={r} value={r}>{translate(r)}</option>
                    ))}
                  </select>
                </label>
              </div>

              <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
                <label className="inline-flex min-h-11 sm:min-h-8 items-center gap-2 text-xs text-zinc-700">
                  <input
                    type="checkbox"
                    checked={Boolean(el.included_in_rate)}
                    onChange={(e) => updateRow(idx, { included_in_rate: e.target.checked ? 1 : 0 })}
                    className="size-4 rounded border-zinc-300"
                  />
                  {translate("Included in package rate")}
                </label>
                <Button
                  type="button"
                  variant="ghost"
                  className="min-h-11 sm:min-h-8 text-xs text-rose-600"
                  onClick={() => removeRow(idx)}
                >
                  <Trash2 className="size-3.5 me-1" aria-hidden />
                  {translate("Remove")}
                </Button>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
        <Button type="button" variant="outline" className="min-h-11 sm:min-h-9" onClick={addRow} disabled={busy}>
          <Plus className="size-4 me-1" aria-hidden />
          {translate("Add element")}
        </Button>
        <Button type="button" className="min-h-11 sm:min-h-9" onClick={save} disabled={busy}>
          <Save className="size-4 me-1" aria-hidden />
          {busy ? translate("Saving…") : translate("Save elements")}
        </Button>
      </div>

      {savedMsg && <p className="text-xs font-medium text-emerald-700">{savedMsg}</p>}
      {error && <p className="text-xs text-rose-700">{translate(error)}</p>}
    </section>
  )
}
