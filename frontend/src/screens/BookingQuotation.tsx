import { useEffect, useState } from "react"
import { ArrowLeft, Mail, Printer } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { call } from "../lib/api"
import { Button } from "../components/ui/button"
import { fmtMoney, moneyLocale } from "../lib/money"
import { currencyText, formatFxRate } from "../components/ContractTariff"
import { serverError } from "../lib/resource"
import { t as translate, useUILanguage } from "../lib/i18n"

/** Printable quotation / proforma for one booking - the GroupQuotation
 *  layout with the booking's own nightly rates, totals and cancellation
 *  terms. "Email to guest" sends it as a PDF through Frappe email, to the
 *  address on record only. Nothing is posted. */

interface Quote {
  property: {
    property_name: string
    logo_url: string | null
    address: string
    phone: string | null
    email: string | null
    website: string | null
    tax_id: string | null
    tax_id_label: string
    tax_label: string
    currency?: string
    currency_symbol?: string
    locale?: string
  }
  booking: {
    name: string
    status: string
    guest_name: string | null
    booked_by: string | null
    company: string | null
    room_type_name: string | null
    meal_plan: string | null
    adults: number
    children: number
    check_in_date: string
    check_out_date: string
    nights: number
  }
  nights: { date: string; rate: number; meal: number }[]
  /** room nights, then each inclusion, fee and discount: with tax, the total */
  lines: { component: string; description: string; amount: number }[]
  totals: { before_tax: number; tax: number; discount: number; total: number; deposit_due: number }
  cancellation: { free_now: boolean; free_until: string | null; fee_label: string }
  /** written by the server for this booking, matching the lines */
  terms: string[]
  email_to: string | null
  issued_on: string
  currency_reference?: {
    contract_currency: string
    fx_rate: number
    fx_date: string
    fx_lock_policy: string
    nights: { date: string; contract_rate: number; estimated?: boolean }[]
    contract_totals: { total: number }
  }
}

