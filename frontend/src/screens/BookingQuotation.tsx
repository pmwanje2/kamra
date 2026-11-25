import { useEffect, useState } from "react"
import { ArrowLeft, Mail, Printer } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { call } from "../lib/api"
import { Button } from "../components/ui/button"
import { cur, fmtAmount } from "../lib/money"
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
}

const money = (n: number) =>
  `${cur()}${fmtAmount(n)}`

export default function BookingQuotation() {
  useUILanguage()
  const { name } = useParams()
  const [d, setD] = useState<Quote | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [sent, setSent] = useState<string | null>(null)

  useEffect(() => {
    if (name)
      call<Quote>("kamra.api.booking_quotation", { reservation: name })
        .then(setD)
        .catch((e) => setError(serverError(e)))
  }, [name])

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

  const { property: p, booking: b, totals } = d
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

      <div className="space-y-6 rounded-xl border border-line bg-surface p-8 text-sm print:border-0 print:p-0">
        <header className="flex items-start justify-between gap-6">
          <div>
            {p.logo_url && <img src={p.logo_url} alt="" className="mb-2 h-12 object-contain" />}
            <div className="text-lg font-semibold text-ink">{p.property_name}</div>
            <div className="text-muted-ink">{p.address}</div>
            <div className="text-muted-ink">{[p.phone, p.email, p.website].filter(Boolean).join(" · ")}</div>
            {p.tax_id && <div className="text-muted-ink">{translate(p.tax_id_label)}: {p.tax_id}</div>}
          </div>
          <div className="text-right">
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

        <table className="w-full">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted-ink">
              <th className="py-2 pr-2 font-medium">{translate("Night")}</th>
              <th className="py-2 text-right font-medium">{translate("Rate / night")}</th>
            </tr>
          </thead>
          <tbody>
            {d.nights.map((n) => (
              <tr key={n.date} className="border-b border-line-subtle">
                <td className="py-2 pr-2">{n.date}</td>
                <td className="py-2 text-right">{money(n.rate)}</td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            {d.lines.map((line) => (
              <tr key={line.component}>
                <td className="pt-2 text-right text-muted-ink">{line.description}</td>
                <td className="pt-2 text-right">{money(line.amount)}</td>
              </tr>
            ))}
            <tr><td className="text-right text-muted-ink">{translate(p.tax_label)}</td><td className="text-right">{money(totals.tax)}</td></tr>
            <tr className="text-base font-semibold"><td className="pt-1 text-right">{translate("Total")}</td><td className="pt-1 text-right">{money(totals.total)}</td></tr>
            {totals.deposit_due > 0 && (
              <tr><td className="pt-2 text-right text-secondary-ink">{translate("Deposit to confirm")}</td><td className="pt-2 text-right font-medium">{money(totals.deposit_due)}</td></tr>
            )}
          </tfoot>
        </table>

        <section className="space-y-1 text-secondary-ink">
          <div className="font-medium text-ink">{translate("Terms")}</div>
          <ul className="list-disc space-y-1 pl-5">
            {d.terms.map((term) => <li key={term}>{term}</li>)}
          </ul>
        </section>
      </div>
    </div>
  )
}
