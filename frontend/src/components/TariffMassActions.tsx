import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react"
import { currencyText, tariffDate } from "./ContractTariff"
import { Button } from "./ui/button"
import { call, serverError } from "../lib/api"
import { useT } from "../lib/i18n"
import { addDays } from "../lib/stay"
import {
  adjustRate, decisionOutcome, roundRate, TARIFF_BATCH_LIMIT, TARIFF_IMPORT_BYTES, type Adjustment,
  type ContractTariffItem, type DecisionResult, type PriceList, type RoundingMode, type TariffVersion,
} from "../lib/tariffMatrix"

export type DraftChoice = { item: ContractTariffItem; draft: TariffVersion }
export type MassAction =
  /** currency: the selection's one tariff currency (null when it spans several); rounding: the plans'
   *  shared rule, only when it rounds that currency (ContractTariffs decides) */
  | { kind: "version"; items: ContractTariffItem[]; currency: string | null; rounding: { step: number; mode: RoundingMode } | null }
  | { kind: "approve" | "discard"; drafts: DraftChoice[] }
  | { kind: "import" }

export const field = "min-h-10 rounded-lg border border-zinc-300 bg-white px-3 text-sm text-zinc-900 focus:border-brand-600 focus:outline-2 focus:outline-brand-600"
export const decisionTone = {
  approve: "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100",
  discard: "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-100",
}
export const itemCurrency = (item: ContractTariffItem, list?: PriceList) => item.currency || list?.currency || ""

/** A server refusal worth showing as it is; integration internals are not. */
export function tariffReason(error: unknown) {
  const message = serverError(error)
  return /\b(?:ERPNext|Frappe|Kamra|URY|HeyKoala|Tridz|BuildSuite)\b/i.test(message) ? "" : message
}

/** The modal form every tariff action uses: focus returns to the opener on close. */
export function TariffModal({ title, subtitle, busy, onClose, onSubmit, children, footer }: {
  title: string; subtitle?: ReactNode; busy: boolean; onClose: () => void
  onSubmit: (event: FormEvent<HTMLFormElement>) => void; children: ReactNode; footer: ReactNode
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  useEffect(() => {
    const node = dialog.current
    const opener = document.activeElement
    node?.showModal()
    return () => {
      if (node?.open) node.close()
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])
  return <dialog ref={dialog} aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); if (!busy) onClose() }}
    className="m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-2xl border border-zinc-200 bg-white p-0 text-zinc-900 shadow-xl backdrop:bg-zinc-950/40">
    <form onSubmit={onSubmit}>
      <header className="space-y-1 border-b border-zinc-200 px-5 py-4">
        <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
        {subtitle && <p className="text-sm text-zinc-700">{subtitle}</p>}
      </header>
      <div className="space-y-4 px-5 py-4">{children}</div>
      <footer className="flex flex-wrap justify-end gap-2 border-t border-zinc-200 bg-zinc-50 px-5 py-3">{footer}</footer>
    </form>
  </dialog>
}

interface MassProps {
  action: MassAction
  property: string
  /** the property's home (posting) currency */
  propertyCurrency: string
  businessDate: string
  /** customers a version may be for: the directory's own and corporate / customer accounts the user can read */
  customers?: string[]
  lists: Map<string, PriceList>
  locale: string
  dateLocale: string
  onClose: () => void
  onDone: (message: string) => void
}

export function TariffMassDialog(props: MassProps) {
  const { action } = props
  if (action.kind === "version")
    return <BulkVersionDialog {...props} items={action.items} currency={action.currency} rounding={action.rounding} />
  if (action.kind === "import") return <ImportDialog {...props} />
  return <DecisionDialog {...props} kind={action.kind} drafts={action.drafts} />
}

/** Hold a submit until the previous one answers, and keep the dialog's own error. */
function useSubmission() {
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<string | null>(null)
  async function run(work: () => Promise<void>, fallback: string) {
    if (submitting.current) return
    submitting.current = true
    setBusy(true)
    setFailure(null)
    try {
      await work()
    } catch (error) {
      setFailure(tariffReason(error) || fallback)
    } finally {
      submitting.current = false
      setBusy(false)
    }
  }
  return { busy, failure, setFailure, run }
}

function Failure({ message }: { message: string | null }) {
  return message ? <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{message}</p> : null
}

