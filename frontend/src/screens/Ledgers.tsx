import { useCallback, useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import AppLink from "../components/AppLink"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { DataTable } from "../components/ui/data-table"
import { keptInErp } from "../lib/erpBooks"
import { fmtMoney, useLocale } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"
import { CheckCircle2, Info, TriangleAlert } from "lucide-react"

/** What each ledger card means, where to see it, how entries get there and
 *  how a mistake is put right, for the (i) on the card. */
const helpFor = (): Record<string, { what: string; where: string; changes: string; fix: string; drill: string; link?: [string, string] }> => ({
  Guest: {
    what: translate("What guests owe on their bills right now: every charge posted to a guest's folio (room nights, restaurant and laundry charged to the room, extras) less what they paid, advances applied at check-in, and bills moved to a company. Positive means guests owe the hotel."),
    where: translate("Billing and each guest's folio; departed guests who still owe are under Guests left owing in Billing."),
    changes: translate("Added automatically: the night audit posts room nights, the restaurant and laundry post their bills, the desk adds charges and takes payments, check-out moves company bills to the city ledger."),
    fix: translate("Entries are never edited or deleted. Void a wrong charge on the open folio, post a discount (allowance), refund an overpayment, or cancel a closed invoice and reissue it. Each one adds a correcting entry."),
    drill: translate("Click the card: every folio with a balance, biggest first. Open a folio to see each line."),
    link: ["/billing", translate("Open Billing")],
  },
  Deposit: {
    what: translate("Advances held for guests who have not arrived yet. Negative means the hotel holds that money for them. An advance leaves this ledger when the guest checks in (it pays the stay) or when a no-show or cancellation fee uses it."),
    where: translate("Each booking's folio; the total is Advances held in Receivables, and the night audit pack lists every booking holding one."),
    changes: translate("Added when an advance or security deposit is taken on a booking; moved out at check-in, by a no-show or cancellation fee, or by a refund."),
    fix: translate("Refund the advance from the booking's folio, or cancel the payment the same day. Nothing is edited in place."),
    drill: translate("Click the card: every booking still holding an advance, with its status. A Cancelled or No Show one needs a refund or a fee."),
  },
  AR: {
    what: translate("What companies and travel agents owe (the city ledger). A company's guest bill lands here at check-out; the company's payments take it off."),
    where: translate("Receivables: each company's page shows every invoice, every payment and the bills it paid, with the running balance."),
    changes: translate("Added at check-out when a bill goes to a company, or by a direct company invoice. Reduced by a recorded company payment or a credit note."),
    fix: translate("Issue a credit note against the invoice, move a bill to another company (Transfer), or cancel and reissue the invoice. Each adds an entry; nothing is deleted."),
    drill: translate("Click the card: every bill still open on a company's account. Each company's full history is in Receivables."),
    link: ["/receivables", translate("Open Receivables")],
  },
  Package: {
    what: translate("Meant for prepaid packages held until they are used (for example a meal plan sold with the room). {product} does not post to it yet, so it stays at 0."),
    where: translate("Nothing to see while it is 0."),
    changes: translate("No screen posts to it today; package revenue goes to the Guest ledger with the room."),
    fix: translate("Nothing to correct."),
    drill: translate("Nothing to drill into while it is 0."),
  },
})

type DetailRow = {
  folio: string; guest_name?: string | null; folio_type?: string | null
  folio_status?: string | null; invoice_number?: string | null
  reservation?: string | null; stay_status?: string | null; company?: string | null
  debit: number; credit: number; balance: number; last_date: string
}

type Ledger = {
  ledger: string
  debit: number | null
  credit: number | null
  balance: number | null
  kept_in_erp?: boolean
  unverified?: boolean
}

/** Guest + Deposit ledger postings per folio against each folio's balance
 *  (kamra.ledger.folio_reconciliation). */
type Recon = {
  in_balance: boolean
  ledger_net: number
  folios_net: number
  difference: number
  folios_out: number
  folios: { folio: string; folio_type: string; status: string; balance: number; ledger: number; difference: number }[]
}

export default function Ledgers() {
  useUILanguage()
  useLocale()
  const [date, setDate] = useState("")
  const [from, setFrom] = useState("")
  const [ledgers, setLedgers] = useState<Ledger[]>([])
  const [arKeptInErp, setArKeptInErp] = useState(false)
  const [arUnverified, setArUnverified] = useState(false)
  const [help, setHelp] = useState<string | null>(null)
  const [open, setOpen] = useState<string | null>(null)
  const [detail, setDetail] = useState<DetailRow[] | null>(null)
  const [capped, setCapped] = useState(false)
  // the newest request wins: typing a date fires one per keystroke
  const seq = useRef(0)
  const [inBalance, setInBalance] = useState<boolean | null | undefined>(undefined)
  const [recon, setRecon] = useState<Recon | null>(null)
  const [journal, setJournal] = useState<JournalRow[]>([])
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    if (!open) { setDetail(null); return }
    let stale = false
    setDetail(null)
    call<{ rows: DetailRow[]; truncated?: boolean }>("kamra.ledger.ledger_detail", {
      property: getCurrentProperty(), ledger: open, business_date: date || undefined,
    }).then((r) => { if (!stale) { setDetail(r.rows); setCapped(!!r.truncated) } })
      .catch(() => { if (!stale) setDetail([]) })
    return () => { stale = true }
  }, [open, date])

  // balances as of `date`; the journal covers `from` to `date`
  const load = useCallback(async () => {
    const mine = ++seq.current
    try {
      const bal = await call<{
        business_date: string
        ledgers: Ledger[]
        in_balance: boolean | null
        reconciliation: Recon | null
        ar_kept_in_erp?: boolean
        ar_unverified?: boolean
      }>("kamra.ledger.ledger_balances", {
        property: getCurrentProperty(),
        business_date: date || undefined,
      })
      if (mine !== seq.current) return
      setLedgers(bal.ledgers)
      setInBalance(bal.in_balance)
      setRecon(bal.reconciliation)
      setArKeptInErp(Boolean(bal.ar_kept_in_erp))
      setArUnverified(Boolean(bal.ar_unverified))
      if (!date) {
        setDate(bal.business_date)
        setFrom(String(bal.business_date).slice(0, 8) + "01")
        return
      }
      const j = await call<{ rows: JournalRow[] }>("kamra.ledger.journal_by_transaction_code", {
        property: getCurrentProperty(), business_date: date, from_date: from || date, to_date: date,
      })
      if (mine !== seq.current) return
      setJournal(j.rows)
      setErr(null)
    } catch (e) {
      setErr(serverError(e))
    }
  }, [date, from])

  useEffect(() => {
    void load()
  }, [load])

  const dateInput = "rounded-lg border border-control px-2 py-1.5 text-sm"
  const helpCard = help ? helpFor()[help] : undefined
  const erpReceivables = keptInErp("/receivables")
  const arRow = ledgers.find((l) => l.ledger === "AR")
  const isArUnverified = arUnverified || Boolean(arRow?.unverified)
  const isArKeptInErp = arKeptInErp || Boolean(arRow?.kept_in_erp) || Boolean(erpReceivables)
  const openArLabel = erpReceivables
    ? translate("Open {v0}", { v0: erpReceivables.label })
    : translate("Open Receivables")

  return (
    <div className="w-full space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-ink">{translate("Books")}</p>
          <h1 className="text-2xl font-semibold">{translate("Books check")}</h1>
          <p className="text-sm text-muted-ink">{translate("Guest, deposit, company and package ledgers against every folio")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1.5 text-sm text-muted-ink">
            {translate("From")}
            <input type="date" className={dateInput} value={from} max={date || undefined}
              onChange={(e) => setFrom(e.target.value)} />
          </label>
          <label className="flex items-center gap-1.5 text-sm text-muted-ink">
            {translate("To")}
            <input type="date" className={dateInput} value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
        </div>
      </div>
      {err ? <p className="text-sm text-danger-text">{translate(err)}</p> : null}

      {/* the four are control ledgers, so debits beat credits by what guests
          owe; the real test is that Guest + Deposit agree with every folio */}
      {inBalance === undefined ? null : inBalance === null ? (
        <p className="rounded-lg border border-line bg-canvas px-4 py-2 text-sm text-secondary-ink">
          {translate("Balances as of {v0}. The check against folios runs for the current business date only.", { v0: String(date) })}
        </p>
      ) : inBalance ? (
        isArUnverified ? (
          <div className="flex items-start gap-2 rounded-lg border border-warning-line bg-warning-subtle px-4 py-2 text-sm text-warning-ink">
            <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="min-w-0 flex-1">
              <strong className="font-medium">{translate("Not verified")}</strong> —{" "}
              {translate("Guest and deposit folios match their ledgers. Company receivables (AR) could not be verified in the accounting system.")}{" "}
              <AppLink to="/receivables" className="font-medium underline">{openArLabel}</AppLink>
            </span>
          </div>
        ) : isArKeptInErp ? (
          <div className="flex items-start gap-2 rounded-lg border border-line bg-canvas px-4 py-2 text-sm text-secondary-ink">
            <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
            <span className="min-w-0 flex-1">
              {translate("Guest and deposit folios match their ledgers. Company receivables (AR) are kept in the accounting system.")}{" "}
              <AppLink to="/receivables" className="font-medium text-accent-ink underline">{openArLabel}</AppLink>
            </span>
          </div>
        ) : (
          <p className="flex items-center gap-2 rounded-lg border border-success-line bg-success-subtle px-4 py-2 text-sm font-medium text-success-ink">
            <CheckCircle2 className="size-4" aria-hidden />
            {translate("Folios and ledgers agree.")}
          </p>
        )
      ) : (
        <div className="flex flex-wrap items-start gap-3 rounded-lg border border-warning-line bg-warning-subtle px-4 py-3 text-sm text-warning-ink">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="font-medium">
              {translate("{v0} folio(s) do not match the ledger · {v1} apart", { v0: String(recon?.folios_out ?? 0), v1: String(fmtMoney(recon?.difference)) })}
            </p>
            <p className="mt-0.5 text-xs">{translate("Open a folio to find the posting that reached one side and not the other.")}</p>
            <ul className="mt-2 flex flex-wrap gap-1.5 text-xs">
              {(recon?.folios ?? []).map((f) => (
                <li key={f.folio}>
                  <Link className="inline-block rounded-md border border-warning-line-strong bg-surface px-2 py-1 hover:bg-warning-muted"
                    to={`/billing/${encodeURIComponent(f.folio)}`}>
                    {f.folio} · {fmtMoney(f.difference)}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {ledgers.map((l) => {
          const arCardUnverified = l.ledger === "AR" && isArUnverified
          const arCardKept = l.ledger === "AR" && isArKeptInErp
          return (
            <Card key={l.ledger}
              className={`transition hover:border-control ${open === l.ledger ? "ring-2 ring-accent-control/40" : ""}`}>
              <CardHeader className="flex flex-row items-center justify-between pb-2">
                <h3 className="text-sm font-medium text-muted-ink">
                  {translate(l.ledger)}{translate(" ledger")}</h3>
                <button type="button" aria-label={translate("What is this ledger?")}
                  className={`rounded-full p-1 hover:bg-subtle ${help === l.ledger ? "text-accent-ink" : "text-muted-ink"}`}
                  onClick={() => setHelp(help === l.ledger ? null : l.ledger)}>
                  <Info className="size-4" />
                </button>
              </CardHeader>
              <CardContent>
                {arCardUnverified ? (
                  <>
                    <p className="text-lg font-semibold text-warning-ink">{translate("Not verified")}</p>
                    <p className="mt-1 text-xs text-muted-ink">{translate("Company receivables could not be verified in the accounting system.")}</p>
                    <AppLink to="/receivables" className="mt-2 inline-block text-xs font-medium text-accent-ink hover:underline">
                      {openArLabel}
                    </AppLink>
                  </>
                ) : arCardKept ? (
                  <>
                    <p className="text-lg font-semibold text-secondary-ink">{translate("Kept in the accounting system")}</p>
                    <p className="mt-1 text-xs text-muted-ink">{translate("This is kept in the accounting system.")}</p>
                    <AppLink to="/receivables" className="mt-2 inline-block text-xs font-medium text-accent-ink hover:underline">
                      {openArLabel}
                    </AppLink>
                  </>
                ) : (
                  <>
                    <p className="text-2xl font-semibold tabular-nums">{fmtMoney(l.balance)}</p>
                    <p className="mt-1 text-xs text-muted-ink">{translate("Dr ")}{fmtMoney(l.debit)}{translate(" · Cr ")}{fmtMoney(l.credit)}</p>
                    <button type="button" aria-expanded={open === l.ledger}
                      className="mt-2 text-xs font-medium text-accent-ink hover:underline"
                      onClick={() => setOpen(open === l.ledger ? null : l.ledger)}>
                      {open === l.ledger ? translate("Hide who makes it up") : translate("Who makes it up")}
                    </button>
                  </>
                )}
              </CardContent>
            </Card>
          )
        })}
      </div>

      {helpCard ? (
        <Card className="border-success-line bg-success-subtle/40">
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardTitle className="text-base">{translate(help)}{translate(" ledger")}</CardTitle>
            <Button variant="ghost" onClick={() => setHelp(null)}>{translate("Close")}</Button>
          </CardHeader>
          <CardContent>
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              {([
                [translate("What it is"), helpCard.what],
                [translate("Where to see it"), helpCard.where],
                [translate("How entries get here"), helpCard.changes],
                [translate("Correcting a mistake"), helpCard.fix],
                [translate("Drill down"), helpCard.drill],
              ] as const).map(([k, v]) => (
                <div key={k}>
                  <dt className="text-xs font-semibold uppercase tracking-wide text-muted-ink">{k}</dt>
                  <dd className="mt-0.5 text-secondary-ink">{v}</dd>
                </div>
              ))}
            </dl>
            {helpCard.link ? (
              <AppLink className="mt-3 inline-block text-sm font-medium text-accent-ink hover:underline"
                to={helpCard.link[0]}>{helpCard.link[1]}</AppLink>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      {open ? (
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-base">
              {translate(open)}{translate(" ledger")}{translate(": who makes up the balance")} · {date}
            </CardTitle>
            <Button variant="ghost" onClick={() => setOpen(null)}>{translate("Close")}</Button>
          </CardHeader>
          <CardContent>
            {!detail ? <p className="py-6 text-center text-sm text-muted-ink">{translate("Loading…")}</p> : (
              <>
              {capped ? (
                <p className="mb-2 text-xs text-warning-ink">
                  {translate("Showing the {n} biggest balances. The ledger total above includes every folio.", { n: detail.length })}
                </p>
              ) : null}
              <DataTable<DetailRow>
                id={`ledgers.detail.${open}`} exportName={`${open.toLowerCase()}-ledger-${date}`}
                rows={detail} rowKey={(r) => r.folio} empty={translate("Nothing on this ledger.")}
                columns={[
                  { key: "folio", label: translate("Folio"), value: (r) => r.invoice_number || r.folio,
                    render: (r) => <Link className="font-mono text-xs hover:underline" to={`/billing/${encodeURIComponent(r.folio)}`}>{r.invoice_number || r.folio}</Link> },
                  { key: "guest_name", label: translate("Guest"), render: (r) => (<>{r.guest_name || "-"}
                    {r.company ? <span className="block text-xs text-muted-ink">{r.company}</span> : null}</>) },
                  { key: "company", label: translate("Company"), hidden: true },
                  { key: "reservation", label: translate("Booking"), render: (r) => <span className="font-mono text-xs">{r.reservation || "-"}</span> },
                  { key: "status", label: translate("Status"), value: (r) => r.stay_status || r.folio_status || "",
                    render: (r) => <span className="text-xs">{translate(r.stay_status || r.folio_status || "-")}</span> },
                  { key: "last_date", label: translate("Last entry"), render: (r) => <span className="text-xs text-muted-ink">{r.last_date}</span> },
                  { key: "debit", label: translate("Debit"), align: "right", render: (r) => fmtMoney(r.debit) },
                  { key: "credit", label: translate("Credit"), align: "right", render: (r) => fmtMoney(r.credit) },
                  { key: "balance", label: translate("Balance"), align: "right", render: (r) => <span className="font-medium">{fmtMoney(r.balance)}</span> },
                ]}
              />
              </>
            )}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">
            {translate("Journal by transaction code")} · {from === date ? date : translate("{a} to {b}", { a: from, b: date })}
          </CardTitle>
        </CardHeader>
        <CardContent>
          <DataTable<JournalRow>
            id="ledgers.journal" exportName={`journal-${from}-to-${date}`}
            rows={journal} rowKey={(r) => r.transaction_code}
            empty={translate("No postings in these dates.")}
            columns={[
              { key: "transaction_code", label: translate("Code"), render: (r) => <span className="font-mono text-xs">{r.transaction_code}</span> },
              { key: "description", label: translate("Description") },
              { key: "debit", label: translate("Debit"), align: "right", render: (r) => fmtMoney(r.debit) },
              { key: "credit", label: translate("Credit"), align: "right", render: (r) => fmtMoney(r.credit) },
              { key: "entries", label: translate("Entries"), align: "right" },
            ]}
          />
        </CardContent>
      </Card>

      <p className="text-sm text-muted-ink">
        {translate("What each company owes, and how late, is in Receivables.")}{" "}
        <AppLink to="/receivables" className="font-medium text-accent-ink hover:underline">{translate("Open Receivables")}</AppLink>
      </p>
    </div>
  )
}

type JournalRow = { transaction_code: string; description: string; debit: number; credit: number; entries: number }
