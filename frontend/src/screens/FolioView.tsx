import { SettlementPreview, useSettlementQuote } from "../components/SettlementPreview"
import TenderPicker, { TenderInput, resolveTender, tenderAmount, tenderModes, tenderReady, tenderTypeOf, useTenderRates } from "../components/TenderPicker"
import { currencyText, formatFxRate, ReferenceCurrencyNote, TariffNight } from "../components/ContractTariff"
import RowCorrection from "../components/RowCorrection"
import { FiscalDocument, type Fiscal } from "../components/FiscalDocument"
import InvoiceCorrectionDialog from "../components/InvoiceCorrectionDialog"
import FolioWindowTabs, { type FolioWindowTabItem } from "../components/FolioWindowTabs"
import RoutingInstructionDrawer from "../components/RoutingInstructionDrawer"
import ReceiptTransferDialog from "../components/ReceiptTransferDialog"
import CreditRebillDialog from "../components/CreditRebillDialog"
import { ResponsiveList, Popin } from "../components/ui/responsive-list"
import { Fragment, useCallback, useEffect, useRef, useState } from "react"
import { ArrowLeft, ArrowRightLeft, Printer } from "lucide-react"
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom"
import { formatDate, roomLabel } from "../lib/utils"
import { call, getCurrentProperty, newIdempotencyKey, type ReferenceCurrency, type TariffReference } from "../lib/api"
import EditableNationality from "../components/EditableNationality"
import LinkedRecords from "../components/LinkedRecords"
import AllowanceRequests from "../components/AllowanceRequests"
import FolioDiscount from "../components/FolioDiscount"
import { ADVANCE_BILL_ACTION } from "../lib/discountHint"
import ChangeCompany from "../components/ChangeCompany"
import {
  cur,
  fmtAmount,
  loadLocale,
  locale,
  moneyDigits,
  roundMoney,
  sellsAlcohol,
  taxLabel,
  taxRates,
  useLocale,
} from "../lib/money"
import { defaultChargeTaxRate } from "../lib/chargeTax"
import { folioLabel, shareFolioArgs, windowBadge, type SiblingFolio } from "../lib/folioLabel"
import { serverError } from "../lib/resource"
import { useCashierAuth } from "../lib/cashierAuth"
import { useAuth } from "../lib/auth"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { useT, t as translate, useUILanguage } from "../lib/i18n"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "../components/ui/card"

interface InvoiceData {
  folio: {
    name: string
    property?: string
    status: "Open" | "Closed"
    folio_type?: string
    window_no?: number | null
    payee_type?: string | null
    payee_company?: string | null
    payee_agent?: string | null
    payment_method?: string | null
    group_booking?: string | null
    bill_to_name?: string | null
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
      qty?: number
      rate?: number
      transaction_code?: string | null
      routing_instruction?: string | null
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
      refund_of?: string | null
      reversal_of?: string | null
      transferred_from?: string | null
      transferred_to?: string | null
      transfer_group_key?: string | null
    }[]
    charges_total: number
    tax_total: number
    grand_total: number
    payments_total: number
    balance: number
  }
  property: {
    currency: string
    currency_symbol?: string
    locale?: string
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
    tax_label?: string
    tax_id_label?: string
    /** the pack's line on how tax is shown (India: GST inclusive) */
    tax_note?: string
  }
  /** an Account folio (raised on a company's AR account) has no stay:
   *  only reservation (null) and company come back */
  stay: TariffReference & {
    /** the contract currency, its rate and the bill in it (Property reference currency calculation) */
    reference_currency?: ReferenceCurrency | null
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
  /** What the tax authority requires on the bill - ZATCA's QR code and
   *  bilingual title in Saudi Arabia; null elsewhere. */
  statutory?: {
    title_ar: string
    qr_image: string
    uuid: string
    icv: number
    status: string
  } | null
  /** Under the ERP accounting bridge: the Sales Invoice behind a settled
   *  folio and its fiscal identifiers (null on an open folio or without it). */
  fiscal?: Fiscal | null
  /** true when hospitality_kamra.credit_and_rebill is installed on this bridge site */
  bridge_credit_and_rebill?: boolean
  document: {
    title: string
    title_local?: string | null
    is_final: boolean
    number: string
    /** this folio's own number, when the ERP invoice is the tax invoice */
    internal_number?: string | null
    reprint?: number | null
    date: string
    amount_in_words: string
    footer: string | null
    invoice_footer?: string | null
    service_code_label: string
    accounting_tax_note?: string | null
  }
  /** Where the printed tax breakdown came from: folio lines, the linked
   *  accounting invoice, or accounting_unavailable when it could not be read. */
  tax_source?: "folio" | "accounting" | "accounting_unavailable"
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

interface TransactionCodeChoice {
  code: string
  description: string
  charge_type?: string | null
  is_alcohol?: number
}

interface InHouseRoomChoice {
  name: string
  room_number?: string | null
  guest_name?: string | null
}

const CHARGE_TYPES = [
  "Food & Beverage", "Minibar", "Laundry", "Spa", "Taxi",
  "Early Check-in", "Late Checkout", "Pet Charge", "Discount", "Misc",
]

const FALLBACK_CODES: TransactionCodeChoice[] = [
  { code: "2000", description: "Food & Beverage", charge_type: "Food & Beverage", is_alcohol: 0 },
  { code: "2010", description: "Beverage & Alcohol", charge_type: "Food & Beverage", is_alcohol: 1 },
  { code: "2100", description: "Minibar", charge_type: "Minibar", is_alcohol: 0 },
  { code: "3000", description: "Laundry", charge_type: "Laundry", is_alcohol: 0 },
  { code: "3100", description: "Spa", charge_type: "Spa", is_alcohol: 0 },
  { code: "3200", description: "Taxi", charge_type: "Taxi", is_alcohol: 0 },
  { code: "8000", description: "Miscellaneous", charge_type: "Misc", is_alcohol: 0 },
]

const inputCls =
  "rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

/** One labelled fact on the printed bill - label and value stay on the
 *  same line so the header block reads as a form, not a paragraph. */
function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  useUILanguage()
  return (
    <div className="flex gap-2">
      <dt className="min-w-24 max-w-44 shrink-0 break-words text-zinc-500">{translate(label)}</dt>
      <dd className="min-w-0">{children}</dd>
    </div>
  )
}