/** The integration takes at most TARIFF_BATCH_LIMIT rows at once: say so instead of sending more. */
function OverLimit({ count }: { count: number }) {
  const { t } = useT()
  const over = Math.max(count - TARIFF_BATCH_LIMIT, 0)
  return over ? <p role="status" className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
    {t("Choose at most {n} tariffs at a time.", { n: TARIFF_BATCH_LIMIT })}
  </p> : null
}

function BulkVersionDialog({
  items, currency: shared, rounding, property, propertyCurrency, businessDate, customers = [], lists, locale, dateLocale,
  onClose, onDone,
}: MassProps & {
  items: ContractTariffItem[]; currency: string | null; rounding: { step: number; mode: RoundingMode } | null
}) {
  const { t, tn } = useT()
  const [kind, setKind] = useState<Adjustment["kind"]>("percent")
  const [value, setValue] = useState("")
  const [dateMode, setDateMode] = useState<"set" | "shift">("set")
  const [validFrom, setValidFrom] = useState(() => addDays(businessDate, 1))
  const [validUntil, setValidUntil] = useState("")
  const [shift, setShift] = useState("")
  const [step, setStep] = useState(() => rounding ? String(rounding.step) : "")
  const [mode, setMode] = useState<RoundingMode>(() => rounding?.mode ?? "Nearest")
  const { busy, failure, setFailure, run } = useSubmission()
  const prefillId = useId()
  const customerList = useId()
  // SAP Create with Reference to a customer: the selection's versions become that customer's own prices
  const [customer, setCustomer] = useState("")
  const target = customer.trim()

  const amount = Number(value)
  const days = Number(shift)
  const multiple = Math.max(0, Number(step) || 0)
  const ready = value.trim() !== "" && Number.isFinite(amount)
    && (dateMode === "set" ? !!validFrom && (!validUntil || validUntil >= validFrom) : shift.trim() !== "" && Number.isInteger(days))
  // one version per item for the customer: its own row when selected, else the first selected row
  const itemKey = (item: ContractTariffItem) => `${item.price_list}|${item.item_code}`
  const sources = target ? items.filter((item, index) => {
    const own = items.find((other) => itemKey(other) === itemKey(item) && other.customer === target)
    return own ? own === item : items.findIndex((other) => itemKey(other) === itemKey(item)) === index
  }) : items
  const plan = sources.map((item) => {
    const reference = item.active ?? null
    const owner = target || item.customer || null
    // a shift needs dates to move, and only an exact rate needs no current rate
    if (!reference && (kind !== "exact" || dateMode === "shift")) return null
    const currency = itemCurrency(item, lists.get(item.price_list))
    const rate = roundRate(adjustRate(reference?.rate ?? 0, { kind, value: amount }), multiple, mode, currency)
    const from = dateMode === "set" || !reference ? validFrom : addDays(reference.valid_from, days)
    const upto = dateMode === "set" ? validUntil || null : reference?.valid_upto ? addDays(reference.valid_upto, days) : null
    // a customer's first price follows no version of its own
    const fresh = owner !== (item.customer || null)
    const earliest = !fresh && reference && reference.valid_from >= businessDate ? addDays(reference.valid_from, 1) : businessDate
    const valid = Number.isFinite(rate) && rate >= 0 && from >= earliest && (!upto || upto >= from)
    return { item, reference, currency, rate, from, upto, valid, owner }
  })
  const entries = plan.filter((entry) => entry != null)
  const leftOut = plan.length - entries.length
  const invalid = entries.filter((entry) => !entry.valid).length
  const tooMany = Math.max(items.length - TARIFF_BATCH_LIMIT, 0) !== 0

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!ready || !entries.length || invalid || tooMany) return
    void run(async () => {
      const result = await call("kamra.rates.bulk_propose_contract_tariffs", {
        property, entries: entries.map(({ item, rate, from, upto, owner }) => ({
          price_list: item.price_list, item_code: item.item_code, price_list_rate: rate, valid_from: from,
          valid_upto: upto, ...(owner ? { customer: owner } : {}),
        })),
      })
      if (result == null) setFailure(t("Tariff editing is not available for this property."))
      else onDone(tn(entries.length, "{n} tariff version submitted for approval", "{n} tariff versions submitted for approval"))
    }, t("The tariff changes could not be saved. Check the latest versions before retrying."))
  }

  // tariffs are edited in their price list's contract currency, never the posting currency
  const valueLabel = kind === "percent" ? t("Percent change")
    : kind === "amount" ? shared ? t("Amount change ({currency})", { currency: shared }) : t("Amount change (in each tariff's currency)")
    : shared ? t("New rate ({currency})", { currency: shared }) : t("New rate (in each tariff's currency)")
  const prefilled = !!rounding && step === String(rounding.step) && mode === rounding.mode
  const converts = items.some((item) => itemCurrency(item, lists.get(item.price_list)) !== propertyCurrency)
  return <TariffModal title={t("Create new version in bulk")} busy={busy} onClose={onClose} onSubmit={submit}
    subtitle={tn(items.length, "{n} tariff selected", "{n} tariffs selected")}
    footer={<>
      <Button type="button" variant="outline" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
      <Button type="submit" disabled={busy || !ready || !entries.length || invalid > 0 || tooMany}>
        {busy ? t("Saving…") : t("Submit {n} for approval", { n: entries.length })}
      </Button>
    </>}>
    <OverLimit count={items.length} />
    <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
      {t("Customer (optional)")}
      <input name="customer" list={customerList} autoComplete="off" className={field} value={customer}
        onChange={(event) => setCustomer(event.target.value)} disabled={busy} />
      <datalist id={customerList}>{customers.map((name) => <option key={name} value={name} />)}</datalist>
      <span className="text-xs font-normal text-zinc-600">{t("Leave empty to keep each tariff's own customer.")}</span>
    </label>
    <div className="grid gap-3 sm:grid-cols-2">
      <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
        {t("Adjustment")}
        <select name="adjustment" className={field} value={kind} disabled={busy}
          onChange={(event) => setKind(event.target.value as Adjustment["kind"])}>
          <option value="percent">{t("Percent (+/-)")}</option>
          <option value="amount">{t("Fixed amount (+/-)")}</option>
          <option value="exact">{t("Set exact rate")}</option>
        </select>
      </label>
      <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
        {valueLabel}
        <input name="adjustment_value" type="number" inputMode="decimal" step="any" className={field + " tabular-nums"}
          value={value} onChange={(event) => setValue(event.target.value)} required disabled={busy} autoFocus />
      </label>
      <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
        {t("Dates")}
        <select name="date_mode" className={field} value={dateMode} disabled={busy}
          onChange={(event) => setDateMode(event.target.value as "set" | "shift")}>
          <option value="set">{t("Set new dates")}</option>
          <option value="shift">{t("Shift current dates")}</option>
        </select>
      </label>
      {dateMode === "shift" ? <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
        {t("Shift by days")}
        <input name="shift_days" type="number" step="1" className={field + " tabular-nums"} value={shift}
          onChange={(event) => setShift(event.target.value)} required disabled={busy} />
      </label> : <span aria-hidden className="hidden sm:block" />}
      {dateMode === "set" && <>
        <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
          {t("Effective from")}
          <input name="valid_from" type="date" className={field} min={businessDate} value={validFrom}
            onChange={(event) => setValidFrom(event.target.value)} required disabled={busy} />
        </label>
        <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
          {t("Valid until (optional)")}
          <input name="valid_upto" type="date" className={field} min={validFrom} value={validUntil}
            onChange={(event) => setValidUntil(event.target.value)} disabled={busy} />
        </label>
      </>}
      <div className="flex min-w-0 flex-col gap-1.5">
        <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
          {shared ? t("Round to ({currency} — tariff currency; 0 = the currency's smallest unit)", { currency: shared })
            : t("Round to (in each tariff's currency; 0 = the currency's smallest unit)")}
          <input name="rounding_step" type="number" min="0" step="any" className={field + " tabular-nums"} value={step}
            onChange={(event) => setStep(event.target.value)} disabled={busy} placeholder="0"
            aria-describedby={prefilled ? prefillId : undefined} />
        </label>
        {prefilled && <p id={prefillId} className="text-xs text-zinc-600">{t("Prefilled from the rate plan rounding rule")}</p>}
      </div>
      <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
        {t("Rounding")}
        <select name="rounding_mode" className={field} value={mode} disabled={busy}
          onChange={(event) => setMode(event.target.value as RoundingMode)}>
          <option value="Nearest">{t("Nearest")}</option>
          <option value="Up">{t("Up")}</option>
          <option value="Down">{t("Down")}</option>
        </select>
      </label>
    </div>
    {converts && <p className="text-xs text-zinc-600">
      {t("Tariffs are edited in the price list's contract currency; the {home} posting converts at booking or check-in under the rate plan's exchange rate lock.", { home: propertyCurrency })}
    </p>}
    {ready && <div className="space-y-2 rounded-lg border border-brand-200 bg-brand-50 p-3 text-sm" aria-live="polite">
      <p className="font-medium">{t("New versions")}</p>
      <ul className="max-h-48 space-y-1 overflow-y-auto tabular-nums">
        {entries.map(({ item, reference, currency, rate, from, upto, valid, owner }) =>
          <li key={[item.price_list, item.item_code, item.customer ?? "", owner ?? ""].join("|")} className={valid ? "" : "text-rose-800"}>
            {t("{item}: {from} → {to}", {
              item: [item.item_name, owner].filter(Boolean).join(" · "),
              from: reference ? currencyText(currency, reference.rate, locale) : "—",
              to: currencyText(currency, rate, locale),
            })} <span className="text-zinc-600">{upto
              ? t("{from} – {until}", { from: tariffDate(from, dateLocale), until: tariffDate(upto, dateLocale) })
              : tariffDate(from, dateLocale)}</span>
          </li>)}
      </ul>
      {leftOut > 0 && <p>{tn(leftOut, "{n} tariff has no current rate and is left out.", "{n} tariffs have no current rate and are left out.")}</p>}
      {invalid > 0 && <p className="font-medium text-rose-800">{tn(invalid,
        "{n} version would start before its current version or end before it starts. Change the dates.",
        "{n} versions would start before their current versions or end before they start. Change the dates.")}</p>}
    </div>}
    <Failure message={failure} />
  </TariffModal>
}

