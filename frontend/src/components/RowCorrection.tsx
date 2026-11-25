import { useState, type MouseEvent } from "react"
import { call, newIdempotencyKey } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { moneyDigits, moneyStep, useLocale, type TenderMode } from "../lib/money"
import { PinPad } from "./PinPad"
import TenderPicker, { tenderTypeOf } from "./TenderPicker"

type Action = {
  id: string
  label: string
  requires: string[]
  reason_codes: {
    name: string
    description: string
    requires_supervisor: number
  }[]
}
type Options = {
  actions: Action[]
  blocked: { id: string; reason: string }[]
  supervisors: { user: string; full_name: string; has_pin?: boolean; must_reset?: boolean; locked?: boolean }[]
  refund_cap: number
}
export default function RowCorrection({
  folio,
  row,
  modes,
  tenderTypes,
  home = "",
  currency = home,
  act,
  onOpenChange,
}: {
  folio: string
  row: string
  modes: TenderMode[]
  tenderTypes?: Record<string, string>
  home?: string
  currency?: string
  act: (fn: () => Promise<unknown>, onError: (message: string) => void) => Promise<void>
  /** told when the inline form opens and closes, so a row can make room for it */
  onOpenChange?: (open: boolean) => void
}) {
  const { t } = useT()
  const loc = useLocale()
  const [options, setOptions] = useState<Options>(),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false)
  const [id, setId] = useState(""),
    [code, setCode] = useState(""),
    [note, setNote] = useState(""),
    [user, setUser] = useState(""),
    [padOpen, setPadOpen] = useState(false),
    [padError, setPadError] = useState("")
  const [amount, setAmount] = useState(""),
    [mode, setMode] = useState(""),
    [key, setKey] = useState(newIdempotencyKey)
  const supervisor = options?.supervisors.find((s) => s.user === user)
  const action = options?.actions.find((a) => a.id === id),
    approvals =
      action?.requires.includes("supervisor_user") ||
      action?.reason_codes.find((c) => c.name === code)?.requires_supervisor
  // Reposting to an FX tender (Foreign Cash or Bank Transfer tender_type) retains
  // the original rate. Other tenders repost the original home value.
  const resolvedTypes = tenderTypes ?? loc.payment_tender_types
  const destinations = modes.flatMap(({ mode: m, currencies }) => {
    const tt = tenderTypeOf({ payment_tender_types: resolvedTypes }, m)
    const targetCode = tt === "Foreign Cash" || tt === "Bank Transfer" ? currency : home
    return currencies.includes(targetCode) ? [{ mode: m, currencies: [targetCode] }] : []
  })
  const destinationCurrency = destinations.find((m) => m.mode === mode)?.currencies[0] ?? currency
  // Partial refunds always pay out in the property's home currency.
  const refundCurrency = home || loc.currency
  const refundDigits = moneyDigits(refundCurrency)
  const refundStep = moneyStep(refundCurrency)
  const choose = (a?: Action) => {
    setError("")
    setId(a?.id ?? "")
    setCode(a?.reason_codes[0]?.name ?? "")
    setMode("")
  }
  const close = () => {
    setOptions(undefined)
    onOpenChange?.(false)
    setUser("")
    setPadOpen(false)
    setPadError("")
    setNote("")
    setAmount("")
    setMode("")
    setError("")
  }
  async function open() {
    setLoading(true)
    setError("")
    try {
      const opts = await call<Options>("kamra.api.payment_correction_options", {
        folio,
        payment_row: row,
      })
      setOptions(opts)
      onOpenChange?.(true)
      choose(opts.actions[0])
    } catch (e) {
      setError(serverError(e))
    } finally {
      setLoading(false)
    }
  }
  async function submit(pinOrEvent?: string | MouseEvent<HTMLButtonElement>) {
    const enteredPin = typeof pinOrEvent === "string" ? pinOrEvent : undefined
    if (!options || loading) return
    const problems: string[] = []
    if (!code) problems.push(t("Choose a reason code."))
    if (!note.trim()) problems.push(t("Enter a reason note."))
    if (id === "reclassify_tender" && !destinations.some((m) => m.mode === mode)) problems.push(t("Choose the destination tender."))
    if (id === "partial_refund") {
      const trimmed = amount.trim()
      const validDecimals =
        refundDigits === 0
          ? /^\d+$/.test(trimmed)
          : new RegExp(`^\\d+(?:\\.\\d{1,${refundDigits}})?$`).test(trimmed)
      if (!(Number(trimmed) > 0 && Number(trimmed) <= options.refund_cap && validDecimals)) {
        problems.push(
          refundDigits === 0
            ? t("Enter a positive whole-unit refund amount up to {cap}.", { cap: options.refund_cap })
            : t("Enter a positive refund amount up to {cap}, with at most {decimals} decimals.", {
                cap: options.refund_cap,
                decimals: refundDigits,
              }),
        )
      }
    }
    if (approvals && !user) problems.push(options.supervisors.length ? t("Choose an approving supervisor.") : t("No eligible supervisor is available for this property."))
    const blockedPin = supervisor?.locked ? t("The supervisor’s PIN is locked. Try again later.")
      : supervisor?.has_pin === false || supervisor?.must_reset
        ? t("The supervisor must enroll an approval PIN first.")
        : ""
    if (approvals && blockedPin) problems.push(blockedPin)
    setError(problems.join(" "))
    if (problems.length) return
    if (approvals && !enteredPin) {
      setPadError("")
      setPadOpen(true)
      return
    }
    setPadError("")
    setLoading(true)
    try {
      await act(async () => {
        await call("kamra.api.correct_folio_payment", {
          folio,
          payment_row: row,
          action_type: id,
          reason_code: code,
          reason_note: note,
          idempotency_key: key,
          supervisor_user: approvals ? user : undefined,
          supervisor_pin: approvals ? enteredPin : undefined,
          refund_amount: id === "partial_refund" ? Number(amount) : undefined,
          destination_mode: id === "reclassify_tender" ? mode : undefined,
        })
        close()
        setKey(newIdempotencyKey())
      }, (message) => {
        if (!approvals) {
          setError(message)
          return
        }
        if (/PIN_LOCKED|DOA-LOCKED/.test(message)) {
          setPadError(t("The supervisor’s PIN is locked. Try again later."))
        } else if (/PIN_NOT_SET|PIN_MUST_RESET|DOA-NO-PIN|no Floor PIN enrolled|must be (?:reset|re-enrolled)/i.test(message)) {
          setPadError(t("The supervisor must enroll an approval PIN first."))
        } else if (/Wrong cashier PIN\.|Invalid approver PIN\./i.test(message)) {
          setPadError(t("Wrong cashier PIN."))
        } else {
          setPadError(message)
        }
      })
    } finally {
      setLoading(false)
    }
  }
  return (
    <div className="max-w-full whitespace-normal text-start text-xs print:hidden">
      {!options && (
        <button
          disabled={loading}
          className="inline-flex min-h-11 items-center rounded-md border border-line px-2 py-1 text-xs text-accent-ink hover:border-accent-control disabled:opacity-50"
          onClick={open}
        >
          {t("Correct…")}
        </button>
      )}
      {error && <p role="alert" className="rounded border border-critical-line bg-critical-subtle p-2 text-critical-ink dark:border-red-800 dark:bg-red-950 dark:text-red-200">{error}</p>}
      {options && (
        <div className="grid gap-2 rounded border p-2 [&_input]:min-w-0 [&_input]:rounded [&_input]:border [&_input]:p-2 [&_select]:min-w-0 [&_select]:rounded [&_select]:border [&_select]:p-2">
          {[...new Set(options.blocked.map((b) => b.reason))].map((b) => (
            <p key={b}>{t(b)}</p>
          ))}
          {action && (
            <>
              <select
                aria-label={t("Correction action")}
                value={id}
                onChange={(e) =>
                  choose(options.actions.find((a) => a.id === e.target.value))
                }
              >
                {options.actions.map((a) => (
                  <option key={a.id} value={a.id}>
                    {t(a.label)}
                  </option>
                ))}
              </select>
              <select
                aria-label={t("Reason code")}
                value={code}
                onChange={(e) => setCode(e.target.value)}
              >
                {action.reason_codes.map((c) => (
                  <option value={c.name} key={c.name}>
                    {c.description}
                  </option>
                ))}
              </select>
              <input
                aria-label={t("Reason note")}
                placeholder={t("Reason note")}
                value={note}
                onChange={(e) => setNote(e.target.value)}
              />
              {id === "reclassify_tender" && (
                <div>
                  <p>{t("Destination tender")}</p>
                  <TenderPicker home={home} fixedCurrency={destinationCurrency} chosen={{ mode, currency: destinationCurrency }}
                    modes={destinations} onChange={(choice) => setMode(choice.mode)} />
                </div>
              )}
              {id === "partial_refund" && (
                <input
                  aria-label={t("Refund amount")}
                  type="number"
                  min="0"
                  step={refundStep}
                  inputMode={refundDigits === 0 ? "numeric" : "decimal"}
                  max={options.refund_cap}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder={`${t("Refund amount")} (≤ ${options.refund_cap})`}
                />
              )}
              {!!approvals && (
                <>
                  <select
                    aria-label={t("Supervisor user")}
                    value={user}
                    className="min-h-11 bg-surface text-ink"
                    onChange={(e) => setUser(e.target.value)}
                  >
                    <option value="">{t("Select a supervisor")}</option>
                    {options.supervisors.map(s => <option key={s.user} value={s.user}>{s.full_name} ({s.user})</option>)}
                  </select>

                </>
              )}
              <button
                className="min-h-11 rounded bg-accent-fill-hover px-3 py-2 font-semibold text-on-solid disabled:opacity-50"
                disabled={loading}
                onClick={submit}
              >
                {t("Confirm correction")}
              </button>
            </>
          )}
          <button className="min-h-11" disabled={loading} onClick={close}>{t("Close")}</button>
          <PinPad
            open={padOpen}
            title={t("Supervisor PIN")}
            subtitle={supervisor?.full_name}
            submitLabel={t("Approve correction")}
            error={padError}
            busy={loading}
            onSubmit={submit}
            onClose={() => setPadOpen(false)}
          />
        </div>
      )}
    </div>
  )
}
