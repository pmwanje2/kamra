import { useCallback, useEffect, useState } from "react"
import { Download, FileSpreadsheet } from "lucide-react"

import { call, getCurrentProperty } from "../lib/api"
import { accountingImportLabel } from "../lib/brand"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader } from "../components/ui/card"
import { fmtMoney, moneyDigits, roundMoney } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"
import { downloadCsv } from "../lib/csv"

type Row = Record<string, unknown>
type Line = Record<string, unknown>

interface ExportData {
  rows: Row[]
  components: string[]
  tax_label: string
  tax_id_label: string
  currency: string
  totals: { invoices: number; taxable: number; total_tax: number; grand_total: number }
}

/** A column in a tool's import file: label shown, and how to pull the value. */
type Col = { header: string; pick: (r: Row, d: ExportData) => string }

const money = (v: unknown, currency?: string) => {
  const digits = moneyDigits(currency)
  return roundMoney(Number(v ?? 0), currency, digits).toFixed(digits)
}
const compHeader = (c: string) => c.toUpperCase()

/** Item-level formats (generic, Zoho) get one row per invoice line group -
 *  each service code (SAC/HSN), GL code and tax rate on the invoice - so a bill with
 *  a room, a dinner and a laundry bag exports three codes, not the room's
 *  code three times. Voucher-level formats (Tally, accounting import) keep one row per
 *  invoice. */
const ITEM_LEVEL = new Set(["generic", "zoho"])

function rowsFor(format: string, d: ExportData): Row[] {
  if (!ITEM_LEVEL.has(format)) return d.rows
  return d.rows.flatMap((r) => {
    const lines = (r.lines as Line[] | undefined) ?? []
    return lines.length
      ? lines.map((l) => {
          const out: Row = {
            ...r,
            item: String(l.description ?? ""),
            service_code: String(l.service_code ?? ""),
            gl_code: String(l.gl_code ?? ""),
            rate: Number(l.rate ?? 0),
            taxable: Number(l.taxable ?? 0),
            total_tax: Number(l.total_tax ?? 0),
            line_total: Number(l.total ?? 0),
          }
          for (const c of d.components) out[c] = Number(l[c] ?? 0)
          return out
        })
      : [{ ...r, item: "", rate: "", line_total: r.grand_total }]
  })
}

/** Per-tool column layouts over the same rows. Tax columns come from the
 *  localization pack (CGST/SGST for India, TAX elsewhere). */
function columnsFor(format: string, d: ExportData): Col[] {
  const taxCols: Col[] = d.components.map((c) => ({
    header: compHeader(c),
    pick: (r) => money(r[c], d.currency),
  }))
  if (format === "zoho")
    return [
      { header: "Invoice Number", pick: (r) => String(r.invoice_number) },
      { header: "Invoice Date", pick: (r) => String(r.date) },
      { header: "Customer Name", pick: (r) => String(r.party) },
      { header: "GST Identification Number (GSTIN)", pick: (r) => String(r.party_tax_id) },
      { header: "Place of Supply", pick: (r) => String(r.place_of_supply) },
      { header: "Item Name", pick: (r) => String(r.item ?? "") },
      { header: "HSN/SAC", pick: (r) => String(r.service_code) },
      { header: "Item Tax %", pick: (r) => String(r.rate ?? "") },
      { header: "Item Total", pick: (r) => money(r.taxable, d.currency) },
      ...taxCols,
      { header: "Total", pick: (r) => money(r.grand_total, d.currency) },
    ]
  if (format === "erpnext")
    return [
      { header: "Invoice No (name)", pick: (r) => String(r.invoice_number) },
      { header: "Date (posting_date)", pick: (r) => String(r.date) },
      { header: "Customer (customer)", pick: (r) => String(r.party) },
      { header: "Tax Id", pick: (r) => String(r.party_tax_id) },
      { header: "Place of Supply", pick: (r) => String(r.place_of_supply) },
      { header: "Net Total (net_total)", pick: (r) => money(r.taxable, d.currency) },
      { header: "Total Taxes (total_taxes_and_charges)", pick: (r) => money(r.total_tax, d.currency) },
      { header: "Grand Total (grand_total)", pick: (r) => money(r.grand_total, d.currency) },
    ]
  if (format === "tally")
    return [
      { header: "Voucher Type", pick: () => "Sales" },
      { header: "Voucher No", pick: (r) => String(r.invoice_number) },
      { header: "Date", pick: (r) => String(r.date) },
      { header: "Party Ledger", pick: (r) => String(r.party) },
      { header: "GSTIN", pick: (r) => String(r.party_tax_id) },
      { header: "Sales Ledger Amount", pick: (r) => money(r.taxable, d.currency) },
      ...taxCols.map((c) => ({ header: c.header + " Ledger", pick: c.pick })),
      { header: "Invoice Amount", pick: (r) => money(r.grand_total, d.currency) },
    ]
  // generic
  return [
    { header: "Invoice", pick: (r) => String(r.invoice_number) },
    { header: "Date", pick: (r) => String(r.date) },
    { header: "Party", pick: (r) => String(r.party) },
    { header: d.tax_id_label, pick: (r) => String(r.party_tax_id) },
    { header: "Place of Supply", pick: (r) => String(r.place_of_supply) },
    { header: "Item", pick: (r) => String(r.item ?? "") },
    { header: "SAC/HSN", pick: (r) => String(r.service_code) },
    { header: "GL Code", pick: (r) => String(r.gl_code ?? "") },
    { header: `${d.tax_label} %`, pick: (r) => String(r.rate ?? "") },
    { header: "Taxable", pick: (r) => money(r.taxable, d.currency) },
    ...taxCols,
    { header: `${d.tax_label} Total`, pick: (r) => money(r.total_tax, d.currency) },
    { header: "Line Total", pick: (r) => money(r.line_total, d.currency) },
    { header: "Invoice Total", pick: (r) => money(r.grand_total, d.currency) },
  ]
}