export default function BookingQuotation() {
  const language = useUILanguage()
  const { name } = useParams()
  const [d, setD] = useState<Quote | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState<string | null>(null)

  useEffect(() => {
    if (!name) return
    let current = true
    call<Quote>("kamra.api.booking_quotation", { reservation: name })
      .then((quote) => { if (current) setD(quote) })
      .catch((e) => { if (current) setError(serverError(e)) })
    return () => { current = false }
  }, [name, language])

  async function emailToGuest() {
    if (!d) return
    setSending(true)
    setError(null)
    setSent(null)
    try {
      const out = await call<{ sent_to: string; status: string }>("kamra.api.email_booking_quotation", { reservation: d.booking.name })
      setSent(out.sent_to)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setSending(false)
    }
  }

  if (!d) return error ? <p className="py-10 text-center text-danger-text">{error}</p>
    : <p className="py-10 text-center text-muted-ink">{translate("Loading…")}</p>

  const { property: p, booking: b, totals, currency_reference: reference } = d
  const numberLocale = language === "en" ? p.locale || moneyLocale() : language
  const money = (n: number) => reference && p.currency
    ? currencyText(p.currency, n, numberLocale)
    : fmtMoney(n, { currency: p.currency, currency_symbol: p.currency_symbol, locale: numberLocale })
  const contractRates = new Map(reference?.nights.map((night) => [night.date, night]))
  const contractMoney = (n: number) => currencyText(reference?.contract_currency || "", n, numberLocale)
  return (
    <div className="mx-auto max-w-3xl">
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 print:hidden">
        <Link
          to={`/reservations?open=${encodeURIComponent(b.name)}`}
          className="inline-flex items-center gap-1.5 text-sm text-muted-ink hover:text-ink"
        >
          <ArrowLeft className="size-4" aria-hidden />{translate("Back to the booking")}
        </Link>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={sending || !d.email_to} onClick={() => void emailToGuest()}
            title={d.email_to ?? translate("No email address on record for the booker.")}>
            <Mail className="size-4" aria-hidden />{sending ? translate("Sending…") : translate("Email to guest")}
          </Button>
          <Button onClick={() => window.print()}>
            <Printer className="size-4" aria-hidden />{translate("Print / Save as PDF")}
          </Button>
        </div>
      </div>
      {(sent || error || !d.email_to) && (
        <div className="mb-4 space-y-1 text-sm print:hidden">
          {sent && <p className="text-success-ink">{translate("Quotation queued for sending to {email}.", { email: sent })}</p>}
          {error && <p className="text-danger-text">{translate(error)}</p>}
          {!d.email_to && <p className="text-muted-ink">{translate("No email address on record for the booker.")}</p>}
        </div>
      )}

      <div className="space-y-6 rounded-xl border border-line bg-surface p-4 text-sm sm:p-8 print:border-0 print:p-0">
        <header className="flex flex-col items-start justify-between gap-3 [overflow-wrap:anywhere] sm:flex-row sm:gap-6">
          <div className="w-full min-w-0 sm:w-auto">
            {p.logo_url && <img src={p.logo_url} alt="" className="mb-2 h-12 object-contain" />}
            <div className="text-lg font-semibold text-ink">{p.property_name}</div>
            <div className="text-muted-ink">{p.address}</div>
            <div className="text-muted-ink"><bdi>{[p.phone, p.email, p.website].filter(Boolean).join(" · ")}</bdi></div>
            {p.tax_id && <div className="text-muted-ink">{translate(p.tax_id_label)}: {p.tax_id}</div>}
          </div>
          <div className="min-w-0 max-w-full text-start sm:text-end">
            <div className="text-xl font-semibold tracking-tight text-ink">{translate("Quotation")}</div>
            <div className="text-muted-ink">{b.name}</div>
            <div className="text-muted-ink">{translate("Issued {v0}", { v0: d.issued_on })}</div>
          </div>
        </header>

        <section className="grid gap-1 rounded-lg bg-canvas p-4 sm:grid-cols-2">
          <div><span className="text-muted-ink">{translate("Guest")}: </span>{b.guest_name}</div>
          {b.booked_by && <div><span className="text-muted-ink">{translate("Booked by")}: </span>{b.booked_by}</div>}
          {b.company && <div><span className="text-muted-ink">{translate("Company")}: </span>{b.company}</div>}
          <div>
            <span className="text-muted-ink">{translate("Stay")}: </span>
            {b.check_in_date} → {b.check_out_date} · {translate("{v0} night(s)", { v0: String(b.nights) })}
          </div>
          {b.room_type_name && <div><span className="text-muted-ink">{translate("Room type")}: </span>{b.room_type_name}</div>}
          {b.meal_plan && <div><span className="text-muted-ink">{translate("Meal plan")}: </span>{b.meal_plan}</div>}
        </section>

        {reference && (
          <p className="text-xs text-secondary-ink">
            {translate("1 {foreign} = {rate} {home}", {
              foreign: reference.contract_currency, rate: formatFxRate(reference.fx_rate, numberLocale), home: p.currency || ""
            })}
            <br />
            {translate("Amounts marked ≈ are estimates.")}
          </p>
        )}
        <table className="w-full">
          <thead>
            <tr className="border-b border-line text-start text-xs text-muted-ink">
              <th className="py-2 pe-2 font-medium">{translate("Night")}</th>
              <th className="py-2 text-end font-medium">
                {reference ? `${translate("Property accounting currency")} (${p.currency})` : translate("Rate / night")}
              </th>
              {reference && <th className="py-2 ps-2 text-end font-medium">
                {translate("Rate currency")} ({reference.contract_currency})
              </th>}
            </tr>
          </thead>
          <tbody>
            {d.nights.map((n) => {
              const rate = contractRates.get(n.date)
              return <tr key={n.date} className="border-b border-line-subtle">
                <td className="py-2 pe-2">{n.date}</td>
                <td className="py-2 text-end">{money(n.rate)}</td>
                {reference && <td className="py-2 ps-2 text-end">
                  {rate && `${rate.estimated ? "≈ " : ""}${contractMoney(rate.contract_rate)}`}
                </td>}
              </tr>
            })}
          </tbody>
          <tfoot>
            {d.lines.map((line) => (
              <tr key={line.component}>
                <td className="pt-2 text-end text-muted-ink">{line.description}</td>
                <td className="pt-2 text-end" colSpan={reference ? 2 : 1}>{money(line.amount)}</td>
              </tr>
            ))}
            <tr><td className="text-end text-muted-ink">{translate(p.tax_label)}</td><td className="text-end" colSpan={reference ? 2 : 1}>{money(totals.tax)}</td></tr>
            <tr className="text-base font-semibold"><td className="pt-1 text-end">{translate("Total")}</td><td className="pt-1 text-end" colSpan={reference ? 2 : 1}>{money(totals.total)}</td></tr>
            {reference && <tr className="text-secondary-ink">
              <td className="pt-1 text-end">{translate("Total in rate currency")}</td>
              <td className="pt-1 text-end" colSpan={2}>≈ {contractMoney(reference.contract_totals.total)}</td>
            </tr>}
            {totals.deposit_due > 0 && (
              <tr><td className="pt-2 text-end text-secondary-ink">{translate("Deposit to confirm")}</td><td className="pt-2 text-end font-medium" colSpan={reference ? 2 : 1}>{money(totals.deposit_due)}</td></tr>
            )}
          </tfoot>
        </table>

        <section className="space-y-1 text-secondary-ink">
          <div className="font-medium text-ink">{translate("Terms")}</div>
          <ul className="list-disc space-y-1 ps-5">
            {d.terms.map((term) => <li key={term}>{term}</li>)}
          </ul>
        </section>
      </div>
    </div>
  )
}
