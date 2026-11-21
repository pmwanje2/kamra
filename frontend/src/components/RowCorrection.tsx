import { useState } from "react"
import { call, newIdempotencyKey } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"

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
  refund_cap: number
}
export default function RowCorrection({
  folio,
  row,
  modes,
  act,
}: {
  folio: string
  row: string
  modes: string[]
  act: (fn: () => Promise<unknown>) => Promise<void>
}) {
  const { t } = useT()
  const [options, setOptions] = useState<Options>(),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false)
  const [id, setId] = useState(""),
    [code, setCode] = useState(""),
    [note, setNote] = useState(""),
    [user, setUser] = useState(""),
    [pin, setPin] = useState("")
  const [amount, setAmount] = useState(""),
    [mode, setMode] = useState(""),
    [key, setKey] = useState(newIdempotencyKey)
  const action = options?.actions.find((a) => a.id === id),
    approvals =
      action?.requires.includes("supervisor_user") ||
      action?.reason_codes.find((c) => c.name === code)?.requires_supervisor
  const choose = (a?: Action) => {
    setId(a?.id ?? "")
    setCode(a?.reason_codes[0]?.name ?? "")
  }
  const close = () => {
    setOptions(undefined)
    setUser("")
    setPin("")
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
      choose(opts.actions[0])
    } catch (e) {
      setError(serverError(e))
    } finally {
      setLoading(false)
    }
  }
  async function submit() {
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
          supervisor_pin: approvals ? pin : undefined,
          refund_amount: id === "partial_refund" ? Number(amount) : undefined,
          destination_mode: id === "reclassify_tender" ? mode : undefined,
        })
        close()
        setKey(newIdempotencyKey())
      })
    } finally {
      setLoading(false)
    }
  }
  return (
    <div className="max-w-full whitespace-normal text-left text-xs print:hidden">
      {!options && (
        <button
          disabled={loading}
          className="text-brand-700 underline"
          onClick={open}
        >
          {t("Correct…")}
        </button>
      )}
      {error && <p role="alert">{error}</p>}
      {options && (
        <div className="grid gap-2 rounded border p-2">
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
                <select
                  aria-label={t("Destination tender")}
                  value={mode}
                  onChange={(e) => setMode(e.target.value)}
                >
                  <option value="">{t("Destination tender")}</option>
                  {modes.map((m) => (
                    <option key={m}>{m}</option>
                  ))}
                </select>
              )}
              {id === "partial_refund" && (
                <input
                  aria-label={t("Refund amount")}
                  type="number"
                  step="0.01"
                  max={options.refund_cap}
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder={`${t("Refund amount")} (≤ ${options.refund_cap})`}
                />
              )}
              {!!approvals && (
                <>
                  <input
                    aria-label={t("Supervisor user")}
                    placeholder={t("Supervisor user")}
                    value={user}
                    onChange={(e) => setUser(e.target.value)}
                  />
                  <input
                    aria-label={t("Supervisor PIN")}
                    type="password"
                    autoComplete="off"
                    placeholder={t("Supervisor PIN")}
                    value={pin}
                    onChange={(e) => setPin(e.target.value)}
                  />
                </>
              )}
              <button
                disabled={
                  loading ||
                  !note.trim() ||
                  !code ||
                  (!!approvals && (!user || !pin)) ||
                  (id === "partial_refund" &&
                    !(
                      Number(amount) > 0 && Number(amount) <= options.refund_cap
                    )) ||
                  (id === "reclassify_tender" && !mode)
                }
                onClick={submit}
              >
                {t("Confirm correction")}
              </button>
            </>
          )}
          <button onClick={close}>{t("Close")}</button>
        </div>
      )}
    </div>
  )
}