const formats = () => [
  { key: "generic", label: "Generic CSV" },
  { key: "tally", label: "Tally" },
  { key: "zoho", label: "Zoho Books" },
  { key: "erpnext", label: accountingImportLabel() },
]

export default function AccountingExport() {
  useUILanguage()
  const property = getCurrentProperty()
  const today = new Date().toISOString().slice(0, 10)
  const monthAgo = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10)
  const [from, setFrom] = useState(monthAgo)
  const [to, setTo] = useState(today)
  const [format, setFormat] = useState("generic")
  const [data, setData] = useState<ExportData | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    call<ExportData>("kamra.accounting.export_invoices", {
      property,
      from_date: from,
      to_date: to,
    })
      .then(setData)
      .catch((e) => setError(serverError(e)))
  }, [property, from, to])
  useEffect(load, [load])

  function download() {
    if (!data) return
    const cols = columnsFor(format, data)
    downloadCsv(
      `invoices-${format}-${from}-to-${to}`,
      cols.map((c) => c.header),
      rowsFor(format, data).map((r) => cols.map((c) => c.pick(r, data))),
    )
  }

  const cols = data ? columnsFor(format, data) : []
  const shown = data ? rowsFor(format, data) : []

  return (
    <div className="space-y-5">
      <header className="flex items-center gap-2">
        <FileSpreadsheet className="size-5 text-accent-text" aria-hidden />
        <h1 className="text-xl font-semibold tracking-tight">{translate("Accounting Export")}</h1>
        <p className="ml-2 text-sm text-muted-ink">{translate("Closed invoices, ready to import into your books.")}</p>
      </header>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-center gap-2">
            <input
              type="date"
              className="rounded-lg border border-control px-2 py-1.5 text-sm"
              value={from}
              onChange={(e) => setFrom(e.target.value)}
              aria-label={translate("From")}
            />
            <span className="text-xs text-muted-ink">{translate("to")}</span>
            <input
              type="date"
              className="rounded-lg border border-control px-2 py-1.5 text-sm"
              value={to}
              onChange={(e) => setTo(e.target.value)}
              aria-label={translate("To")}
            />
            <select
              className="rounded-lg border border-control px-2.5 py-1.5 text-sm"
              value={format}
              onChange={(e) => setFormat(e.target.value)}
              aria-label={translate("Format")}
            >
              {formats().map((f) => (
                <option key={f.key} value={f.key}>
                  {translate(f.label)}
                </option>
              ))}
            </select>
          </div>
          <Button disabled={!data || data.rows.length === 0} onClick={download}>
            <Download className="size-4" aria-hidden />{translate("Download CSV")}</Button>
        </CardHeader>
        <CardContent>
          {error && (
            <div className="mb-3 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
              {translate(error)}
            </div>
          )}
          {data && (
            <>
              <p className="mb-3 text-sm text-muted-ink">
                {data.totals.invoices}{translate(" invoice")}{data.totals.invoices === 1 ? "" : translate("s")}{translate(" · taxable ")}
                {fmtMoney(data.totals.taxable, data.currency)} · {data.tax_label}{" "}
                {fmtMoney(data.totals.total_tax, data.currency)}{translate(" · total ")}
                {fmtMoney(data.totals.grand_total, data.currency)}
              </p>
              {data.rows.length === 0 ? (
                <p className="py-6 text-center text-sm text-muted-ink">{translate("No closed invoices in this window.")}</p>
              ) : (
                <div className="overflow-x-auto rounded-xl border border-line">
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="border-b border-line bg-canvas text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
                        {cols.map((c) => (
                          <th key={c.header} className="whitespace-nowrap px-3 py-2">
                            {translate(c.header)}
                          </th>
                        ))}
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-line-subtle">
                      {shown.slice(0, 12).map((r, i) => (
                        <tr key={i}>
                          {cols.map((c) => (
                            <td
                              key={c.header}
                              className="whitespace-nowrap px-3 py-2"
                            >
                              {c.pick(r, data)}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {shown.length > 12 && (
                    <p className="px-3 py-2 text-xs text-muted-ink">{translate("Showing 12 of ")}{shown.length}{translate(" rows - download for all.")}</p>
                  )}
                </div>
              )}
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
