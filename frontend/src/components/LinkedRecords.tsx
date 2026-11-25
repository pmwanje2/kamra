import { useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import {
  Building2,
  PartyPopper,
  Receipt,
  UserCircle2,
  Users,
} from "lucide-react"

import { call } from "../lib/api"
import { cur, fmtAmount, useLocale } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"

/** The connective tissue, rendered: one strip of chips on any record that
 *  jumps straight to its guest, folios (billing), company, group or event.
 *  Same component everywhere - reservations, events, groups, folios. */

interface Links {
  guest: string | null
  guest_name: string | null
  reservations: { name: string; status: string; check_in_date: string }[]
  folios: {
    name: string
    folio_type: string
    status: string
    balance: number
    invoice_number: string | null
    currency?: string | null
    currency_symbol?: string | null
    locale?: string | null
  }[]
  company: string | null
  group_booking: string | null
  group_name: string | null
  event: string | null
  event_type: string | null
}

const chipCls =
  "inline-flex min-h-11 items-center gap-1.5 rounded-full border border-line " +
  "bg-surface px-2.5 py-1 text-xs font-medium text-secondary-ink " +
  "hover:border-accent-control hover:text-accent-ink transition"

export default function LinkedRecords({
  doctype,
  name,
  exclude = [],
  currency,
  currencySymbol,
  numberLocale,
}: {
  doctype: string
  name: string
  /** hide chip kinds that would link back to the current screen */
  exclude?: ("guest" | "folio" | "company" | "group" | "event")[]
  currency?: string | null
  currencySymbol?: string | null
  numberLocale?: string | null
}) {
  useUILanguage()
  const loc = useLocale()
  const [links, setLinks] = useState<Links | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    call<Links>("kamra.api.linked_records", { doctype, name })
      .then(setLinks)
      .catch(() => setLinks(null))
  }, [doctype, name])

  if (!links) return null
  const fmtFolioBalance = (f: Links["folios"][number]) => {
    const ccy = f.currency || currency || loc.currency
    const sym =
      f.currency_symbol != null
        ? f.currency_symbol
        : currencySymbol != null
          ? currencySymbol
          : ccy && ccy !== loc.currency
            ? `${ccy} `
            : cur()
    const numLoc = f.locale || numberLocale || loc.locale
    return `${sym}${fmtAmount(f.balance || 0, ccy, numLoc)}`
  }

  const chips: React.ReactNode[] = []
  if (links.guest && !exclude.includes("guest"))
    chips.push(
      <button
        key="guest"
        className={chipCls}
        onClick={() => navigate(`/guests/${encodeURIComponent(links.guest!)}`)}
      >
        <UserCircle2 className="size-3.5" aria-hidden />
        {links.guest_name || translate("Guest")}
      </button>,
    )
  if (!exclude.includes("folio"))
    for (const f of links.folios)
      chips.push(
        <button
          key={f.name}
          className={chipCls}
          onClick={() => navigate(`/billing/${encodeURIComponent(f.name)}`)}
          title={(f.invoice_number ? translate("Invoice {v0}", { v0: String(f.invoice_number) }) : translate("Open folio"))}
        >
          <Receipt className="size-3.5" aria-hidden />
          {f.folio_type === "Guest" ? translate("Guest folio")
            : f.folio_type === "Company" ? translate("Company folio")
            : f.folio_type === "Extra" ? translate("Extra folio")
            : translate("{type} folio", { type: translate(f.folio_type) })}<span
            dir="ltr"
            className={
              f.balance > 0 ? "text-warning-text" : "text-muted-ink"
            }
          >
            {fmtFolioBalance(f)}
          </span>
        </button>,
      )
  if (links.group_booking && !exclude.includes("group"))
    chips.push(
      <button
        key="group"
        className={chipCls}
        onClick={() =>
          navigate(`/groups?open=${encodeURIComponent(links.group_booking!)}`)
        }
        title={translate("Open this group")}
      >
        <Users className="size-3.5" aria-hidden />
        {translate("Part of {v0} →", {
          v0: links.group_name || translate("Group"),
        })}
      </button>,
    )
  if (links.event && !exclude.includes("event"))
    chips.push(
      <button
        key="event"
        className={chipCls}
        onClick={() => navigate("/events")}
        title={translate("Open Event Bookings")}
      >
        <PartyPopper className="size-3.5" aria-hidden />
        {translate(links.event_type) || translate("Event")}
      </button>,
    )
  if (links.company && !exclude.includes("company"))
    chips.push(
      <button
        key="company"
        className={chipCls}
        onClick={() => navigate("/companies")}
        title={translate("Open Companies")}
      >
        <Building2 className="size-3.5" aria-hidden />
        {links.company}
      </button>,
    )

  if (chips.length === 0) return null
  return <div className="flex flex-wrap items-center gap-1.5">{chips}</div>
}
