import { SettlementPreview, useSettlementQuote } from "../components/SettlementPreview"
import TenderPicker, { TenderInput, resolveTender, tenderAmount, tenderModes, tenderReady, useTenderRates } from "../components/TenderPicker"
import { currencyText, formatFxRate, TariffNight } from "../components/ContractTariff"
import RowCorrection from "../components/RowCorrection"
import InvoiceCorrectionDialog from "../components/InvoiceCorrectionDialog"
import { ResponsiveList, Popin } from "../components/ui/responsive-list"
import { Fragment, useCallback, useEffect, useRef, useState } from "react"
import { ArrowLeft, ArrowRightLeft, Printer, X } from "lucide-react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { formatDate, roomLabel } from "../lib/utils"
import { call, getCurrentProperty, newIdempotencyKey, type TariffReference } from "../lib/api"
import EditableNationality from "../components/EditableNationality"
import LinkedRecords from "../components/LinkedRecords"
import AllowanceRequests from "../components/AllowanceRequests"
import FolioDiscount from "../components/FolioDiscount"
import { ADVANCE_BILL_ACTION } from "../lib/discountHint"
import ChangeCompany from "../components/ChangeCompany"
import { loadLocale, sellsAlcohol, taxRates, fmtAmount } from "../lib/money"
import { defaultChargeTaxRate } from "../lib/chargeTax"
import { folioLabel, type SiblingFolio } from "../lib/folioLabel"
import { serverError } from "../lib/resource"
import { useCashierAuth } from "../lib/cashierAuth"
import { useAuth } from "../lib/auth"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { cur, locale, taxLabel, useLocale } from "../lib/money"
import { useT, t as translate, useUILanguage } from "../lib/i18n"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../components/ui/card"


const inr = (n: unknown) =>
  fmtAmount(n ?? 0)

interface InvoiceData {
  folio: {
    name: string
    property?: string
    status: "Open" | "Closed"
    folio_type?: string
    invoice_number: string | null
    /** the accounting system's invoice for this folio, where it issued one:
     *  it is then corrected there (credit note and re-bill), never here */
    sales_invoice?: string | null
    guest_name: string
    charges: {
      name: string
      posting_date: string
      charge_type: string
      description: string
      allowance_serial?: string | null
      /** on a void's reversal line: the line it reverses (both stay on the bill) */
      reversal_of?: string | null
      void_reason?: string | null
      voided_by?: string | null
      /** the line a split part, or a split's reversal, came from */
      split_of?: string | null
      /** outlet charge reference (e.g. POS order) — moves whole, never splits */
      source_ref?: string | null
      amount: number
      gst_rate: number
      gst_amount: number
      total: number
    }[]
    payments: {
      name: string
      exchange_rate_date?: string | null
      payment_kind?: string | null
      posting_date: string
      mode: string
      amount: number
      reference: string | null
      currency?: string | null
      fx_amount?: number | null
      exchange_rate?: number | null
    }[]
    charges_total: number
    tax_total: number
    grand_total: number
    payments_total: number
    balance: number
  }
  property: {
    currency: string
    name: string
    legal_name: string | null
    logo_url: string | null
    address: string
    state: string | null
    gstin: string | null
    phone: string | null
    email: string | null
    sac: string | null
    place_of_supply: string | null
    tax_id_label?: string
    /** the pack's line on how tax is shown (India: GST inclusive) */
    tax_note?: string
  }
  /** an Account folio (raised on a company's AR account) has no stay:
   *  only reservation (null) and company come back */
  stay: TariffReference & {
    reservation: string | null
    check_in: string
    check_out: string
    arrival: string | null
    departure: string | null
    nights: number
    adults: number
    children: number
    pax: number
    meal_plan: string | null
    room_type: string | null
    room: string | null
    room_number?: string | null
    company: string | null
    group_booking: string | null
    booked_by_name: string | null
    booked_by_phone: string | null
    contact_preference: string | null
  }
  gst_summary: {
    rate: number
    taxable: number
    cgst: number
    sgst: number
    total_tax: number
  }[]
  bill_to: { name: string; gstin: string | null } | null
  /** The document's own identity: a bill before settlement is provisional
   *  and says so; only a closed folio carries an invoice number. */
  /** What the tax authority requires on the bill - ZATCA's QR code and
   *  bilingual title in Saudi Arabia; null elsewhere. */
  statutory?: {
    title_ar: string
    qr_image: string
    uuid: string
    icv: number
    status: string
  } | null
  document: {
    title: string
    title_local?: string | null
    is_final: boolean
    number: string
    date: string
    amount_in_words: string
    footer: string | null
    invoice_footer?: string | null
    service_code_label: string
  }
  /** Charges with the service code the tax authority expects per LINE -
   *  a room night, a restaurant cover and a laundry bag are three
   *  different supplies and can't share one code. */
  lines: {
    row: string
    posting_date: string
    charge_type: string
    description: string | null
    qty: number
    rate: number
    amount: number
    gst_rate: number
    gst_amount: number
    total: number
    service_code: string | null
  }[]
  guest: {
    guest_id?: string
    name: string
    phone: string | null
    email: string | null
    nationality: string | null
    address: string
    id_type: string | null
  }
  /** what the issued credit notes on this invoice add up to */
  credited?: number
  summary_by_head: {
    head: string
    amount: number
    tax: number
    total: number
    lines: number
    service_code: string | null
  }[]
  tax_summary: {
    rate: number
    taxable: number
    total_tax: number
    parts: { label: string; rate: number; amount: number }[]
  }[]
}

const CHARGE_TYPES = [
  "Food & Beverage", "Minibar", "Laundry", "Spa", "Taxi",
  "Early Check-in", "Late Checkout", "Pet Charge", "Discount", "Misc",
]

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

const isEmptyFolio = (s: SiblingFolio) =>
  !s.grand_total && !s.payments_total

/** One labelled fact on the printed bill - label and value stay on the
 *  same line so the header block reads as a form, not a paragraph. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  useUILanguage()
  return (
    <div className="flex gap-2">
      <dt className="w-24 shrink-0 text-zinc-500">{translate(label)}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  )
}

/** Kamra Verify: check the bill-to tax ID before the invoice goes out.
 *  Offered where the country pack has it (India: GSTIN); paid per check
 *  from the Kamra Connect wallet. */
function VerifyTaxId({ taxId, name }: { taxId: string; name: string }) {
  const { t } = useT()
  const [busy, setBusy] = useState(false)
  const [res, setRes] = useState<{ ok: boolean; text: string } | null>(null)
  return (
    <span className="print:hidden">
      {" "}
      <button type="button" disabled={busy}
        className="text-xs font-medium text-brand-700 hover:underline disabled:opacity-50"
        title={t("ID verification partner checks this tax ID with the tax network. Charged per check to your verification wallet.")}
        onClick={async () => {
          setBusy(true); setRes(null)
          try {
            const r = await call<{ valid: boolean; legal_name: string; status: string; charged: number }>(
              "kamra.api.verify_tax_id",
              { property: getCurrentProperty(), tax_id: taxId, business_name: name })
            setRes({
              ok: r.valid && (r.status || "").toLowerCase() === "active",
              text: `${r.valid ? r.legal_name || t("Valid") : t("Not a valid tax ID")}${r.status ? ` · ${r.status}` : ""} · ${cur()}${r.charged}`,
            })
          } catch (e) {
            setRes({ ok: false, text: serverError(e) })
          } finally {
            setBusy(false)
          }
        }}>
        {busy ? t("Checking…") : t("Verify")}
      </button>
      {res && <span className={`ml-1 text-xs ${res.ok ? "text-emerald-700" : "text-red-700"}`}>{translate(res.text)}</span>}
    </span>
  )
}