/** Kamra Verify: check the bill-to tax ID before the invoice goes out.
 *  Offered where the country pack has it (India: GSTIN); paid per check
 *  from the Kamra Connect wallet. */
function VerifyTaxId({ taxId, name, symbol }: { taxId: string; name: string; symbol?: string }) {
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
              text: `${r.valid ? r.legal_name || t("Valid") : t("Not a valid tax ID")}${r.status ? ` · ${r.status}` : ""} · ${symbol ?? cur()}${r.charged}`,
            })
          } catch (e) {
            setRes({ ok: false, text: serverError(e) })
          } finally {
            setBusy(false)
          }
        }}>
        {busy ? t("Checking…") : t("Verify")}
      </button>
      {res && <span className={`ms-1 text-xs ${res.ok ? "text-emerald-700" : "text-red-700"}`}>{translate(res.text)}</span>}
    </span>
  )
}

/** A charge line's actions on one compact line (OPERA folio billing); while the inline
 *  correction form is open it has the cell to itself, and Move comes back when it closes. */
function ChargeActions({ folio, row, correct, onMove, act }: {
  folio: string
  row: string
  correct: boolean
  onMove: (() => void) | null
  act: (fn: () => Promise<unknown>, onError: (message: string) => void) => Promise<void>
}) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const correcting = correct && open
  if (!correct && !onMove) return null
  return (
    <div data-row-actions
      className={correcting ? "grid min-w-56 gap-1.5 text-start" : "inline-flex items-center justify-end gap-1.5"}>
      {correct && <RowCorrection folio={folio} row={row} modes={[]} act={act} onOpenChange={setOpen} />}
      {onMove && !correcting && (
        <button
          aria-label={t("Move or split this charge")}
          className="inline-flex items-center gap-1 rounded-md border border-zinc-200 px-2 py-1 text-xs text-zinc-500 hover:border-brand-400 hover:text-zinc-800"
          onClick={onMove}
        >
          <ArrowRightLeft className="size-3" aria-hidden />
          {t("Move")}
        </button>
      )}
    </div>
  )
}