function DecisionDialog({ kind, drafts, locale, dateLocale, lists, onClose, onDone }: MassProps & {
  kind: "approve" | "discard"; drafts: DraftChoice[]
}) {
  const { t, tn } = useT()
  const { busy, failure, setFailure, run } = useSubmission()
  // separation of duties keeps a maker from approving their own draft, never from discarding it
  const decidable = kind === "approve" ? drafts.filter(({ draft }) => draft.can_approve !== false) : drafts
  const excluded = drafts.length - decidable.length
  const n = decidable.length
  const tooMany = Math.max(n - TARIFF_BATCH_LIMIT, 0) !== 0

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!n || tooMany) return
    void run(async () => {
      const result = await call<DecisionResult | null>(`kamra.rates.bulk_${kind}_contract_tariffs`,
        { item_prices: decidable.map(({ draft }) => draft.name) })
      if (result == null) {
        setFailure(t("Tariff editing is not available for this property."))
        return
      }
      // the integration skips a draft that fails a guard: report what it did, never the count sent
      const { done, skipped, reasons } = decisionOutcome(result, kind, n)
      const summary = [
        done ? kind === "approve" ? tn(done, "{n} tariff version approved", "{n} tariff versions approved")
          : tn(done, "{n} tariff draft discarded", "{n} tariff drafts discarded")
          : kind === "approve" ? t("No tariff versions were approved") : t("No tariff drafts were discarded"),
        skipped ? tn(skipped, "{n} skipped: {reasons}", "{n} skipped: {reasons}", { reasons: reasons.join("; ") || "—" }) : "",
      ].filter(Boolean).join(" · ")
      if (done) onDone(summary)
      else setFailure(summary)
    }, t("The tariff change could not be saved. Check the latest versions before retrying."))
  }

  return <TariffModal busy={busy} onClose={onClose} onSubmit={submit}
    title={kind === "approve" ? tn(n, "Approve {n} tariff version?", "Approve {n} tariff versions?")
      : tn(n, "Discard {n} tariff draft?", "Discard {n} tariff drafts?")}
    footer={<>
      <Button type="button" variant="outline" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
      <Button type="submit" variant="outline" className={decisionTone[kind]} disabled={busy || !n || tooMany}>
        {busy ? t("Saving…") : kind === "approve" ? t("Approve") : t("Discard")}
      </Button>
    </>}>
    <OverLimit count={n} />
    <ul className="max-h-48 space-y-1 overflow-y-auto text-sm tabular-nums">
      {decidable.map(({ item, draft }) => <li key={draft.name}>{t("{item}: {rate} from {date}", {
        item: [item.item_name, item.price_list, item.customer].filter(Boolean).join(" · "),
        rate: currencyText(itemCurrency(item, lists.get(item.price_list)), draft.rate, locale),
        date: tariffDate(draft.valid_from, dateLocale),
      })}</li>)}
    </ul>
    {excluded > 0 && <p className="rounded-lg bg-amber-50 p-3 text-sm text-amber-900">
      {tn(excluded, "{n} draft is left out: a different approver must release a draft its maker proposed.",
        "{n} drafts are left out: a different approver must release a draft its maker proposed.")}</p>}
    <p className="text-sm text-zinc-700">{kind === "approve"
      ? t("These versions take effect on their effective dates once approved.")
      : t("Discarding drafts cannot be undone. Released tariffs do not change.")}</p>
    <Failure message={failure} />
  </TariffModal>
}