export default function FolioView() {
  const { t } = useT()
  const { name } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [data, setData] = useState<InvoiceData | null>(null)
  const [siblings, setSiblings] = useState<SiblingFolio[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [charge, setCharge] = useState({
    charge_type: "Food & Beverage", description: "", amount: "",
    // the property's own rate (0% where it charges no tax), never a made-up one
    gst_rate: defaultChargeTaxRate(taxRates(), locale().services_tax_rate),
    is_alcohol: false,
  })
  const taxPicked = useRef(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [partVal, setPartVal] = useState("")
  const loc = useLocale()
  const [payment, setPayment] = useState({
    mode: "",
    amount: "",
    reference: "",
    kind: "Payment",
    currency: "",
  })
  // one key per Record press; reused on retry (#550)
  const [paymentKey, setPaymentKey] = useState(newIdempotencyKey)
  const folioProperty = data?.folio.property || getCurrentProperty()
  const home = data?.property.currency || loc.currency
  const { today: settlementRates, rates: fxRates } = useTenderRates(folioProperty, !!data)
  const availableTenders = tenderModes(loc, home, true)
  const paymentTenders = payment.kind === "Security Deposit"
    ? availableTenders.filter(({ currencies }) => currencies.includes(home)).map(({ mode }) => ({ mode, currencies: [home] }))
    : availableTenders
  const tender = resolveTender(paymentTenders, home, payment)
  const paymentReady = tenderReady(tender, home, settlementRates)
  const foreign = !!tender.currency && tender.currency !== home
  const [showIn, setShowIn] = useState("")
  const [shown, setShown] = useState<{
    currency: string; rate: number; rate_date: string; home_currency: string
    grand_total: number; balance: number
  } | null>(null)
  const quote = useSettlementQuote(folioProperty, foreign && paymentReady ? tender.currency : "", Number(payment.amount))
  const [showCancel, setShowCancel] = useState(false)
  const [showCredit, setShowCredit] = useState(false)
  const [cancelReason, setCancelReason] = useState("")
  const [allowance, setAllowance] = useState({ amount: "", reason: "", gst_rate: "0", reason_code: "" })
  const [adjustCodes, setAdjustCodes] = useState<{ name: string; description: string }[]>([])
  const [allowanceNote, setAllowanceNote] = useState<string | null>(null)
  const [allowanceKey, setAllowanceKey] = useState(0)
  // a manager's own allowance is signed with their PIN, asked when needed
  const [allowancePin, setAllowancePin] = useState<string | null>(null)
  const [panel, setPanel] = useState<"company" | "share" | null>(null)
  const [shares, setShares] = useState<Record<string, string>>({})
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  // Correct charged nights: Finance / Hotel Admin, when the property's
  // rate-change policy allows it (the dry run refuses otherwise)
  const { roles } = useAuth()
  const canRequestCorrection = roles.some(r => ["Front Desk","Finance","Hotel Admin","System Manager","Administrator"].includes(r))
  const canCorrect = roles.some((r) =>
    ["Finance", "Hotel Admin", "System Manager", "Administrator"].includes(r))
  // who may correct an issued invoice - a credit note, or the accounting
  // system's credit and re-bill: the server's INVOICE_CORRECTION_ROLES
  const canCreditInvoice = roles.some((r) =>
    ["Finance", "Hotel Admin", "System Manager", "Administrator"].includes(r))
  const [toCorrect, setToCorrect] = useState<
    { date: string; charged: number; new: number }[]>([])
  const [correction, setCorrection] = useState({ reason: "", pin: "" })

  const load = useCallback(() => {
    if (name)
      call<InvoiceData>("kamra.api.folio_invoice", { folio: name })
        .then((d) => {
          setData(d)
          setPayment((p) => ({
            ...p, amount: p.currency && p.currency !== d.property.currency ? "" : String(d.folio.balance || ""),
          }))
          return d.stay.reservation
            ? call<SiblingFolio[]>("kamra.api.reservation_folios", {
                reservation: d.stay.reservation,
              })
            : []
        })
        .then((s) => s && setSiblings(s))
        .catch((e) => setError(serverError(e)))
  }, [name])

  useEffect(load, [load])
  useEffect(() => {
    call<{ name: string; description: string }[]>("kamra.ledger.list_reason_codes", { category: "Adjust" })
      .then(setAdjustCodes).catch(() => {})
  }, [])

  const balanceNow = data?.folio.balance
  useEffect(() => {
    if (!showIn || !name) return setShown(null)
    call<NonNullable<typeof shown>>("kamra.ledger.folio_in_currency", { folio: name, currency: showIn })
      .then(setShown)
      .catch((e) => { setShown(null); setError(serverError(e)) })
  }, [showIn, name, balanceNow])

  const stayRes = data?.stay.reservation
  const grand = data?.folio.grand_total
  useEffect(() => {
    if (!canCorrect || !stayRes) return
    call<{ nights: { date: string; charged: number; new: number }[] }>(
      "kamra.api.correct_charged_nights", { reservation: stayRes, dry_run: 1 })
      .then((r) => setToCorrect(r.nights))
      .catch(() => setToCorrect([]))  // policy off or nothing charged
  }, [canCorrect, stayRes, grand])

  const [rates, setRates] = useState<number[]>(taxRates())
  useEffect(() => {
    loadLocale().then((l) => {
      setRates(l.tax_rates)
      // start on the property's rate once it is known, unless the desk picked one
      if (!taxPicked.current)
        setCharge((c) => ({ ...c, gst_rate: defaultChargeTaxRate(l.tax_rates, l.services_tax_rate) }))
    })
  }, [])
  // a hotel that charges no tax (its only rate is 0%) gets no tax pickers
  const noTax = rates.length === 1 && Number(rates[0]) === 0
  // the rate the server will put on this charge when it is food & beverage
  // (same words and rate as kamra.api._resolve_charge_gst); null = the
  // picked rate applies
  const fnbWords = loc.fnb_charge_words ?? []
  const chargeText = `${charge.charge_type} ${charge.description}`.toLowerCase()
  const fixedRate: number | null =
    !charge.is_alcohol && loc.fnb_tax_rate != null && fnbWords.some((w) => chargeText.includes(w))
      ? Number(loc.fnb_tax_rate)
      : null

  /** Params helper kept for call sites; unlock happens in act(). */
  const withPin = (params: Record<string, unknown>) => params

  async function act(fn: () => Promise<unknown>, onError: (message: string) => void = setError) {
    setBusy(true)
    setError(null)
    try {
      if (pinStatus?.required) await ensureUnlocked()
      await fn()
      load()
    } catch (e) {
      onError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  function removeFolio(target: SiblingFolio) {
    act(async () => {
      await call("kamra.api.delete_folio", { folio: target.name })
      if (target.name === name) {
        const guest = siblings.find((s) => s.folio_type === "Guest")
        navigate(
          guest ? `/billing/${encodeURIComponent(guest.name)}` : "/billing",
        )
      }
    })
  }

  /** OPERA advance billing: post the stay's coming nights now, so a guest who
   *  pays at check-in can be discounted and receipted before night audit. */
  function advanceBill() {
    if (!stayRes) return
    act(() => call("kamra.ledger.force_advance_bill", withPin({ reservation: stayRes, nights: "entire" })))
  }

  // a link to /billing/<folio>?action=advance-bill (the discount hint, a chat
  // hand-off) brings the desk to the Advance bill button; the cashier presses it
  const wantsAdvanceBill = searchParams.get("action") === ADVANCE_BILL_ACTION
  const focusedAdvanceBill = useRef(false)
  useEffect(() => {
    if (!wantsAdvanceBill || !data || focusedAdvanceBill.current) return
    const button = document.getElementById(ADVANCE_BILL_ACTION)
    if (!button) return
    focusedAdvanceBill.current = true
    button.scrollIntoView({ block: "center" })
    button.focus()
  }, [wantsAdvanceBill, data])

  if (!data)
    return <p className="py-10 text-center text-sm text-zinc-400">{t("Loading…")}</p>

  const { folio, property, stay, gst_summary } = data
  const doc = data.document
  // the server sends charges enriched with their service code; fall back
  // to the raw folio rows so a cached older payload still renders
  const lines = data.lines ?? folio.charges.map((c) => ({ ...c, row: c.name, qty: 1, rate: c.amount, service_code: null }))
  const taxRows = data.tax_summary ?? []
  // a bill with no tax on any line prints no tax columns or totals
  const taxed = Number(folio.tax_total) !== 0 ||
    folio.charges.some((c) => Number(c.gst_rate) !== 0 || Number(c.gst_amount) !== 0)
  const open = folio.status === "Open"
  // invoiced here, or by the accounting system: its check-out close sets only
  // its own invoice (sales_invoice), and that bill is corrected all the same
  const invoiced = !!(folio.invoice_number || folio.sales_invoice)

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 print:hidden">
        <Link
          to="/billing"
          className="inline-flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-800"
        >
          <ArrowLeft className="size-4" aria-hidden />
          {t("Billing")}
        </Link>
        <div className="flex max-w-full flex-wrap gap-2">
          {siblings.length > 1 && (
            <div className="flex flex-wrap items-center gap-1 rounded-lg bg-zinc-100 p-1">
              {siblings.map((s) => {
                const active = s.name === folio.name
                const empty = isEmptyFolio(s)
                const canDelete =
                  empty && s.folio_type !== "Guest" && s.folio_type !== "Group"
                return (
                  <div
                    key={s.name}
                    className={
                      "group flex items-center rounded-md " +
                      (active ? "bg-white shadow-sm" : "")
                    }
                  >
                    <Link
                      to={`/billing/${encodeURIComponent(s.name)}`}
                      className={
                        "flex items-center gap-1.5 px-2.5 py-1 text-xs " +
                        (active
                          ? "font-medium text-zinc-800"
                          : "text-zinc-500 hover:text-zinc-800")
                      }
                    >
                      {folioLabel(siblings, s, t)}
                      <span
                        className={
                          "tabular-nums " +
                          (s.balance > 0 ? "text-amber-600" : "text-zinc-400")
                        }
                      >
                        {s.balance < 0 ? `${t("Advance")} ` : ""}{cur()}{inr(Math.abs(s.balance))}
                      </span>
                    </Link>
                    {canDelete && (
                      <button
                        aria-label={t("Delete empty {label} folio", { label: folioLabel(siblings, s, t) })}
                        title={t("Delete this empty folio")}
                        disabled={busy}
                        onClick={() => removeFolio(s)}
                        className="pr-1.5 text-zinc-300 hover:text-rose-500"
                      >
                        <X className="size-3.5" />
                      </button>
                    )}
                  </div>
                )
              })}
            </div>
          )}
          {open && folio.balance > 0 && loc.integrations.payment_gateways.length > 0 && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                act(async () => {
                  const r = await call<{ url: string }>(
                    "kamra.api.folio_payment_link",
                    { folio: folio.name },
                  )
                  navigator.clipboard.writeText(r.url)
                })
              }
              title={translate("Creates a payment link for the balance and copies it - send to the {v0}", { v0: String(data.stay.contact_preference === "Booker" &&
                data.stay.booked_by_name
                  ? `booker, ${data.stay.booked_by_name}${data.stay.booked_by_phone ? ` (${data.stay.booked_by_phone})` : ""}`
                  : data.stay.contact_preference === "Both" &&
                      data.stay.booked_by_name
                    ? `guest and the booker (${data.stay.booked_by_name})`
                    : "guest") })}
            >
              {t("Payment link")}
            </Button>
          )}
          {open && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() =>
                act(() =>
                  call("kamra.api.split_folio", {
                    reservation: data.stay.reservation,
                    folio_type: siblings.some((s) => s.folio_type === "Company")
                      ? "Extra"
                      : "Company",
                  }),
                )
              }
            >
              {t("Split folio")}
            </Button>
          )}
          {open && siblings.find((s) => s.folio_type === "Guest" && s.status === "Open")?.name === folio.name && (
            <Button
              variant="outline"
              disabled={busy}
              title={t("A separate bill for each guest sharing the room, in their own name")}
              onClick={() =>
                act(async () => {
                  await call("kamra.api.open_guest_bills", { reservation: data.stay.reservation })
                  setPanel("share")
                })
              }
            >
              {t("Bill each guest")}
            </Button>
          )}
          {open && data.stay.reservation && (
            <Button variant="outline" disabled={busy}
              onClick={() => setPanel(panel === "company" ? null : "company")}>
              {t("Change company")}
            </Button>
          )}
          {open &&
            data.stay.group_booking &&
            !siblings.some((s) => s.folio_type === "Group") && (
              <Button
                variant="outline"
                disabled={busy}
                title={t("One consolidated company bill across every room of the group")}
                onClick={() =>
                  act(() =>
                    call("kamra.api.group_master_folio", {
                      group_booking: data.stay.group_booking,
                    }),
                  )
                }
              >
                {t("Group folio")}
              </Button>
            )}
          <Button variant="outline" disabled={busy} onClick={() =>
              act(async () => {
                await call("kamra.ledger.generate_proforma", { folio: folio.name })
                window.print()
              })
            }>
            {t("Proforma")}
          </Button>
          {open && stay?.reservation ? (
            <Button variant="outline" disabled={busy} id={ADVANCE_BILL_ACTION}
              title={t("Post future room & tax ahead of night audit")}
              onClick={advanceBill}>
              {t("Advance bill")}
            </Button>
          ) : null}
          {!open && invoiced && canCreditInvoice ? (
            <Button variant="outline" disabled={busy} onClick={() => setShowCredit(true)}>
              {t("Credit note")}
            </Button>
          ) : null}
          <Button variant="outline" onClick={() => {
            void call("kamra.ledger.record_folio_reprint", {
              folio: folio.name,
              document: folio.invoice_number ? "Guest Folio" : "Proforma",
            }).finally(() => window.print())
          }}>
            <Printer className="size-4" aria-hidden />
            {folio.invoice_number ? t("Print invoice") : t("Print folio")}
          </Button>
          {open &&
            folio.balance === 0 &&
            folio.charges.length > 0 && (
              <Button
                variant="outline"
                disabled={busy}
                title={translate("Interim invoice: freeze this paid folio and keep the stay running on a fresh one - for long stays settled every few days")}
                onClick={() =>
                  act(async () => {
                    const r = await call<{ new_folio: string }>(
                      "kamra.api.part_settle_folio",
                      withPin({ folio: folio.name }),
                    )
                    navigate(`/billing/${encodeURIComponent(r.new_folio)}`)
                  })
                }
              >
                {t("Settle & continue stay")}
              </Button>
            )}
          {!open && invoiced && folio.folio_type !== "Account"
            && (!folio.sales_invoice || canCreditInvoice) && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setShowCancel((s) => !s)}
            >
              {t("Cancel invoice")}
            </Button>
          )}
          {open && (
            <Button
              disabled={busy}
              onClick={() =>
                act(() => call("kamra.api.close_folio", withPin({ folio: folio.name })))
              }
            >
              {t("Close & generate invoice")}
            </Button>
          )}
        </div>
      </div>

      {showCredit && !open && invoiced && (
        <InvoiceCorrectionDialog folio={folio} credited={data.credited ?? 0}
          onClose={() => setShowCredit(false)}
          onDone={() => { setShowCredit(false); load() }} />
      )}

      {showCancel && !open && (
        <div className="mb-4 flex flex-wrap items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-4 py-2.5 text-sm print:hidden">
          <span className="text-amber-800">
            {folio.sales_invoice
              ? t("Cancel {invoice}? The accounting system credits it and issues a new invoice; this bill is not reopened here.", {
                  invoice: folio.invoice_number || folio.sales_invoice,
                })
              : t("Cancel {invoice}? The number goes on the cancelled register and the folio reopens for correction.", {
                  invoice: folio.invoice_number ?? "",
                })}
          </span>
          <input
            className="min-w-56 flex-1 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-sm"
            placeholder={t("Reason (required - goes on the record)")}
            value={cancelReason}
            onChange={(e) => setCancelReason(e.target.value)}
          />
          <Button
            variant="outline"
            disabled={busy || !cancelReason.trim()}
            onClick={() =>
              act(async () => {
                await call("kamra.api.cancel_invoice", withPin({
                  folio: folio.name,
                  reason: cancelReason.trim(),
                }))
                setShowCancel(false)
                setCancelReason("")
              })
            }
          >
            {t("Confirm cancel")}
          </Button>
        </div>
      )}

      {stay.reservation && (
        <div className="mb-4 print:hidden">
          <LinkedRecords doctype="Reservation" name={stay.reservation} />
        </div>
      )}

      {pinStatus?.required ? (
        <div className="mb-4 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-2 text-sm print:hidden">
          <span className="text-zinc-600">
            {pinStatus.unlocked
              ? t("Cashier unlocked - money actions are open for {n} minutes.", { n: pinStatus.unlock_minutes ?? 15 })
              : pinStatus.must_reset || !pinStatus.has_pin
                ? t("Set your cashier PIN to post payments and settle folios.")
                : t("Cashier PIN required for money actions.")}
          </span>
          {!pinStatus.unlocked ? (
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => void ensureUnlocked().catch((e) => setError(serverError(e)))}
            >
              {pinStatus.must_reset || !pinStatus.has_pin ? t("Set PIN") : t("Unlock")}
            </Button>
          ) : null}
        </div>
      ) : null}
      {error && (
        <div className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-4 py-2 text-sm text-rose-700 print:hidden">
          {translate(error)}
        </div>
      )}
      {panel === "company" && data.stay.reservation && (
        <div className="mb-4 print:hidden">
          <ChangeCompany reservation={data.stay.reservation} current={data.stay.company}
            onCancel={() => setPanel(null)}
            onDone={() => { setPanel(null); load() }} />
        </div>
      )}
      {panel === "share" && open && (() => {
        const bills = siblings.filter((s) => s.bill_to_name && s.status === "Open" && s.name !== folio.name)
        const rows = selected.size
          ? folio.charges.filter((c) => selected.has(c.name))
          : folio.charges.filter((c) => ["Room", "Meal Plan", "Room Levy"].includes(c.charge_type) && c.amount > 0)
        const even = (100 / (bills.length + 1)).toFixed(2)
        const pct = (f: string) => shares[f] ?? even
        const total = bills.reduce((n, b) => n + Number(pct(b.name) || 0), 0)
        return (
          <div className="mb-4 space-y-2 rounded-lg border border-zinc-200 bg-white p-3 text-sm print:hidden">
            <p className="font-medium text-zinc-800">{t("Share charges with the other guests' bills")}</p>
            {bills.length === 0 ? (
              <p className="text-zinc-500">{t("Add the other guests' names to the stay, then press Bill each guest.")}</p>
            ) : (
              <>
                <p className="text-xs text-zinc-500">
                  {selected.size
                    ? t("{n} ticked line(s) are shared.", { n: rows.length })
                    : t("{n} room line(s) are shared. Tick lines on the bill to share others.", { n: rows.length })}
                </p>
                {bills.map((b) => (
                  <label key={b.name} className="flex items-center gap-2">
                    <span className="w-40 truncate">{b.bill_to_name}</span>
                    <input className={`${inputCls} w-20`} type="number" min={0} max={100}
                      value={pct(b.name)} onChange={(e) => setShares({ ...shares, [b.name]: e.target.value })} />
                    <span className="text-zinc-500">%</span>
                  </label>
                ))}
                <p className="text-xs text-zinc-500">
                  {t("This bill keeps {pct}%.", { pct: (100 - total).toFixed(2) })}
                </p>
                <div className="flex gap-2">
                  <Button variant="outline" disabled={busy || !rows.length || total >= 100}
                    onClick={() =>
                      act(async () => {
                        await call("kamra.api.share_folio_charges", {
                          from_folio: folio.name,
                          charge_rows: rows.map((c) => c.name),
                          to_folios: bills.map((b) => b.name),
                          shares: bills.map((b) => Number(pct(b.name))),
                        })
                        setSelected(new Set())
                        setPanel(null)
                      })
                    }>
                    {t("Share these charges")}
                  </Button>
                  <Button variant="ghost" onClick={() => setPanel(null)}>{t("Cancel")}</Button>
                </div>
              </>
            )}
          </div>
        )
      })()}

      {/* printable document */}
      <Card className="print:border-0 print:shadow-none">
        <CardContent className="py-6">
          <div className="mb-4 flex items-center justify-center gap-3">
            <span
              className={
                "rounded-full px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.18em] " +
                (doc?.is_final
                  ? "bg-zinc-900 text-white"
                  : "border border-dashed border-amber-400 bg-amber-50 text-amber-700")
              }
            >
              {translate(doc?.title) ?? (folio.invoice_number ? t("Invoice") : t("Provisional Bill"))}
              {doc?.title_local && (
                <>
                  <span aria-hidden className="mx-2 opacity-50">·</span>
                  <span dir="rtl" lang="ar" className="normal-case tracking-normal">
                    {doc.title_local}
                  </span>
                </>
              )}
            </span>
          </div>
          <div className="mb-6 flex flex-wrap items-start justify-between gap-4 border-b border-zinc-200 pb-5">
            <div className="flex items-start gap-3">
              {property.logo_url && (
                <img
                  src={property.logo_url}
                  alt=""
                  className="h-12 w-12 shrink-0 rounded object-contain"
                  onError={(e) => {
                    ;(e.target as HTMLImageElement).style.display = "none"
                  }}
                />
              )}
              <div>
                <h1 className="text-xl font-semibold">
                  {property.legal_name || property.name}
                </h1>
                {property.legal_name &&
                  property.legal_name !== property.name && (
                    <p className="text-sm text-zinc-500">{property.name}</p>
                  )}
                <p className="text-sm text-zinc-500">{property.address}</p>
                <p className="text-sm text-zinc-500">
                  {property.gstin && (
                    <>
                      {translate(property.tax_id_label ?? "Tax ID")}:{" "}
                      <span className="font-medium">{property.gstin}</span>{" "}
                      ·{" "}
                    </>
                  )}
                  {property.phone}
                </p>
              </div>
            </div>
            <div className="text-right">
              <p className="font-mono text-lg font-semibold">
                {doc?.number ?? folio.invoice_number ?? folio.name}
              </p>
              <p className="text-sm text-zinc-500">
                {doc?.date ?? ""}
              </p>
              {property.place_of_supply && (
                <p className="mt-1 text-xs text-zinc-500">
                  {t("Place of supply")}: {property.place_of_supply}
                </p>
              )}
              {!doc?.is_final && (
                <p className="mt-1 max-w-52 text-xs text-amber-700">
                  {t("Not a tax invoice yet - the number is issued when the folio is settled.")}
                </p>
              )}
              <Badge tone={open ? "amber" : "green"}>{t(folio.status)}</Badge>
            </div>
          </div>

          {data.bill_to && (
            <div className="mb-4 rounded-lg bg-zinc-50 px-4 py-2.5 text-sm">
              <span className="text-zinc-500">{t("Bill to")}: </span>
              <span className="font-medium">{data.bill_to.name}</span>
              {data.bill_to.gstin && (
                <span className="text-zinc-500">{" · "}{translate(property.tax_id_label ?? "Tax ID")}{" "}{data.bill_to.gstin}</span>
              )}
              {data.bill_to.gstin && loc.integrations.verify_tax_id && (
                <VerifyTaxId taxId={data.bill_to.gstin} name={data.bill_to.name} />
              )}
            </div>
          )}
          {/* The facts a bill is expected to state - who, which room, how
              many people, on what plan, and for a foreign guest their
              nationality (the same field the police report needs). */}
          {stay.tariff && <p className="mb-3 text-sm text-zinc-600">
            {t("Booked contract tariff")}: <TariffNight {...stay} />
          </p>}
          <dl className="mb-6 grid gap-x-6 gap-y-1 text-sm sm:grid-cols-2">
            <Fact label={t("Guest")}>
              <span className="font-medium">
                {data.guest?.name ?? folio.guest_name}
              </span>
              {stay.company && (
                <span className="text-zinc-500"> · {stay.company}</span>
              )}
            </Fact>
            {stay.reservation && (<>
            <Fact label={t("Room")}>
              {roomLabel(stay)}
              {stay.room_type && (
                <span className="text-zinc-500">
                  {" "}
                  · {stay.room_type.split("-").pop()}
                </span>
              )}
            </Fact>
            <Fact label={t("Stay")}>
              {stay.check_in} → {stay.check_out} · {t("{n} night{s}", {
                n: stay.nights,
                s: stay.nights === 1 ? "" : "s",
              })}
            </Fact>
            <Fact label={t("Pax / plan")}>
              {stay.pax || stay.adults || "-"}
              {stay.children ? ` (${stay.adults}+${stay.children})` : ""}
              {stay.meal_plan && (
                <span className="text-zinc-500">
                  {" "}
                  · {stay.meal_plan.split("-").pop()}
                </span>
              )}
            </Fact>
            {(stay.arrival || stay.departure) && (
              <Fact label={t("In / out")}>
                {(stay.arrival ?? "").slice(0, 16).replace("T", " ") || "-"}
                {" → "}
                {(stay.departure ?? "").slice(0, 16).replace("T", " ") || "-"}
              </Fact>
            )}
            </>)}
            {data.guest?.guest_id && (
              <Fact label={t("Nationality")}>
                <EditableNationality
                  guestId={data.guest.guest_id}
                  value={data.guest.nationality}
                  onSaved={(nationality) =>
                    setData((d) =>
                      d
                        ? {
                            ...d,
                            guest: { ...d.guest, nationality },
                          }
                        : d,
                    )
                  }
                />
              </Fact>
            )}
            {data.guest?.address && (
              <Fact label={t("Address")}>{data.guest.address}</Fact>
            )}
          </dl>

          {(() => {
            const targets = siblings.filter(
              (s) => s.status === "Open" && s.name !== folio.name,
            )
            // moving is always possible on an open folio - the Move panel
            // can mint a fresh Extra/Company folio as the destination
            const editable = open // charges can be voided on any open folio
            return (
              <>
                {open && selected.size > 0 && (() => {
                  const sel = folio.charges.filter((c) => selected.has(c.name))
                  const selTotal = sel.reduce((s, c) => s + (c.total || 0), 0)
                  const one = sel.length === 1 ? sel[0] : null
                  const canSplit =
                    !!one && !one.source_ref && one.charge_type !== "Meal Plan" && (one.amount || 0) > 0
                  const v = partVal.trim()
                  const isPct = v.endsWith("%")
                  const num = Number(v.replace("%", ""))
                  const partOk =
                    canSplit && !!v && num > 0 &&
                    (isPct ? num < 100 : num < (one!.amount || 0))
                  const partBad = canSplit && !!v && !partOk
                  const movedTotal = !partOk
                    ? selTotal
                    : isPct
                      ? ((one!.total || 0) * num) / 100
                      : num * (1 + (one!.gst_rate || 0) / 100)
                  const moveTo = (to: string) =>
                    act(async () => {
                      if (one && partOk) {
                        await call("kamra.api.split_folio_charge", {
                          from_folio: folio.name,
                          charge_row: one.name,
                          to_folio: to,
                          percent: isPct ? num : null,
                          amount: isPct ? null : num,
                        })
                      } else {
                        await call("kamra.api.transfer_folio_charges", {
                          from_folio: folio.name,
                          charge_rows: [...selected],
                          to_folio: to,
                        })
                      }
                      setSelected(new Set())
                      setPartVal("")
                    })
                  const moveToNew = (type: "Extra" | "Company") =>
                    act(async () => {
                      const r = await call<{ folio: string }>(
                        "kamra.api.split_folio",
                        { reservation: data.stay.reservation, folio_type: type },
                      )
                      if (one && partOk) {
                        await call("kamra.api.split_folio_charge", {
                          from_folio: folio.name,
                          charge_row: one.name,
                          to_folio: r.folio,
                          percent: isPct ? num : null,
                          amount: isPct ? null : num,
                        })
                      } else {
                        await call("kamra.api.transfer_folio_charges", {
                          from_folio: folio.name,
                          charge_rows: [...selected],
                          to_folio: r.folio,
                        })
                      }
                      setSelected(new Set())
                      setPartVal("")
                    })
                  return (
                    <div className="mb-3 rounded-xl border border-brand-200 bg-brand-50/40 p-3 text-sm print:hidden">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">
                          {t("{n} line{s} · {cur}{amount} selected", {
                            n: sel.length,
                            s: sel.length > 1 ? "s" : "",
                            cur: cur(),
                            amount: inr(selTotal),
                          })}
                        </span>
                        {canSplit ? (
                          <span className="inline-flex items-center gap-1.5 text-xs text-zinc-600">
                            <span>· {t("move only")}</span>
                            <input
                              className="w-24 rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs"
                              aria-label={t("Part to move (percent, or {cur} before {tax})", { cur: cur(), tax: taxLabel() })}
                              placeholder={t("30% or 1500")}
                              value={partVal}
                              onChange={(e) => setPartVal(e.target.value)}
                            />
                            <span className="text-zinc-400">
                              ({cur()} = {t("before {tax}", { tax: taxLabel() })})
                            </span>
                          </span>
                        ) : one ? (
                          <span className="text-xs text-zinc-500">
                            · {t("Moves whole (cannot be split)")}
                          </span>
                        ) : null}
                        <button
                          className="ml-auto text-xs text-zinc-400 hover:text-zinc-700"
                          onClick={() => {
                            setSelected(new Set())
                            setPartVal("")
                          }}
                        >
                          {t("Clear")}
                        </button>
                      </div>
                      {partBad && (
                        <p className="mt-1.5 text-xs text-rose-600">
                          {t("Enter 1-99%, or a {cur} amount under the line's {cur}{amount} (before {tax}).", {
                            cur: cur(),
                            amount: inr(one!.amount),
                            tax: taxLabel(),
                          })}
                        </p>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        <span className="text-xs text-zinc-500">
                          {t("Send {cur}{amount} to", { cur: cur(), amount: inr(movedTotal) })}
                        </span>
                        {targets.map((s) => (
                          <button
                            key={s.name}
                            className="rounded-lg border border-zinc-200 bg-white px-2.5 py-1.5 text-xs hover:border-brand-400 disabled:opacity-50"
                            disabled={busy || partBad}
                            onClick={() => moveTo(s.name)}
                          >
                            <span className="font-medium">
                              {folioLabel(siblings, s, t)}
                            </span>
                            <span
                              className={
                                s.balance > 0
                                  ? "ml-1.5 text-amber-600"
                                  : "ml-1.5 text-zinc-400"
                              }
                            >
                              {t("owes {cur}{amount}", { cur: cur(), amount: inr(s.balance) })}
                            </span>
                          </button>
                        ))}
                        <button
                          className="rounded-lg border border-dashed border-zinc-300 bg-white px-2.5 py-1.5 text-xs text-zinc-600 hover:border-brand-400 disabled:opacity-50"
                          disabled={busy || partBad}
                          onClick={() => moveToNew("Company")}
                        >
                          {t("+ New Company folio")}
                        </button>
                        <button
                          className="rounded-lg border border-dashed border-zinc-300 bg-white px-2.5 py-1.5 text-xs text-zinc-600 hover:border-brand-400 disabled:opacity-50"
                          disabled={busy || partBad}
                          onClick={() => moveToNew("Extra")}
                        >
                          {t("+ New Extra folio")}
                        </button>
                      </div>
                    </div>
                  )
                })()}
                <ResponsiveList className="mb-5 w-full text-sm">
                  <thead>
                    <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                      {open && (
                        <th className="w-6 py-2 pr-2 print:hidden" aria-label={t("Select")} />
                      )}
                      <th className="py-2 pr-3">{t("Date")}</th>
                      <th className="py-2 pr-3">{t("Item")}</th>
                      <th className="hidden py-2 pr-3 print:table-cell">
                        {doc?.service_code_label ?? t("SAC")}
                      </th>
                      <th className="py-2 pr-3 text-right">{t("Amount {cur}", { cur: cur() })}</th>
                      {taxed && <th className="py-2 pr-3 text-right">{taxLabel()} %</th>}
                      {taxed && <th className="py-2 pr-3 text-right">{taxLabel()} {cur()}</th>}
                      <th className="py-2 text-right">{t("Total {cur}", { cur: cur() })}</th>
                      {editable && (
                        <th className="py-2 pl-3 print:hidden" aria-label={t("Actions")} />
                      )}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100">
                    {folio.charges.map((c, i) => {
                      // a voided line and its reversal stay on the bill; neither is voided or moved again
                      const voided = !!c.reversal_of || folio.charges.some((r) => r.reversal_of === c.name)
                      // a split's line and its reversal: the trail of the split, shown as
                      // such here and left off the printed bill, which shows the parts
                      const split = voided && (c.reversal_of
                        ? !!c.split_of
                        : folio.charges.some((r) => r.reversal_of === c.name && !!r.split_of))
                      return (
                      <Fragment key={c.name ?? i}>
                        <tr className={[voided && "text-zinc-400", split && "print:hidden"]
                          .filter(Boolean).join(" ") || undefined}>
                          {open && (
                            <Popin label={t("Select")} importance="medium" className="py-2 pr-2 print:hidden">
                              {!voided && <input
                                type="checkbox"
                                className="size-3.5 accent-brand-600"
                                aria-label={t("Select charge")}
                                checked={selected.has(c.name)}
                                onChange={(e) =>
                                  setSelected((prev) => {
                                    const next = new Set(prev)
                                    if (e.target.checked) next.add(c.name)
                                    else next.delete(c.name)
                                    return next
                                  })
                                }
                              />}
                            </Popin>
                          )}
                          <Popin label={t("Date")} importance="medium" className="py-2 pr-3 text-zinc-500">{formatDate(c.posting_date)}</Popin>
                          <Popin label={t("Item")} importance="high" className="py-2 pr-3">
                            {/* on the guest's bill an allowance reads as a discount */}
                            <span className="font-medium">{translate(c.charge_type === "Allowance" ? "Discount" : c.charge_type)}</span>
                            {c.description && (
                              <span className="text-zinc-500"> - {c.description}</span>
                            )}
                            {voided && (
                              <span className="ml-1 rounded bg-zinc-100 px-1 text-xs text-zinc-500">
                                {split ? t("status|Split") : t("Voided")}
                              </span>
                            )}
                            {c.allowance_serial && (
                              <Link to={`/allowance-voucher/${encodeURIComponent(c.allowance_serial)}`}
                                className="ml-1 font-mono text-xs text-brand-700 hover:underline print:text-zinc-500 print:no-underline"
                                title={t("Print voucher")}>
                                {c.allowance_serial}
                              </Link>
                            )}
                          </Popin>
                          <td className="hidden py-2 pr-3 font-mono text-xs text-zinc-400 print:table-cell">
                            {lines.find((l) => l.row === c.name)?.service_code ?? ""}
                          </td>
                          <Popin label={t("Amount")} importance="medium" className="py-2 pr-3 text-right">{inr(c.amount)}</Popin>
                          {taxed && <Popin label={`${taxLabel()} %`} importance="medium" className="py-2 pr-3 text-right">{c.gst_rate}%</Popin>}
                          {taxed && <Popin label={taxLabel()} importance="medium" className="py-2 pr-3 text-right">{inr(c.gst_amount)}</Popin>}
                          <Popin label={t("Total")} importance="medium" className="py-2 text-right font-medium">{inr(c.total)}</Popin>
                          {editable && (
                            <Popin label={t("Actions")} importance="medium" className="relative whitespace-nowrap py-2 pl-3 text-right print:hidden">
                              {canRequestCorrection && c.charge_type !== "Allowance" && !voided && <RowCorrection folio={folio.name} row={c.name} modes={[]} act={act} />}
                              {open && !voided && !selected.has(c.name) && (
                                <button
                                  aria-label={t("Move or split this charge")}
                                  className="inline-flex items-center gap-1 rounded-md border border-zinc-200 px-2 py-1 text-xs text-zinc-500 hover:border-brand-400 hover:text-zinc-800"
                                  onClick={() =>
                                    setSelected((prev) => new Set(prev).add(c.name))
                                  }
                                >
                                  <ArrowRightLeft className="size-3" aria-hidden />
                                  {t("Move")}
                                </button>
                              )}
                            </Popin>
                          )}
                        </tr>
                      </Fragment>
                      )
                    })}
                  </tbody>
                </ResponsiveList>
              </>
            )
          })()}

          {/* What the guest actually bought, before they read forty lines. */}
          {(data.summary_by_head?.length ?? 0) > 1 && (
            <div className="mb-6 break-inside-avoid rounded-xl bg-zinc-50 px-4 py-3">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                {t("Summary")}
              </h3>
              <table className="w-full text-sm">
                <tbody>
                  {data.summary_by_head.map((h) => (
                    <tr key={h.head}>
                      <td className="py-1">
                        {translate(h.head)}
                        <span className="ml-1.5 text-xs text-zinc-400">
                          {t("{n} line{s}", { n: h.lines, s: h.lines === 1 ? "" : "s" })}
                        </span>
                      </td>
                      <td className="py-1 text-right text-zinc-500">
                        {cur()}{inr(h.amount)}
                      </td>
                      {taxed && (
                        <td className="py-1 text-right text-zinc-400">
                          + {cur()}{inr(h.tax)} {taxLabel().toLowerCase()}
                        </td>
                      )}
                      <td className="py-1 text-right font-medium">
                        {cur()}{inr(h.total)}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <div className="mb-6 grid gap-6 sm:grid-cols-2">
            {taxed ? <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                {t("{tax} summary", { tax: taxLabel() })}
              </h3>
              <table className="w-full text-sm">
                <tbody className="divide-y divide-zinc-100">
                  {(taxRows.length ? taxRows : gst_summary.map((g) => ({
                    rate: g.rate, taxable: g.taxable, total_tax: g.total_tax,
                    parts: [
                      { label: "CGST", rate: g.rate / 2, amount: g.cgst },
                      { label: "SGST", rate: g.rate / 2, amount: g.sgst },
                    ],
                  }))).map((r) => (
                    <tr key={r.rate}>
                      <td className="py-1.5 pr-3">{r.rate}%</td>
                      <td className="py-1.5 pr-3 text-right text-zinc-500">
                        {t("taxable {cur}{amount}", { cur: cur(), amount: inr(r.taxable) })}
                      </td>
                      <td className="py-1.5 text-right">
                        {r.parts
                          .map((x) => `${x.label} @ ${x.rate}% ${cur()}${inr(x.amount)}`)
                          .join(" · ")}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div> : <div />}
            <div className="space-y-1.5 text-sm sm:text-right">
              <p className="text-zinc-500">
                {t("Charges")}: <span className="text-zinc-900">{cur()}{inr(folio.charges_total)}</span>
              </p>
              {taxed && (
                <p className="text-zinc-500">
                  {taxLabel()}: <span className="text-zinc-900">{cur()}{inr(folio.tax_total)}</span>
                </p>
              )}
              <p className="text-lg font-semibold">
                {t("Grand total")}: {cur()}{inr(folio.grand_total)}
              </p>
              {doc?.amount_in_words && (
                <p className="text-xs italic text-zinc-500">
                  {doc.amount_in_words}
                </p>
              )}
              {fxRates.length > 0 && (
                <label className="flex items-center justify-end gap-2 text-xs text-zinc-500 print:hidden">
                  {t("Show in")}
                  <select className="rounded border border-zinc-200 px-1.5 py-0.5"
                    value={showIn} onChange={(e) => setShowIn(e.target.value)}>
                    <option value="">{t("Hotel currency only")}</option>
                    {fxRates.map((r) => <option key={r.currency}>{r.currency}</option>)}
                  </select>
                </label>
              )}
              {shown && (
                <p className="text-xs text-zinc-600">
                  {t("Approx. {cur} {total} (balance {cur} {balance}) at {rate}, rate of {date}. Tax invoice amounts are in {home}.", {
                    cur: shown.currency, total: inr(shown.grand_total), balance: inr(shown.balance),
                    rate: shown.rate, date: shown.rate_date, home: shown.home_currency,
                  })}
                </p>
              )}
              {data.statutory && (
                <div className="flex items-center justify-end gap-3 pt-2">
                  <div className="text-right text-[10px] leading-tight text-zinc-400">
                    <p>{translate("ZATCA · ICV ")}{data.statutory.icv}</p>
                    <p className="font-mono">{data.statutory.uuid}</p>
                  </div>
                  <img
                    src={data.statutory.qr_image}
                    alt={t("E-invoice QR code")}
                    className="size-28 shrink-0"
                  />
                </div>
              )}
              <p className="text-zinc-500">
                {t("Paid")}: {cur()}{inr(folio.payments_total)} ·{" "}
                {/* paid more than charged so far: that money is the guest's advance */}
                {folio.balance < 0 ? t("Advance") : t("Balance")}:{" "}
                <span
                  className={
                    folio.balance > 0
                      ? "font-medium text-amber-600"
                      : "font-medium text-emerald-600"
                  }
                >
                  {cur()}{inr(Math.abs(folio.balance))}
                </span>
              </p>
            </div>
          </div>

          {folio.payments.length > 0 && (
            <div className="text-sm">
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-zinc-500">
                {t("Payments")}
              </h3>
              <ul className="divide-y divide-zinc-100">
                {folio.payments.map((p) => (
                  <li key={p.name} className="flex flex-wrap justify-between gap-2 py-1.5">
                    <span>
                      {formatDate(p.posting_date)} · {translate(p.mode)}
                      {p.payment_kind && p.payment_kind !== "Payment" && (
                        <span className={"ml-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold " +
                          (p.payment_kind === "Refund" ? "bg-rose-50 text-rose-600"
                            : p.payment_kind === "Security Deposit" ? "bg-violet-50 text-violet-700"
                              : "bg-sky-50 text-sky-700")}>
                          {t(p.payment_kind)}
                        </span>
                      )}
                      {p.reference && (
                        <span className="text-zinc-400"> · {p.reference}</span>
                      )}
                      {!!Number(p.fx_amount) && (
                        <span className="text-zinc-400"> · {currencyText(p.currency!, Number(p.fx_amount), loc.locale)} @ {formatFxRate(Number(p.exchange_rate), loc.locale)}{p.exchange_rate_date && ` · ${formatDate(p.exchange_rate_date)}`}</span>
                      )}
                    </span>
                    <span className={Number(p.amount) < 0 ? "font-medium text-rose-600" : ""}>{property.currency} {Number(p.amount).toLocaleString(undefined,{maximumFractionDigits:2})}</span>
                    {canRequestCorrection && <RowCorrection folio={folio.name} row={p.name}
                      home={home} currency={Number(p.fx_amount) > 0 ? p.currency || home : home}
                      modes={availableTenders.filter(({ mode }) => mode !== p.mode && mode !== "Payment Link")} act={act} />}
                  </li>
                ))}
              </ul>

            </div>
          )}

          {folio.invoice_number && (
            <div className="mt-8 flex items-end justify-between border-t border-zinc-200 pt-4 text-xs text-zinc-500">
              <p className="max-w-md">
                {/* the pack words the footer for its country (GST Act in
                    India, ZATCA simplified invoice in Saudi Arabia…) */}
                {doc?.footer
                  ? t(doc.footer)
                  : t("This is a computer-generated invoice.")}
                {property.gstin && property.tax_note
                  ? ` ${t(property.tax_note)}`
                  : ""}
                {/* the hotel's own lines - CIN, bank details - as typed */}
                {doc?.invoice_footer && (
                  <span className="mt-2 block whitespace-pre-line">
                    {doc.invoice_footer}
                  </span>
                )}
              </p>
              <div className="text-center">
                <div className="mb-1 h-8 w-40 border-b border-zinc-300" />
                {t("For {name}", { name: property.legal_name || property.name })}
                <br />
                {t("Authorised signatory")}
              </div>
            </div>
          )}
        </CardContent>
      </Card>

      {open && (
        <div className="mt-4 grid gap-4 md:grid-cols-2 print:hidden">
          <Card>
            <CardHeader>
              <CardTitle>{t("Post a charge")}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-end gap-2">
              <select
                className={inputCls}
                value={charge.charge_type}
                onChange={(e) =>
                  setCharge({ ...charge, charge_type: e.target.value })
                }
              >
                {CHARGE_TYPES.map((ct) => (
                  <option key={ct} value={ct}>{t(ct)}</option>
                ))}
              </select>
              <input
                className={`${inputCls} flex-1`}
                placeholder={t("Description")}
                value={charge.description}
                onChange={(e) =>
                  setCharge({ ...charge, description: e.target.value })
                }
              />
              <input
                className={`${inputCls} w-24`}
                type="number"
                placeholder={translate(`${cur()}`)}
                value={charge.amount}
                onChange={(e) => setCharge({ ...charge, amount: e.target.value })}
              />
              {noTax ? null : fixedRate !== null ? (
                /* the server taxes food & beverage at the pack's F&B rate
                   whatever is picked (_resolve_charge_gst) - show that rate
                   rather than offer a choice that would be ignored */
                <span
                  className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-1.5 text-sm text-zinc-600"
                  title={t("Food & beverage is taxed at the property's F&B rate.")}
                >
                  {t("{tax} {rate}%", { tax: taxLabel(), rate: fixedRate })}
                  <span className="ml-1 text-xs text-zinc-400">{t("(F&B rate)")}</span>
                </span>
              ) : (
                <select
                  className={inputCls}
                  aria-label={t("{tax} rate", { tax: taxLabel() })}
                  value={charge.gst_rate}
                  onChange={(e) => {
                    taxPicked.current = true
                    setCharge({ ...charge, gst_rate: e.target.value })
                  }}
                >
                  {rates.map((n) => String(n)).map((r) => (
                    <option key={r} value={r}>
                      {t("{tax} {rate}%", { tax: taxLabel(), rate: r })}
                    </option>
                  ))}
                </select>
              )}
              {sellsAlcohol() && (
              <label className="flex items-center gap-1.5 text-sm text-zinc-600">
                <input
                  type="checkbox"
                  className="size-4 accent-brand-600"
                  checked={charge.is_alcohol}
                  onChange={(e) =>
                    setCharge({ ...charge, is_alcohol: e.target.checked })
                  }
                />
                {t("Alcohol")}
              </label>)}
              <Button
                disabled={busy || !charge.amount}
                onClick={() =>
                  act(() =>
                    call("kamra.api.add_folio_charge", {
                      folio: folio.name,
                      ...charge,
                      amount: Number(charge.amount),
                      gst_rate: fixedRate !== null && rates.includes(fixedRate)
                        ? fixedRate
                        : Number(charge.gst_rate),
                      is_alcohol: charge.is_alcohol ? 1 : 0,
                    }),
                  )
                }
              >
                {t("Post")}
              </Button>
              <FolioDiscount folio={folio.name} adjustCodes={adjustCodes} act={act}
                onAdvanceBill={stay?.reservation ? advanceBill : undefined}
                refreshKey={data}
                onPosted={(pending) => {
                  setAllowanceNote(pending
                    ? t("Sent to a manager. It goes on the bill once approved.")
                    : null)
                  setAllowanceKey((k) => k + 1)
                }} />
              <div className="mt-1 w-full border-t border-zinc-100 pt-3">
                <div className="mb-2 text-xs font-medium uppercase tracking-wider text-zinc-400">
                  {t("Pass an allowance")}
                  <span className="ml-1.5 normal-case tracking-normal">
                    {t("- write off part of the bill, with a reason")}
                  </span>
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <input
                    className={`${inputCls} w-24`}
                    type="number"
                    placeholder={translate(`${cur()}`)}
                    value={allowance.amount}
                    onChange={(e) =>
                      setAllowance({ ...allowance, amount: e.target.value })
                    }
                  />
                  {!noTax && (
                  <select
                    className={inputCls}
                    value={allowance.gst_rate}
                    onChange={(e) =>
                      setAllowance({ ...allowance, gst_rate: e.target.value })
                    }
                  >
                    {rates.map((n) => String(n)).map((r) => (
                      <option key={r} value={r}>
                        {t("{tax} {rate}%", { tax: taxLabel(), rate: r })}
                      </option>
                    ))}
                  </select>
                  )}
                  {adjustCodes.length > 0 && (
                    <select
                      className={inputCls}
                      aria-label={t("Allowance reason")}
                      value={allowance.reason_code}
                      onChange={(e) => setAllowance({ ...allowance, reason_code: e.target.value })}
                    >
                      <option value="">{t("Reason…")}</option>
                      {adjustCodes.map((c) => (
                        <option key={c.name} value={c.name}>{translate(c.description)}</option>
                      ))}
                    </select>
                  )}
                  <input
                    className={`${inputCls} min-w-40 flex-1`}
                    placeholder={allowance.reason_code ? t("Details (optional)") : t("Reason (required)")}
                    value={allowance.reason}
                    onChange={(e) =>
                      setAllowance({ ...allowance, reason: e.target.value })
                    }
                  />
                  {allowancePin !== null && (
                    <input
                      className={`${inputCls} w-24`}
                      type="password" inputMode="numeric" autoComplete="off" autoFocus
                      value={allowancePin}
                      onChange={(e) => setAllowancePin(e.target.value.replace(/\D/g, "").slice(0, 8))}
                      placeholder={t("Your PIN")} aria-label={t("Your PIN")}
                    />
                  )}
                  <Button
                    variant="outline"
                    disabled={busy || !allowance.amount || !(allowance.reason.trim() || allowance.reason_code)
                      || allowancePin === ""}
                    onClick={() =>
                      act(async () => {
                        const r = await call<{ pending?: boolean }>("kamra.api.post_allowance", withPin({
                          folio: folio.name,
                          amount: Number(allowance.amount),
                          reason: allowance.reason.trim(),
                          gst_rate: Number(allowance.gst_rate),
                          reason_code: allowance.reason_code || undefined,
                          pin: allowancePin || undefined,
                        })).catch((e) => {
                          // the manager approves their own allowance: ask for their PIN
                          if (serverError(e).includes("SUPERVISOR_PIN_REQUIRED")) {
                            setAllowancePin("")
                            return null
                          }
                          throw e
                        })
                        if (!r) return
                        setAllowancePin(null)
                        setAllowance({ amount: "", reason: "", gst_rate: "0", reason_code: "" })
                        setAllowanceNote(r?.pending
                          ? t("Sent to a manager. It goes on the bill once approved.")
                          : null)
                        setAllowanceKey((k) => k + 1)
                      })
                    }
                  >
                    {t("Allow")}
                  </Button>
                </div>
                {allowancePin !== null && (
                  <p className="mt-2 text-xs text-zinc-600">{t("Your PIN signs this allowance, like the paper voucher.")}</p>
                )}
                {allowanceNote && <p className="mt-2 text-xs text-amber-700">{allowanceNote}</p>}
                <div className="mt-2">
                  <AllowanceRequests property={folio.property ?? getCurrentProperty()} folio={folio.name}
                    refreshKey={allowanceKey} onChanged={load} />
                </div>
              </div>
              {toCorrect.length > 0 && folio.status !== "Closed" && (
                <div className="mt-1 w-full border-t border-zinc-100 pt-3">
                  <div className="mb-1 text-xs font-medium uppercase tracking-wider text-zinc-400">
                    {t("Correct charged nights")}
                  </div>
                  <p className="mb-2 text-xs text-zinc-500">
                    {t("Each night is reversed and posted again at the current rate, dated today.")}
                  </p>
                  <ul className="mb-2 space-y-0.5 text-xs text-zinc-600">
                    {toCorrect.map((n) => (
                      <li key={n.date}>
                        {t("Night of {date}: {from} to {to}", {
                          date: n.date, from: inr(n.charged), to: inr(n.new),
                        })}
                      </li>
                    ))}
                  </ul>
                  <div className="flex flex-wrap items-end gap-2">
                    <input
                      className={`${inputCls} min-w-40 flex-1`}
                      placeholder={t("Reason (required)")}
                      value={correction.reason}
                      onChange={(e) => setCorrection({ ...correction, reason: e.target.value })}
                    />
                    <input
                      className={`${inputCls} w-28`}
                      type="password"
                      inputMode="numeric"
                      autoComplete="off"
                      placeholder={t("Your PIN")}
                      value={correction.pin}
                      onChange={(e) => setCorrection({ ...correction, pin: e.target.value })}
                    />
                    <Button
                      variant="outline"
                      disabled={busy || !correction.reason.trim() || !correction.pin.trim()}
                      onClick={() =>
                        act(async () => {
                          await call("kamra.api.correct_charged_nights", {
                            reservation: stay.reservation,
                            reason: correction.reason.trim(),
                            pin: correction.pin.trim(),
                          })
                          setCorrection({ reason: "", pin: "" })
                        })
                      }
                    >
                      {t("Correct")}
                    </Button>
                  </div>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>{t("Record a payment")}</CardTitle>
            </CardHeader>
            <CardContent className="flex flex-wrap items-end gap-2">
              <select
                className={inputCls}
                value={payment.kind}
                title={t("What this money is: against the bill, an advance, or a refundable deposit")}
                onChange={(e) => setPayment({ ...payment, kind: e.target.value })}
              >
                {["Payment", "Advance", ...(foreign ? [] : ["Security Deposit"])].map((k) => (
                  <option key={k} value={k}>{t(k)}</option>
                ))}
              </select>
              <TenderPicker home={home} chosen={tender} modes={paymentTenders} rates={settlementRates}
                onChange={(choice) => setPayment({
                  ...payment, ...choice,
                  amount: choice.currency === tender.currency ? payment.amount : tenderAmount(folio.balance, choice.currency, home, settlementRates),
                })} />
              <TenderInput currency={tender.currency} value={payment.amount}
                full={tenderAmount(folio.balance, tender.currency, home, settlementRates)}
                onChange={amount => setPayment({ ...payment, amount })} />
              <input
                className={`${inputCls} flex-1`}
                placeholder={t("Reference (optional)")}
                value={payment.reference}
                onChange={(e) =>
                  setPayment({ ...payment, reference: e.target.value })
                }
              />
              {foreign && <SettlementPreview result={quote} amount={Number(payment.amount)} balance={folio.balance} />}
              <Button
                disabled={busy || !paymentReady || !(Number(payment.amount) > 0) || (foreign && !quote?.quote)}
                onClick={() =>
                  act(async () => {
                    if (!paymentReady) throw new Error(t("Choose an available payment method and currency."))
                    if (foreign && !quote?.quote) throw new Error(quote?.error || t("Wait for the settlement preview."))
                    await call("kamra.api.add_folio_payment", withPin({
                      folio: folio.name,
                      mode: tender.mode,
                      kind: payment.kind,
                      amount: Number(payment.amount),
                      reference: payment.reference || undefined,
                      currency: tender.currency, quote_id: quote?.quote?.quote_id,
                      idempotency_key: paymentKey,
                    }))
                    setPaymentKey(newIdempotencyKey())
                  })
                }
              >
                {t("Record")}
              </Button>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  )
}