export default function FolioView() {
  const { t } = useT()
  const { name } = useParams()
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const [data, setData] = useState<InvoiceData | null>(null)
  const [siblings, setSiblings] = useState<SiblingFolio[]>([])
  const [windowOverview, setWindowOverview] = useState<{
    window_cap: number
    can_add_window: boolean
    windows: FolioWindowTabItem[]
  } | null>(null)
  const [txnCodes, setTxnCodes] = useState<TransactionCodeChoice[]>([])
  const [inHouseRooms, setInHouseRooms] = useState<InHouseRoomChoice[]>([])
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const [charge, setCharge] = useState({
    charge_type: "Food & Beverage",
    transaction_code: "",
    description: "",
    amount: "",
    // the property's own rate (0% where it charges no tax), never a made-up one
    gst_rate: defaultChargeTaxRate(taxRates(), locale().services_tax_rate),
  })
  const taxPicked = useRef(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [splitMode, setSplitMode] = useState<"auto" | "qty">("auto")
  const [partVal, setPartVal] = useState("")
  const [qtyVal, setQtyVal] = useState("1")
  const [moveRoomTarget, setMoveRoomTarget] = useState("")
  const [moveRoomReason, setMoveRoomReason] = useState("")
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
  const [showCreditRebill, setShowCreditRebill] = useState(false)
  const [showRouting, setShowRouting] = useState(false)
  const [transferPayment, setTransferPayment] = useState<InvoiceData["folio"]["payments"][number] | null>(null)
  const [cancelReason, setCancelReason] = useState("")
  const [allowance, setAllowance] = useState({ amount: "", reason: "", gst_rate: "0", reason_code: "" })
  const [adjustCodes, setAdjustCodes] = useState<{ name: string; description: string }[]>([])
  const [allowanceNote, setAllowanceNote] = useState<string | null>(null)
  const [allowanceKey, setAllowanceKey] = useState(0)
  const [allowancePin, setAllowancePin] = useState<string | null>(null)
  const [panel, setPanel] = useState<"company" | "share" | null>(null)
  const [shares, setShares] = useState<Record<string, string>>({})
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const { roles } = useAuth()
  const canRequestCorrection = roles.some(r => ["Front Desk", "Finance", "Hotel Admin", "System Manager", "Administrator"].includes(r))
  const canCorrect = roles.some((r) =>
    ["Finance", "Hotel Admin", "System Manager", "Administrator"].includes(r))
  const canCreditInvoice = roles.some((r) =>
    ["Finance", "Hotel Admin", "System Manager", "Administrator"].includes(r))
  const [toCorrect, setToCorrect] = useState<
    { date: string; charged: number; new: number }[]>([])
  const [correction, setCorrection] = useState({ reason: "", pin: "" })

  const load = useCallback(() => {
    if (!name) return
    call<InvoiceData>("kamra.api.folio_invoice", { folio: name })
      .then(async (d) => {
        setData(d)
        setPayment((p) => ({
          ...p,
          amount: p.currency && p.currency !== d.property.currency ? "" : String(d.folio.balance || ""),
        }))
        const [ov, sibs] = await Promise.all([
          call<{
            window_cap?: number
            can_add_window?: boolean
            windows?: Array<FolioWindowTabItem & { folio?: string }>
          }>("kamra.payment_transfers.folio_windows_overview", { folio: name }).catch(() => null),
          d.stay.reservation
            ? call<SiblingFolio[]>("kamra.api.reservation_folios", {
                reservation: d.stay.reservation,
              }).catch(() => [])
            : Promise.resolve([]),
        ])
        const normalizedSibs = Array.isArray(sibs) ? sibs : []
        if (ov && Array.isArray(ov.windows) && ov.windows.length > 0) {
          const mapped: FolioWindowTabItem[] = ov.windows.map((w) => ({
            ...w,
            name: w.folio || w.name,
            folio_type: w.folio_type || "Guest",
            status: w.status || "Open",
            balance: Number(w.balance ?? 0),
            grand_total: Number(w.grand_total ?? w.charges_total ?? 0),
            payments_total: Number(w.payments_total ?? 0),
          }))
          setWindowOverview({
            window_cap: ov.window_cap ?? 8,
            can_add_window: ov.can_add_window ?? false,
            windows: mapped,
          })
          setSiblings(normalizedSibs.length ? normalizedSibs : mapped)
        } else {
          setWindowOverview(null)
          setSiblings(normalizedSibs)
        }
      })
      .catch((e) => setError(serverError(e)))
  }, [name])

  useEffect(load, [load])
  useEffect(() => {
    call<{ name: string; description: string }[]>("kamra.ledger.list_reason_codes", { category: "Adjust" })
      .then((list) => setAdjustCodes(Array.isArray(list) ? list : []))
      .catch(() => {})
  }, [])

  const stayRes = data?.stay.reservation
  const folioOpen = data?.folio.status === "Open"
  useEffect(() => {
    if (!folioOpen || !folioProperty) return
    call<{
      transaction_codes?: TransactionCodeChoice[]
      in_house_reservations?: InHouseRoomChoice[]
    }>("kamra.routing.routing_options", {
      property: folioProperty,
      reservation: stayRes || undefined,
    })
      .then((res) => {
        if (Array.isArray(res?.transaction_codes) && res.transaction_codes.length > 0) {
          setTxnCodes(res.transaction_codes)
        }
        if (Array.isArray(res?.in_house_reservations)) {
          setInHouseRooms(res.in_house_reservations)
        }
      })
      .catch(() => {})
  }, [folioOpen, folioProperty, stayRes])

  const balanceNow = data?.folio.balance
  useEffect(() => {
    if (!showIn || !name) return setShown(null)
    call<NonNullable<typeof shown>>("kamra.ledger.folio_in_currency", { folio: name, currency: showIn })
      .then(setShown)
      .catch((e) => { setShown(null); setError(serverError(e)) })
  }, [showIn, name, balanceNow])

  const grand = data?.folio.grand_total
  useEffect(() => {
    if (!canCorrect || !stayRes) return
    call<{ nights: { date: string; charged: number; new: number }[] }>(
      "kamra.api.correct_charged_nights", { reservation: stayRes, dry_run: 1 })
      .then((r) => setToCorrect(Array.isArray(r?.nights) ? r.nights : []))
      .catch(() => setToCorrect([]))
  }, [canCorrect, stayRes, grand])

  const [rates, setRates] = useState<number[]>(taxRates())
  useEffect(() => {
    loadLocale().then((l) => {
      setRates(l.tax_rates)
      if (!taxPicked.current)
        setCharge((c) => ({ ...c, gst_rate: defaultChargeTaxRate(l.tax_rates, l.services_tax_rate) }))
    })
  }, [])
  const noTax = rates.length === 1 && Number(rates[0]) === 0
  const effectiveCodes = (txnCodes.length ? txnCodes : FALLBACK_CODES).filter(
    (c) => sellsAlcohol() || !c.is_alcohol,
  )
  const chosenCode = effectiveCodes.find((c) => c.code === charge.transaction_code)
  const fnbWords = loc.fnb_charge_words ?? []
  const chargeText = `${charge.charge_type} ${charge.description}`.toLowerCase()
  const fixedRate: number | null =
    !chosenCode?.is_alcohol && loc.fnb_tax_rate != null && fnbWords.some((w) => chargeText.includes(w))
      ? Number(loc.fnb_tax_rate)
      : null

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

  function advanceBill() {
    if (!stayRes) return
    act(() => call("kamra.ledger.force_advance_bill", withPin({ reservation: stayRes, nights: "entire" })))
  }

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
    return error ? (
      <div className="my-6 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
        {translate(error)}
      </div>
    ) : (
      <p className="py-10 text-center text-sm text-zinc-400">{t("Loading…")}</p>
    )

  const { folio, property, stay, gst_summary } = data
  const doc = data.document
  const fmt = (n: unknown) => fmtAmount(n ?? 0, home)
  const docCurrency = property.currency || loc.currency
  const docSymbol =
    property.currency_symbol != null
      ? property.currency_symbol
      : property.currency && property.currency !== loc.currency
        ? `${property.currency} `
        : cur()
  const docLocale = property.locale || loc.locale
  const docTaxLabel = property.tax_label || taxLabel()
  const lines = data.lines ?? folio.charges.map((c) => ({ ...c, row: c.name, qty: c.qty ?? 1, rate: c.rate ?? c.amount, service_code: null }))
  const lineByRow = new Map(lines.map((l) => [l.row, l]))
  const taxRows = data.tax_summary ?? []
  const taxUnavailable = data.tax_source === "accounting_unavailable"
  const taxed = taxUnavailable || Number(folio.tax_total) !== 0 ||
    lines.some((l) => Number(l.gst_rate) !== 0 || Number(l.gst_amount) !== 0) ||
    taxRows.length > 0
  const showLineTax = taxed && !taxUnavailable
  const open = folio.status === "Open"
  const invoiced = !!(folio.invoice_number || folio.sales_invoice)
  const isFinal = doc ? doc.is_final : Boolean(folio.invoice_number)
  const hasServiceCode = lines.some((l) => Boolean(l.service_code))
  const groupBookingId = stay.group_booking || folio.group_booking || null

  const windowTabs: FolioWindowTabItem[] = windowOverview?.windows?.length
    ? windowOverview.windows
    : siblings.length
      ? siblings
      : (stay.reservation || groupBookingId)
        ? [{
            name: folio.name,
            folio_type: folio.folio_type || "Guest",
            status: folio.status,
            invoice_number: folio.invoice_number,
            sales_invoice: folio.sales_invoice,
            window_no: folio.window_no ?? 1,
            payee_type: folio.payee_type ?? "Guest",
            payee_company: folio.payee_company,
            payee_agent: folio.payee_agent,
            payment_method: folio.payment_method,
            bill_to_name: folio.bill_to_name,
            balance: folio.balance,
            grand_total: folio.grand_total,
            payments_total: folio.payments_total,
          }]
        : []

  const openTransferTargets = windowTabs.filter(
    (s) => s.status === "Open" && !s.invoice_number && !s.sales_invoice && s.name !== folio.name,
  )

  function remainingTransferable(p: InvoiceData["folio"]["payments"][number]): number {
    const origAmt = Number(p.amount || 0)
    if (origAmt <= 0) return 0
    const prefix = `PT:${p.name}:`
    let deducted = 0
    for (const row of folio.payments) {
      if (row.name === p.name) continue
      const rowAmt = Number(row.amount || 0)
      if (rowAmt >= 0) continue
      if (row.refund_of === p.name) {
        deducted += Math.abs(rowAmt)
      } else if (row.transferred_to && (row.transfer_group_key || "").startsWith(prefix)) {
        deducted += Math.abs(rowAmt)
      }
    }
    return Math.max(0, roundMoney(origAmt - deducted, home))
  }

  return (
    <div>
      <div className="mb-4 flex flex-wrap items-center justify-between gap-2 print:hidden">
        <Link
          to="/billing"
          className="inline-flex items-center gap-1 text-sm text-zinc-500 hover:text-zinc-800"
        >
          <ArrowLeft className="size-4 rtl:rotate-180" aria-hidden />
          {t("Billing")}
        </Link>
        <div className="flex max-w-full flex-wrap gap-2">
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
          {!open && invoiced && canCreditInvoice
            && (!folio.sales_invoice || data.bridge_credit_and_rebill !== false) ? (
            <Button variant="outline" disabled={busy} onClick={() => setShowCredit(true)}>
              {t("Credit note")}
            </Button>
          ) : null}
          {!open && invoiced && canCreditInvoice && folio.folio_type !== "Account" ? (
            <Button
              variant="outline"
              disabled={busy}
              data-testid="open-credit-rebill-btn"
              onClick={() => setShowCreditRebill(true)}
            >
              {t("Credit & Rebill")}
            </Button>
          ) : null}
          <Button variant="outline" onClick={() => {
            void call("kamra.ledger.record_folio_reprint", {
              folio: folio.name,
              document: isFinal ? "Guest Folio" : "Proforma",
            }).finally(() => window.print())
          }}>
            <Printer className="size-4" aria-hidden />
            {isFinal ? t("Print invoice") : t("Print folio")}
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
            && (!folio.sales_invoice || (canCreditInvoice && data.bridge_credit_and_rebill !== false)) && (
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

      {windowTabs.length > 0 && (
        <FolioWindowTabs
          currentFolio={folio.name}
          windows={windowTabs}
          currency={home}
          reservation={stay.reservation}
          groupBooking={groupBookingId}
          windowCap={windowOverview?.window_cap ?? 8}
          canAddWindow={windowOverview ? windowOverview.can_add_window : windowTabs.length < 8}
          busy={busy}
          onDeleteEmpty={removeFolio}
          onOpenRouting={() => setShowRouting(true)}
          onWindowChanged={(targetFolio) => {
            if (targetFolio && targetFolio !== folio.name) {
              navigate(`/billing/${encodeURIComponent(targetFolio)}`)
            } else {
              load()
            }
          }}
          act={act}
        />
      )}

      <RoutingInstructionDrawer
        open={showRouting}
        onClose={() => setShowRouting(false)}
        property={folioProperty}
        currency={home}
        reservation={stay.reservation}
        groupBooking={groupBookingId}
        onChanged={load}
      />

      {transferPayment && open && !invoiced && (
        <ReceiptTransferDialog
          folio={folio.name}
          currency={home}
          payment={transferPayment}
          maxAmount={remainingTransferable(transferPayment)}
          siblings={windowTabs}
          targets={openTransferTargets}
          onClose={() => setTransferPayment(null)}
          onDone={() => {
            setTransferPayment(null)
            load()
          }}
        />
      )}

      {showCredit && !open && invoiced && (
        <InvoiceCorrectionDialog folio={folio} credited={data.credited ?? 0}
          currency={docCurrency} currencySymbol={docSymbol} numberLocale={docLocale}
          onClose={() => setShowCredit(false)}
          onDone={() => { setShowCredit(false); load() }} />
      )}

      {showCreditRebill && !open && invoiced && (
        <CreditRebillDialog
          folio={folio}
          currency={home}
          credited={data.credited ?? 0}
          onClose={() => setShowCreditRebill(false)}
          onDone={() => {
            setShowCreditRebill(false)
            load()
          }}
        />
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
          <LinkedRecords doctype="Reservation" name={stay.reservation}
            currency={docCurrency} currencySymbol={docSymbol} numberLocale={docLocale} />
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
                    <input className={`${inputCls} w-20`} type="number" min={0} max={100} step="any" inputMode="decimal"
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
                        await call(
                          "kamra.api.share_folio_charges",
                          shareFolioArgs(
                            folio.name,
                            rows.map((c) => c.name),
                            bills.map((b) => b.name),
                            shares,
                          ),
                        )
                        setSelected(new Set())
                        setShares({})
                        setPanel(null)
                      })
                    }>
                    {t("Share these charges")}
                  </Button>
                  <Button variant="ghost" onClick={() => { setShares({}); setPanel(null) }}>{t("Cancel")}</Button>
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
              {translate(doc?.title) ?? (isFinal ? t("Invoice") : t("Provisional Bill"))}
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
            <div className="text-end">
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
              {doc?.reprint ? (
                <p className="mt-1 text-xs font-semibold uppercase text-zinc-600">
                  {t("Copy - reprint {n}", { n: doc.reprint })}
                </p>
              ) : null}
              {data.fiscal ? (
                <FiscalDocument fiscal={data.fiscal} internalNumber={doc?.internal_number} />
              ) : !doc?.is_final && (
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
                <VerifyTaxId taxId={data.bill_to.gstin} name={data.bill_to.name} symbol={docSymbol} />
              )}
            </div>
          )}
          {stay.tariff && <p className="mb-3 text-sm text-zinc-600">
            {t("Booked contract tariff")}: <TariffNight {...stay} />
          </p>}
          <ReferenceCurrencyNote reference={stay.reference_currency} />
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
            const editable = open
            return (
              <>
                {open && selected.size > 0 && (() => {
                  const sel = folio.charges.filter((c) => selected.has(c.name))
                  const selTotal = sel.reduce((s, c) => s + (c.total || 0), 0)
                  const one = sel.length === 1 ? sel[0] : null
                  const oneLine = one ? lineByRow.get(one.name) : null
                  const rowQty = Number(oneLine?.qty ?? one?.qty ?? 1)
                  const canSplit =
                    !!one && !one.source_ref && one.charge_type !== "Meal Plan" && (one.amount || 0) > 0
                  const canQtySplit = canSplit && rowQty > 1
                  const activeMode = splitMode === "qty" && canQtySplit ? "qty" : "auto"

                  const v = partVal.trim()
                  const isPct = v.endsWith("%")
                  const num = Number(v.replace("%", ""))
                  const autoPartOk =
                    canSplit && !!v && num > 0 &&
                    (isPct ? num < 100 : num < (one!.amount || 0))
                  const autoPartBad = canSplit && !!v && !autoPartOk

                  const qTrim = qtyVal.trim()
                  const qtyNum = Number(qTrim)
                  const qtyOk =
                    activeMode === "qty" &&
                    !!qTrim &&
                    Number.isInteger(qtyNum) &&
                    qtyNum >= 1 &&
                    qtyNum < rowQty
                  const qtyBad = activeMode === "qty" && !qtyOk

                  const partOk = activeMode === "qty" ? qtyOk : autoPartOk
                  const partBad = activeMode === "qty" ? qtyBad : autoPartBad

                  const movedTotal = !partOk
                    ? selTotal
                    : activeMode === "qty"
                      ? roundMoney(((one!.total || 0) * qtyNum) / rowQty, home)
                      : isPct
                        ? roundMoney(((one!.total || 0) * num) / 100, home)
                        : roundMoney(num * (1 + (one!.gst_rate || 0) / 100), home)
                  const keptTotal = partOk && one ? roundMoney((one.total || 0) - movedTotal, home) : 0

                  const resetSelection = () => {
                    setSelected(new Set())
                    setPartVal("")
                    setQtyVal("1")
                    setSplitMode("auto")
                    setMoveRoomTarget("")
                    setMoveRoomReason("")
                  }

                  const moveTo = (to: string) =>
                    act(async () => {
                      if (one && activeMode === "qty" && qtyOk) {
                        await call("kamra.api.split_folio_charge", {
                          from_folio: folio.name,
                          charge_row: one.name,
                          to_folio: to,
                          qty: qtyNum,
                        })
                      } else if (one && activeMode === "auto" && autoPartOk) {
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
                      resetSelection()
                    })

                  const moveToNew = (type: "Extra" | "Company") =>
                    act(async () => {
                      const r = await call<{ folio: string }>(
                        "kamra.api.split_folio",
                        { reservation: data.stay.reservation, folio_type: type },
                      )
                      if (one && activeMode === "qty" && qtyOk) {
                        await call("kamra.api.split_folio_charge", {
                          from_folio: folio.name,
                          charge_row: one.name,
                          to_folio: r.folio,
                          qty: qtyNum,
                        })
                      } else if (one && activeMode === "auto" && autoPartOk) {
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
                      resetSelection()
                    })

                  const moveToRoom = () =>
                    act(async () => {
                      await call("kamra.api.transfer_folio_charges", {
                        from_folio: folio.name,
                        charge_rows: [...selected],
                        to_reservation: moveRoomTarget,
                        reason: moveRoomReason.trim(),
                      })
                      resetSelection()
                    })

                  return (
                    <div className="mb-3 rounded-xl border border-brand-200 bg-brand-50/40 p-3 text-sm print:hidden">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="font-medium">
                          {t("{n} line{s} · {cur}{amount} selected", {
                            n: sel.length,
                            s: sel.length > 1 ? "s" : "",
                            cur: cur(),
                            amount: fmt(selTotal),
                          })}
                        </span>
                        {canSplit ? (
                          <span className="inline-flex flex-wrap items-center gap-1.5 text-xs text-zinc-600">
                            <span>· {t("move only")}</span>
                            <span className="inline-flex overflow-hidden rounded-md border border-zinc-200 bg-white">
                              <button
                                type="button"
                                data-testid="split-mode-auto"
                                onClick={() => setSplitMode("auto")}
                                className={
                                  "px-2 py-1 text-xs " +
                                  (activeMode === "auto"
                                    ? "bg-brand-600 font-medium text-white"
                                    : "text-zinc-600 hover:bg-zinc-50")
                                }
                              >
                                {t("% / Amount")}
                              </button>
                              {canQtySplit && (
                                <button
                                  type="button"
                                  data-testid="split-mode-qty"
                                  onClick={() => setSplitMode("qty")}
                                  className={
                                    "px-2 py-1 text-xs " +
                                    (activeMode === "qty"
                                      ? "bg-brand-600 font-medium text-white"
                                      : "text-zinc-600 hover:bg-zinc-50")
                                  }
                                >
                                  {t("By Qty")}
                                </button>
                              )}
                            </span>
                            {activeMode === "qty" ? (
                              <>
                                <input
                                  className="w-20 rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs"
                                  type="number"
                                  min={1}
                                  max={Math.max(1, Math.floor(rowQty) - 1)}
                                  step={1}
                                  inputMode="numeric"
                                  aria-label={t("Quantity to split")}
                                  value={qtyVal}
                                  onChange={(e) => setQtyVal(e.target.value)}
                                />
                                <span className="text-zinc-500">
                                  {t("of {total} units", { total: rowQty })}
                                </span>
                              </>
                            ) : (
                              <>
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
                              </>
                            )}
                          </span>
                        ) : one ? (
                          <span className="text-xs text-zinc-500">
                            · {t("Moves whole (cannot be split)")}
                          </span>
                        ) : null}
                        <button
                          className="ms-auto text-xs text-zinc-400 hover:text-zinc-700"
                          onClick={resetSelection}
                        >
                          {t("Clear")}
                        </button>
                      </div>
                      {partOk && one && (
                        <p className="mt-1.5 text-xs text-brand-700">
                          {t("Preview: Kept {kept} · Moved {moved}", {
                            kept: `${cur()}${fmt(keptTotal)}`,
                            moved: `${cur()}${fmt(movedTotal)}`,
                          })}
                        </p>
                      )}
                      {partBad && (
                        <p className="mt-1.5 text-xs text-rose-600">
                          {activeMode === "qty"
                            ? t("Enter a whole quantity from 1 to {max}.", { max: Math.max(1, Math.floor(rowQty) - 1) })
                            : t("Enter 1-99%, or a {cur} amount under the line's {cur}{amount} (before {tax}).", {
                                cur: cur(),
                                amount: fmt(one!.amount),
                                tax: taxLabel(),
                              })}
                        </p>
                      )}
                      <div className="mt-2 flex flex-wrap items-center gap-1.5">
                        <span className="text-xs text-zinc-500">
                          {t("Send {cur}{amount} to", { cur: cur(), amount: fmt(movedTotal) })}
                        </span>
                        {targets.map((s) => (
                          <button
                            key={s.name}
                            className="rounded-lg border border-zinc-200 bg-white px-2.5 py-1.5 text-xs hover:border-brand-400 disabled:opacity-50"
                            disabled={busy || partBad}
                            onClick={() => moveTo(s.name)}
                          >
                            {s.window_no ? (
                              <span className="me-1 font-mono text-[11px] text-zinc-500">
                                {windowBadge(s)}
                              </span>
                            ) : null}
                            <span className="font-medium">
                              {folioLabel(siblings, s, t)}
                            </span>
                            <span
                              className={
                                s.balance > 0
                                  ? "ms-1.5 text-amber-600"
                                  : "ms-1.5 text-zinc-400"
                              }
                            >
                              {t("owes {cur}{amount}", { cur: cur(), amount: fmt(s.balance) })}
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
                      {canCorrect && inHouseRooms.length > 0 && !partOk && (
                        <div className="mt-2.5 flex flex-wrap items-center gap-1.5 border-t border-brand-200/70 pt-2">
                          <span className="text-xs text-zinc-500">{t("Or transfer to another room")}:</span>
                          <select
                            aria-label={t("Destination room")}
                            className="rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs"
                            value={moveRoomTarget}
                            onChange={(e) => setMoveRoomTarget(e.target.value)}
                          >
                            <option value="">{t("Select in-house room")}</option>
                            {inHouseRooms.map((rm) => (
                              <option key={rm.name} value={rm.name}>
                                {rm.room_number ? `${rm.room_number} · ` : ""}
                                {rm.guest_name || rm.name}
                              </option>
                            ))}
                          </select>
                          {moveRoomTarget && (
                            <>
                              <input
                                className="min-w-40 flex-1 rounded-md border border-zinc-300 bg-white px-2 py-1 text-xs"
                                placeholder={t("Reason (required)")}
                                value={moveRoomReason}
                                onChange={(e) => setMoveRoomReason(e.target.value)}
                              />
                              <button
                                type="button"
                                className="rounded-lg border border-brand-600 bg-brand-600 px-2.5 py-1 text-xs font-medium text-white disabled:opacity-50"
                                disabled={busy || !moveRoomReason.trim()}
                                onClick={moveToRoom}
                              >
                                {t("Move to room")}
                              </button>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  )
                })()}
                <ResponsiveList className="mb-5 w-full text-sm">
                  <thead>
                    <tr className="border-b border-zinc-200 text-start text-xs font-medium uppercase tracking-wider text-zinc-500">
                      {open && (
                        <th className="w-6 py-2 pe-2 print:hidden" aria-label={t("Select")} />
                      )}
                      <th className="py-2 pe-3">{t("Date")}</th>
                      <th className="py-2 pe-3">{t("Item")}</th>
                      {hasServiceCode && (
                        <th className="hidden py-2 pe-3 print:table-cell">
                          {doc?.service_code_label ?? t("SAC")}
                        </th>
                      )}
                      <th className="py-2 pe-3 text-end">{t("Amount {cur}", { cur: cur() })}</th>
                      {showLineTax && <th className="py-2 pe-3 text-end">{docTaxLabel} %</th>}
                      {showLineTax && <th className="py-2 pe-3 text-end">{docTaxLabel} {cur()}</th>}
                      <th className="py-2 text-end">{t("Total {cur}", { cur: cur() })}</th>
                      {editable && (
                        <th className="py-2 ps-3 print:hidden" aria-label={t("Actions")} />
                      )}
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-zinc-100">
                    {folio.charges.map((c, i) => {
                      const voided = !!c.reversal_of || folio.charges.some((r) => r.reversal_of === c.name)
                      const split = voided && (c.reversal_of
                        ? !!c.split_of
                        : folio.charges.some((r) => r.reversal_of === c.name && !!r.split_of))
                      return (
                      <Fragment key={c.name ?? i}>
                        <tr className={[voided && "text-zinc-400", split && "print:hidden"]
                          .filter(Boolean).join(" ") || undefined}>
                          {open && (
                            <Popin label={t("Select")} importance="medium" className="py-2 pe-2 print:hidden">
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
                          <Popin label={t("Date")} importance="medium" className="py-2 pe-3 text-zinc-500">{formatDate(c.posting_date)}</Popin>
                          <Popin label={t("Item")} importance="high" className="py-2 pe-3">
                            <span className="font-medium">{translate(c.charge_type === "Allowance" ? "Discount" : c.charge_type)}</span>
                            {c.description && (
                              <span className="text-zinc-500"> - {c.description}</span>
                            )}
                            {voided && (
                              <span className="ms-1 rounded bg-zinc-100 px-1 text-xs text-zinc-500">
                                {split ? t("status|Split") : t("Voided")}
                              </span>
                            )}
                            {c.allowance_serial && (
                              <Link to={`/allowance-voucher/${encodeURIComponent(c.allowance_serial)}`}
                                className="ms-1 font-mono text-xs text-brand-700 hover:underline print:text-zinc-500 print:no-underline"
                                title={t("Print voucher")}>
                                {c.allowance_serial}
                              </Link>
                            )}
                          </Popin>
                          {hasServiceCode && (
                            <td className="hidden py-2 pe-3 font-mono text-xs text-zinc-400 print:table-cell">
                              {lineByRow.get(c.name)?.service_code ?? ""}
                            </td>
                          )}
                          {(() => {
                            const ln = lineByRow.get(c.name)
                            const amt = ln ? ln.amount : c.amount
                            const rate = ln ? ln.gst_rate : c.gst_rate
                            const tax = ln ? ln.gst_amount : c.gst_amount
                            const tot = ln ? ln.total : c.total
                            return (
                              <>
                                <Popin label={t("Amount")} importance="medium" className="py-2 pe-3 text-end">{fmt(amt)}</Popin>
                                {showLineTax && <Popin label={`${docTaxLabel} %`} importance="medium" className="py-2 pe-3 text-end">{rate}%</Popin>}
                                {showLineTax && <Popin label={docTaxLabel} importance="medium" className="py-2 pe-3 text-end">{fmt(tax)}</Popin>}
                                <Popin label={t("Total")} importance="medium" className="py-2 text-end font-medium">{fmt(tot)}</Popin>
                              </>
                            )
                          })()}
                          {editable && (
                            <Popin label={t("Actions")} importance="medium" className="relative whitespace-nowrap py-2 ps-3 text-end print:hidden">
                              <ChargeActions folio={folio.name} row={c.name} act={act}
                                correct={canRequestCorrection && c.charge_type !== "Allowance" && !voided}
                                onMove={open && !voided && !selected.has(c.name)
                                  ? () => setSelected((prev) => new Set(prev).add(c.name)) : null} />
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
                        <span className="ms-1.5 text-xs text-zinc-400">
                          {t("{n} line{s}", { n: h.lines, s: h.lines === 1 ? "" : "s" })}
                        </span>
                      </td>
                      <td className="py-1 text-end text-zinc-500">
                        {cur()}{fmt(h.amount)}
                      </td>
                      {showLineTax && (
                        <td className="py-1 text-end text-zinc-400">
                          + {cur()}{fmt(h.tax)} {docTaxLabel.toLowerCase()}
                        </td>
                      )}
                      <td className="py-1 text-end font-medium">
                        {cur()}{fmt(h.total)}
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
                {t("{tax} summary", { tax: docTaxLabel })}
              </h3>
              {taxUnavailable ? (
                <p className="text-sm text-zinc-600">
                  {t("Tax as per the accounting invoice")}
                </p>
              ) : (
                <table className="w-full text-sm">
                  <tbody className="divide-y divide-zinc-100">
                    {(taxRows.length ? taxRows : gst_summary.map((g) => ({
                      rate: g.rate, taxable: g.taxable, total_tax: g.total_tax,
                      parts: [
                        { label: "CGST", rate: g.rate / 2, amount: g.cgst },
                        { label: "SGST", rate: g.rate / 2, amount: g.sgst },
                      ],
                    }))).map((r, idx) => (
                      <tr key={`${r.rate}-${idx}`}>
                        <td className="py-1.5 pe-3">{r.rate === 0 && Number(r.total_tax) !== 0 ? "—" : `${r.rate}%`}</td>
                        <td className="py-1.5 pe-3 text-end text-zinc-500">
                          {t("taxable {cur}{amount}", { cur: cur(), amount: fmt(r.taxable) })}
                        </td>
                        <td className="py-1.5 text-end">
                          {r.parts
                            .map((x) => x.rate === 0
                              ? `${x.label} ${cur()}${fmt(x.amount)}`
                              : `${x.label} @ ${x.rate}% ${cur()}${fmt(x.amount)}`)
                            .join(" · ")}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div> : <div />}
            <div className="space-y-1.5 text-sm sm:text-end">
              <p className="text-zinc-500">
                {t("Charges")}: <span className="text-zinc-900">{cur()}{fmt(folio.charges_total)}</span>
              </p>
              {showLineTax && (
                <p className="text-zinc-500">
                  {docTaxLabel}: <span className="text-zinc-900">{cur()}{fmt(folio.tax_total)}</span>
                </p>
              )}
              <p className="text-lg font-semibold">
                {t("Grand total")}: {cur()}{fmt(folio.grand_total)}
              </p>
              {(data.credited ?? 0) > 0 && (
                <p className="text-sm font-medium text-rose-600">
                  {t("Credited")}: −{cur()}{fmt(data.credited)}
                </p>
              )}
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
                    cur: shown.currency,
                    total: fmtAmount(shown.grand_total, shown.currency),
                    balance: fmtAmount(shown.balance, shown.currency),
                    rate: shown.rate, date: shown.rate_date, home: shown.home_currency,
                  })}
                </p>
              )}
              {data.statutory && (
                <div className="flex items-center justify-end gap-3 pt-2">
                  <div className="text-end text-[10px] leading-tight text-zinc-400">
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
                {t("Paid")}: {cur()}{fmt(folio.payments_total)} ·{" "}
                {folio.balance < 0 ? t("Advance") : t("Balance")}:{" "}
                <span
                  className={
                    folio.balance > 0
                      ? "font-medium text-amber-600"
                      : "font-medium text-emerald-600"
                  }
                >
                  {cur()}{fmt(Math.abs(folio.balance))}
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
                {folio.payments.map((p) => {
                  const canTransferRow =
                    open &&
                    !invoiced &&
                    Number(p.amount) > 0 &&
                    (!p.payment_kind || p.payment_kind === "Payment" || p.payment_kind === "Advance") &&
                    !p.transferred_from &&
                    !p.transferred_to &&
                    !p.reversal_of &&
                    !p.refund_of &&
                    !Number(p.fx_amount) &&
                    openTransferTargets.length > 0 &&
                    remainingTransferable(p) > 0
                  return (
                    <li key={p.name} className="flex flex-wrap items-center justify-between gap-2 py-1.5">
                      <span>
                        {formatDate(p.posting_date)} · {translate(p.mode)}
                        {p.payment_kind && p.payment_kind !== "Payment" && (
                          <span className={"ms-1.5 rounded px-1.5 py-0.5 text-[10px] font-semibold " +
                            (p.payment_kind === "Refund" ? "bg-rose-50 text-rose-600"
                              : p.payment_kind === "Security Deposit" ? "bg-violet-50 text-violet-700"
                                : "bg-sky-50 text-sky-700")}>
                            {t(p.payment_kind)}
                          </span>
                        )}
                        {p.transferred_to && (
                          <span className="ms-1.5 rounded bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-700">
                            {t("Transferred out")}
                          </span>
                        )}
                        {p.transferred_from && (
                          <span className="ms-1.5 rounded bg-emerald-50 px-1.5 py-0.5 text-[10px] font-semibold text-emerald-700">
                            {t("Transferred in")}
                          </span>
                        )}
                        {p.reference && (
                          <span className="text-zinc-400"> · {p.reference}</span>
                        )}
                        {!!Number(p.fx_amount) && (
                          <span className="text-zinc-400"> · {currencyText(p.currency!, Number(p.fx_amount), loc.locale)} @ {formatFxRate(Number(p.exchange_rate), loc.locale)}{p.exchange_rate_date && ` · ${formatDate(p.exchange_rate_date)}`}</span>
                        )}
                      </span>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={Number(p.amount) < 0 ? "font-medium text-rose-600" : ""}>
                          {property.currency} {fmtAmount(p.amount, property.currency)}
                        </span>
                        {canTransferRow && (
                          <button
                            type="button"
                            data-testid={`transfer-payment-${p.name}`}
                            className="inline-flex items-center gap-1 rounded-md border border-zinc-200 px-2 py-1 text-xs text-zinc-600 hover:border-brand-400 hover:text-zinc-900 print:hidden"
                            onClick={() => setTransferPayment(p)}
                          >
                            <ArrowRightLeft className="size-3" aria-hidden />
                            {t("Transfer")}
                          </button>
                        )}
                        {canRequestCorrection && <RowCorrection folio={folio.name} row={p.name}
                          home={home} currency={Number(p.fx_amount) > 0 ? p.currency || home : home}
                          tenderTypes={loc.payment_tender_types}
                          modes={availableTenders.filter(({ mode }) => mode !== p.mode && tenderTypeOf(loc, mode) !== "Payment Link")} act={act} />}
                      </div>
                    </li>
                  )
                })}
              </ul>
            </div>
          )}

          {isFinal && (
            <div className="mt-8 flex flex-wrap items-end justify-between gap-4 border-t border-zinc-200 pt-4 text-xs text-zinc-500">
              <p className="max-w-md">
                {doc?.footer
                  ? t(doc.footer)
                  : t("This is a computer-generated invoice.")}
                {property.gstin && property.tax_note
                  ? ` ${t(property.tax_note)}`
                  : ""}
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
                aria-label={t("Charge type")}
                value={charge.charge_type}
                onChange={(e) =>
                  setCharge({ ...charge, charge_type: e.target.value })
                }
              >
                {CHARGE_TYPES.map((ct) => (
                  <option key={ct} value={ct}>{t(ct)}</option>
                ))}
              </select>
              <select
                className={inputCls}
                aria-label={t("Transaction Code")}
                value={charge.transaction_code}
                onChange={(e) => {
                  const tcCode = e.target.value
                  const match = effectiveCodes.find((c) => c.code === tcCode)
                  setCharge({
                    ...charge,
                    transaction_code: tcCode,
                    charge_type: match?.charge_type && CHARGE_TYPES.includes(match.charge_type)
                      ? match.charge_type
                      : charge.charge_type,
                  })
                }}
              >
                <option value="">{t("Default code")}</option>
                {effectiveCodes.map((tc) => (
                  <option key={tc.code} value={tc.code}>
                    {tc.code} · {translate(tc.description)}
                  </option>
                ))}
              </select>
              <input
                className={`${inputCls} min-w-36 flex-1`}
                placeholder={t("Description")}
                value={charge.description}
                onChange={(e) =>
                  setCharge({ ...charge, description: e.target.value })
                }
              />
              <input
                className={`${inputCls} w-28`}
                type="number"
                step="any"
                inputMode={moneyDigits(home) === 0 ? "numeric" : "decimal"}
                placeholder={translate(`${cur()}`)}
                value={charge.amount}
                onChange={(e) => setCharge({ ...charge, amount: e.target.value })}
              />
              {noTax ? null : fixedRate !== null ? (
                <span
                  className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-1.5 text-sm text-zinc-600"
                  title={t("Food & beverage is taxed at the property's F&B rate.")}
                >
                  {t("{tax} {rate}%", { tax: taxLabel(), rate: fixedRate })}
                  <span className="ms-1 text-xs text-zinc-400">{t("(F&B rate)")}</span>
                </span>
              ) : (
                <select
                  className={inputCls}
                  aria-label={t("{tax} rate", { tax: docTaxLabel })}
                  value={charge.gst_rate}
                  onChange={(e) => {
                    taxPicked.current = true
                    setCharge({ ...charge, gst_rate: e.target.value })
                  }}
                >
                  {rates.map((n) => String(n)).map((r) => (
                    <option key={r} value={r}>
                      {t("{tax} {rate}%", { tax: docTaxLabel, rate: r })}
                    </option>
                  ))}
                </select>
              )}
              <Button
                disabled={busy || !charge.amount}
                onClick={() =>
                  act(() =>
                    call("kamra.api.add_folio_charge", {
                      folio: folio.name,
                      charge_type: charge.charge_type,
                      description: charge.description,
                      amount: Number(charge.amount),
                      gst_rate: fixedRate !== null && rates.includes(fixedRate)
                        ? fixedRate
                        : Number(charge.gst_rate),
                      ...(charge.transaction_code ? { transaction_code: charge.transaction_code } : {}),
                    }),
                  )
                }
              >
                {t("Post")}
              </Button>
              <FolioDiscount folio={folio.name} adjustCodes={adjustCodes} act={act}
                currency={docCurrency} currencySymbol={docSymbol} numberLocale={docLocale}
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
                  <span className="ms-1.5 normal-case tracking-normal">
                    {t("- write off part of the bill, with a reason")}
                  </span>
                </div>
                <div className="flex flex-wrap items-end gap-2">
                  <input
                    className={`${inputCls} w-28`}
                    type="number"
                    step="any"
                    inputMode={moneyDigits(home) === 0 ? "numeric" : "decimal"}
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
                        {t("{tax} {rate}%", { tax: docTaxLabel, rate: r })}
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
                    currency={docCurrency} currencySymbol={docSymbol} numberLocale={docLocale}
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
                          date: n.date, from: fmt(n.charged), to: fmt(n.new),
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