interface ImportResult {
  created: number
  unchanged: number
  skipped: number
  errors: { row: number; message: string }[]
}

/** A chosen file's text (FileReader also covers browsers without Blob.text). */
function readText(file: File) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(String(reader.result ?? ""))
    reader.onerror = () => reject(reader.error)
    reader.readAsText(file)
  })
}

function ImportDialog({ property, onClose, onDone }: MassProps) {
  const { t, tn } = useT()
  const [file, setFile] = useState<File | null>(null)
  const [errors, setErrors] = useState<ImportResult["errors"]>([])
  const { busy, failure, setFailure, run } = useSubmission()

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!file) return
    setErrors([])
    if (file.size > TARIFF_IMPORT_BYTES) {
      // the server refuses it too; never read or send a file that large
      setFailure(t("The file is larger than 1 MB. Split it and import each part."))
      return
    }
    void run(async () => {
      const result = await call<ImportResult | null>("kamra.rates.import_contract_tariffs_rows", { property, rows: await readText(file) })
      if (result == null) setFailure(t("Tariff editing is not available for this property."))
      else if (result.errors.length) setErrors(result.errors)
      else onDone([
        result.created ? tn(result.created, "{n} draft version created for approval", "{n} draft versions created for approval")
          : t("No new versions to create"),
        result.unchanged ? t("{n} unchanged", { n: result.unchanged }) : "",
        result.skipped ? tn(result.skipped, "{n} row without a rate skipped", "{n} rows without a rate skipped") : "",
      ].filter(Boolean).join(" · "))
    }, t("The tariff file could not be imported. Check the file and try again."))
  }

  return <TariffModal title={t("Import tariffs from CSV")} busy={busy} onClose={onClose} onSubmit={submit}
    footer={<>
      <Button type="button" variant="outline" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
      <Button type="submit" disabled={busy || !file}>{busy ? t("Saving…") : t("Import as drafts")}</Button>
    </>}>
    <p className="text-sm text-zinc-700">{t("Use a file exported from this screen. Each changed row becomes a draft version for approval; nothing is released until it is approved.")}</p>
    <p className="text-sm text-zinc-600">{t("Up to {n} rows and 1 MB per file.", { n: TARIFF_BATCH_LIMIT })}</p>
    <label className="flex flex-col gap-1.5 text-sm font-medium">
      {t("CSV file")}
      <input name="tariff_file" type="file" accept=".csv,text/csv" className="text-sm" disabled={busy}
        onChange={(event) => { setFile(event.target.files?.[0] ?? null); setErrors([]) }} />
    </label>
    {errors.length > 0 && <div role="alert" className="space-y-1 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">
      <p className="font-medium">{t("Nothing was imported. Fix these rows and import again:")}</p>
      <ul className="max-h-48 list-disc space-y-0.5 overflow-y-auto ps-5">
        {errors.map((error) => <li key={`${error.row}:${error.message}`}>{t("Row {row}: {message}", error)}</li>)}
      </ul>
    </div>}
    <Failure message={failure} />
  </TariffModal>
}
