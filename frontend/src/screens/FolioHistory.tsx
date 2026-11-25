import { currencyText, formatFxRate } from "../components/ContractTariff"
import { formatDate } from "../lib/utils"
import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Badge } from "../components/ui/badge"
import { fmtAmount, fmtMoney, moneyLocale, useLocale, type MoneyContext } from "../lib/money"
import { windowBadge } from "../lib/folioLabel"
import { t as translate, useT, useUILanguage } from "../lib/i18n"

type FolioRow = {
  name: string
  money?: MoneyContext
  guest_name: string
  reservation: string
  venue_booking?: string | null
  status: string
  folio_type?: string
  window_no?: number | null
  payee_type?: string | null
  payee_company?: string | null
  payee_agent?: string | null
  bill_to_name?: string | null
  invoice_number?: string
  sales_invoice?: string | null
  grand_total: number
  balance: number
  closed_on?: string
  fx_payments?: {
    currency: string
    fx_amount: number
    exchange_rate: number
    exchange_rate_date?: string
  }[]
  reprints: number
}

function payeeSummary(r: FolioRow, tr: (s: string) => string): string | null {
  if (r.payee_type === "Agent" || r.payee_type === "Travel Agent") return r.payee_agent || tr("Travel Agent")
  if (r.bill_to_name) return r.bill_to_name
  if (r.payee_company) return r.payee_company
  if (r.payee_type && r.payee_type !== "Guest") return tr(r.payee_type)
  return null
}

export default function FolioHistory() {
  useUILanguage()
  const { t } = useT()
  const loc = useLocale()
  const [q, setQ] = useState("")
  const [status, setStatus] = useState("")
  const [fromDate, setFromDate] = useState("") // closed on, "" = any day
  const [toDate, setToDate] = useState("")
  const [rows, setRows] = useState<FolioRow[]>([])
  const [selected, setSelected] = useState<string[]>([])
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const showFiscalBadge = Boolean(loc.integrations?.tax_return)
  const amount = (n: unknown, r: FolioRow, withSymbol = true) => (
    <span dir="ltr" className="inline-block tabular-nums">{withSymbol ? fmtMoney(n, r.money ?? loc) : fmtAmount(n, r.money?.currency ?? loc.currency, r.money?.locale ?? loc.locale)}</span>
  )

  const load = useCallback(async () => {
    try {
      const out = await call<{ folios: FolioRow[] }>(
        "kamra.ledger.folio_history",
        {
          property: getCurrentProperty(),
          query: q || undefined,
          status: status || undefined,
          from_date: fromDate || undefined,
          to_date: (fromDate && toDate) || undefined,
        },
      )
      setRows(out.folios ?? [])
    } catch (e) {
      setErr(serverError(e))
    }
  }, [q, status, fromDate, toDate])

  useEffect(() => {
    void load()
  }, [load])

  const toggle = (name: string) =>
    setSelected((s) =>
      s.includes(name) ? s.filter((x) => x !== name) : [...s, name],
    )

  const batchPrint = async () => {
    setBusy(true)
    try {
      await call("kamra.ledger.batch_print_folios", {
        folios: JSON.stringify(selected),
      })
      await load()
      setSelected([])
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="w-full space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-zinc-400">
            {translate("Billing")}
          </p>
          <h1 className="text-2xl font-semibold">{translate("Folio History")}</h1>
        </div>
        <div className="flex flex-wrap gap-2">
          <input
            className="min-h-11 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm sm:min-h-9"
            placeholder={translate("Search guest / invoice")}
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <select
            className="min-h-11 rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm sm:min-h-9"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="">{translate("All")}</option>
            <option value="Open">{translate("Open")}</option>
            <option value="Closed">{translate("Closed")}</option>
          </select>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            {translate("Closed from")}
            <input
              type="date"
              className="min-h-11 rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm sm:min-h-9"
              value={fromDate}
              onChange={(e) => setFromDate(e.target.value)}
            />
          </label>
          <label className="flex items-center gap-1 text-xs text-zinc-500">
            {translate("To")}
            <input
              type="date"
              className="min-h-11 rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm sm:min-h-9"
              value={toDate}
              min={fromDate || undefined}
              disabled={!fromDate}
              onChange={(e) => setToDate(e.target.value)}
            />
          </label>
          <Button
            variant="outline"
            className="min-h-11 sm:min-h-9"
            disabled={!selected.length || busy}
            onClick={() => void batchPrint()}
          >
            {translate("Batch print (")}
            {selected.length})
          </Button>
        </div>
      </div>
      {err ? <p className="text-sm text-rose-600">{translate(err)}</p> : null}

      {/* Mobile card list (390px viewport) */}
      <div data-testid="folio-history-cards" className="grid gap-3 md:hidden">
        {rows.map((r) => {
          const payee = payeeSummary(r, t)
          const fiscalized = showFiscalBadge && Boolean(r.sales_invoice || r.invoice_number)
          return (
            <div
              key={r.name}
              className="rounded-xl border border-zinc-200 bg-white p-3.5 text-sm shadow-xs"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="flex min-w-0 items-start gap-2.5">
                  <label className="grid min-h-11 min-w-11 place-items-center">
                    <input
                      type="checkbox"
                      aria-label={t("Select {name}", { name: r.guest_name || r.name })}
                      className="size-4 accent-brand-600"
                      checked={selected.includes(r.name)}
                      onChange={() => toggle(r.name)}
                    />
                  </label>
                  <div className="min-w-0">
                    <Link
                      to={`/billing/${encodeURIComponent(r.name)}`}
                      className="flex min-h-11 items-center truncate font-medium text-brand-700 hover:underline"
                    >
                      {r.guest_name || r.name}
                    </Link>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1 text-xs text-zinc-500">
                      <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono text-[11px] font-semibold text-zinc-700">
                        {windowBadge({ window_no: r.window_no, folio_type: r.folio_type || "Guest" })}
                      </span>
                      {payee && <span className="truncate">{payee}</span>}
                      {(r.reservation || r.venue_booking) && <span>{payee && "· "}{r.reservation || r.venue_booking}</span>}
                    </div>
                  </div>
                </div>
                <div className="flex shrink-0 flex-col items-end gap-1">
                  <Badge tone={r.status === "Open" ? "green" : "zinc"}>
                    {translate(r.status)}
                  </Badge>
                  {fiscalized && (
                    <Badge tone="brand">{t("Tax return")}</Badge>
                  )}
                </div>
              </div>
              <div className="mt-3 grid grid-cols-2 gap-2 border-t border-zinc-100 pt-2.5 text-xs">
                <div>
                  <span className="block text-zinc-400">{translate("Invoice")}</span>
                  <span className="font-mono text-zinc-700">{r.sales_invoice || r.invoice_number || "-"}</span>
                </div>
                <div className="flex flex-wrap justify-end gap-x-3 gap-y-1 text-end">
                  <div className="font-medium text-zinc-900">
                    <span className="block text-zinc-400">{t("Total")}</span>
                    {amount(r.grand_total, r)}
                  </div>
                  <div className="text-zinc-500">
                    <span className="block text-zinc-400">{t("Balance")}</span>
                    {amount(r.balance, r)}
                  </div>
                </div>
              </div>
            </div>
          )
        })}
      </div>

      {/* Desktop / tablet table */}
      <div className="hidden overflow-x-auto rounded-xl border border-zinc-200 bg-white md:block">
        <table className="w-full text-start text-sm">
          <thead className="border-b text-xs uppercase text-zinc-400">
            <tr>
              <th className="px-3 py-2" />
              <th className="px-3 py-2">{translate("Guest")}</th>
              <th className="px-3 py-2">{translate("Window")}</th>
              <th className="px-3 py-2">{translate("Invoice")}</th>
              <th className="px-3 py-2">{translate("Status")}</th>
              <th className="px-3 py-2 text-end">{translate("Total")}</th>
              <th className="px-3 py-2 text-end">{translate("Balance")}</th>
              <th className="px-3 py-2 text-end">{translate("Reprints")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const payee = payeeSummary(r, t)
              const fiscalized = showFiscalBadge && Boolean(r.sales_invoice || r.invoice_number)
              return (
                <tr key={r.name} className="border-t border-zinc-50">
                  <td className="px-3 py-2">
                    <input
                      type="checkbox"
                      checked={selected.includes(r.name)}
                      onChange={() => toggle(r.name)}
                    />
                  </td>
                  <td className="px-3 py-2">
                    <Link
                      to={`/billing/${encodeURIComponent(r.name)}`}
                      className="font-medium text-brand-700 hover:underline"
                    >
                      {r.guest_name || r.name}
                    </Link>
                    <div className="text-xs text-zinc-400">{r.reservation || r.venue_booking}</div>
                  </td>
                  <td className="px-3 py-2 text-xs">
                    <span className="rounded bg-zinc-100 px-1.5 py-0.5 font-mono font-semibold text-zinc-700">
                      {windowBadge({ window_no: r.window_no, folio_type: r.folio_type || "Guest" })}
                    </span>
                    {payee && <div className="mt-0.5 text-zinc-500">{payee}</div>}
                  </td>
                  <td className="px-3 py-2 font-mono text-xs">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span>{r.sales_invoice || r.invoice_number || "-"}</span>
                      {fiscalized && <Badge tone="brand">{t("Tax return")}</Badge>}
                    </div>
                    {r.fx_payments?.map((p, i) => (
                      <div className="tabular-nums" key={i}>
                        {currencyText(p.currency, p.fx_amount, (r.money?.locale || moneyLocale()))} @{" "}
                        {formatFxRate(p.exchange_rate, (r.money?.locale || moneyLocale()))}
                        {p.exchange_rate_date && ` · ${formatDate(p.exchange_rate_date)}`}
                      </div>
                    ))}
                  </td>
                  <td className="px-3 py-2">
                    <Badge tone={r.status === "Open" ? "green" : "zinc"}>
                      {translate(r.status)}
                    </Badge>
                  </td>
                  <td className="px-3 py-2 text-end tabular-nums">{amount(r.grand_total, r, false)}</td>
                  <td className="px-3 py-2 text-end tabular-nums">{amount(r.balance, r, false)}</td>
                  <td className="px-3 py-2 text-end tabular-nums">{r.reprints}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
