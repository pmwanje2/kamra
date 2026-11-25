import { useEffect, useMemo, useState } from "react"
import { call, newIdempotencyKey } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Sheet } from "../components/ui/sheet"
import { fmtMoney, locale, moneyEps, moneyStep, roundMoney, useLocale } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"

// Record a company's payment against its city ledger (AR) balance. The
// server (kamra.ledger.city_ledger_settle) decides what it clears; this
// screen previews the allocation and asks for a confirm first.

type OpenItem = {
  folio: string
  invoice_number?: string | null
  invoice_date: string
  due_date?: string
  days_overdue?: number
  guest_name?: string | null
  reservation?: string | null
  billed: number
  paid: number
  outstanding: number
}

type Account = {
  account: string
  company: string
  account_number?: string
  balance: number
  open_balance: number
  unapplied_credit: number
  credit_limit?: number
  credit_days?: number
  not_due: number
  d1_30: number
  d31_60: number
  d61_90: number
  d90p: number
  overdue: number
  open_items: OpenItem[]
  payments: { name: string; posting_date: string; credit: number; debit?: number; reference?: string; reverses?: string | null }[]
  payment_pin_required?: boolean
}

type Result = {
  payment?: string
  balance?: number
  applied: { folio: string; amount: number }[]
  unapplied: number
  // an AI agent's payment waits for Finance (never from this screen's user)
  pending?: boolean
}

// plus the country's instant modes (a wallet / instant transfer)
const modes = () => ["Bank Transfer", "Cheque", "Card", ...locale().instant_payment_modes, "Cash", "Other"]

export default function CityLedgerPayment(props: {
  account: string
  onClose: () => void
  onPosted: () => void
}) {
  useUILanguage()
  const loc = useLocale()
  const cur = loc.currency
  const eps = moneyEps(cur)
  const inputStep = moneyStep(cur)
  const [acc, setAcc] = useState<Account | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [amount, setAmount] = useState("")
  const [mode, setMode] = useState("Bank Transfer")
  const [reference, setReference] = useState("")
  // blank = the business date
  const [receivedOn, setReceivedOn] = useState("")
  const [pin, setPin] = useState("")
  const [manual, setManual] = useState(false)
  const [picks, setPicks] = useState<Record<string, string>>({})
  const [step, setStep] = useState<"form" | "confirm" | "done">("form")
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<Result | null>(null)
  // one key per payment: a retry of the same press sends it again and the
  // server posts nothing the second time; a new key after it lands
  const [payKey, setPayKey] = useState(newIdempotencyKey)

  useEffect(() => {
    call<Account>("kamra.ledger.city_ledger_account", { account: props.account })
      .then(setAcc)
      .catch((e) => setErr(serverError(e)))
  }, [props.account])

  const amt = Number(amount) || 0
  const items = useMemo(() => acc?.open_items ?? [], [acc])

  // what the payment will clear - mirrors the server's rule for the preview
  const plan = useMemo(() => {
    if (manual) {
      return items
        .filter((it) => picks[it.folio] !== undefined)
        .map((it) => ({ folio: it.folio, amount: roundMoney(Number(picks[it.folio]) || 0, cur) }))
        .filter((p) => p.amount > 0)
    }
    let left = roundMoney(amt, cur)
    const out: { folio: string; amount: number }[] = []
    for (const it of items) {
      if (left <= 0) break
      const take = roundMoney(Math.min(left, it.outstanding), cur)
      if (take > 0) {
        out.push({ folio: it.folio, amount: take })
        left = roundMoney(left - take, cur)
      }
    }
    return out
  }, [manual, items, picks, amt, cur])

  const applied = roundMoney(plan.reduce((s, p) => s + p.amount, 0), cur)
  const unapplied = roundMoney(amt - applied, cur)

  const problem = (() => {
    if (roundMoney(amt, cur) <= 0) return "Enter the amount received."
    if (manual) {
      for (const p of plan) {
        const it = items.find((i) => i.folio === p.folio)
        if (it && p.amount > it.outstanding + eps)
          return `${it.invoice_number || it.folio} owes ${fmtMoney(it.outstanding)}.`
      }
      if (applied > amt + eps)
        return "The folios add up to more than the payment."
    }
    return null
  })()

  const toggle = (it: OpenItem) =>
    setPicks((curPicks) => {
      const next = { ...curPicks }
      if (next[it.folio] !== undefined) delete next[it.folio]
      else {
        const used = Object.values(next).reduce((s, v) => s + (Number(v) || 0), 0)
        const room = amt > 0 ? Math.max(roundMoney(amt - used, cur), 0) : it.outstanding
        next[it.folio] = String(roundMoney(Math.min(it.outstanding, room), cur))
      }
      return next
    })

  const submit = async () => {
    setBusy(true)
    setErr(null)
    try {
      const out = await call<Result>("kamra.ledger.city_ledger_settle", {
        account: props.account,
        amount: amt,
        mode,
        reference: reference.trim() || undefined,
        allocations: manual ? JSON.stringify(plan) : undefined,
        idempotency_key: payKey,
        received_on: receivedOn || undefined,
        pin: pin || undefined,
      })
      setPayKey(newIdempotencyKey())
      setResult(out)
      setStep("done")
      props.onPosted()
    } catch (e) {
      setErr(serverError(e))
      setStep("form")
    } finally {
      setBusy(false)
    }
  }

  const label = (folio: string) => {
    const it = items.find((i) => i.folio === folio)
    return it ? `${it.invoice_number || it.folio}${it.guest_name ? ` · ${it.guest_name}` : ""}` : folio
  }

  const footer =
    step === "form" ? (
      <div className="flex justify-end gap-2">
        <Button variant="outline" onClick={props.onClose}>{translate("Cancel")}</Button>
        <Button disabled={!!problem || !acc} onClick={() => setStep("confirm")}>{translate("Review payment")}</Button>
      </div>
    ) : step === "confirm" ? (
      <div className="flex justify-end gap-2">
        <Button variant="outline" disabled={busy} onClick={() => setStep("form")}>{translate("Back")}</Button>
        <Button disabled={busy || (!!acc?.payment_pin_required && !pin)} onClick={() => void submit()}>
          {busy ? translate("Recording…") : translate("Confirm {v0}", { v0: String(fmtMoney(amt)) })}
        </Button>
      </div>
    ) : (
      <div className="flex justify-end">
        <Button onClick={props.onClose}>{translate("Done")}</Button>
      </div>
    )

  return (
    <Sheet
      title={translate("Record company payment")}
      description={(acc ? translate("{v0} · city ledger", { v0: String(acc.company) }) : translate("City ledger"))}
      onClose={props.onClose}
      footer={footer}
      wide
    >
      {err ? <p className="mb-3 text-sm text-danger-text">{translate(err)}</p> : null}
      {!acc ? (
        <p className="text-sm text-muted-ink">{translate("Loading account…")}</p>
      ) : step === "done" && result ? (
        <div className="space-y-3 text-sm">
          {result.pending ? (
            <p className="rounded-lg border border-warning-line bg-warning-subtle px-4 py-3 text-warning-ink">{translate("Recorded for Finance to confirm. It does not change the account until a Finance user confirms it with their PIN.")}</p>
          ) : (
            <p className="rounded-lg border border-success-line bg-success-subtle px-4 py-3 text-success-ink">{translate("Payment recorded. Account balance is now ")}{fmtMoney(result.balance ?? 0)}.
            </p>
          )}
          <ul className="space-y-1">
            {result.applied.map((a) => (
              <li key={a.folio} className="flex justify-between">
                <span>{label(a.folio)}</span>
                <span className="tabular-nums">{fmtMoney(a.amount)}</span>
              </li>
            ))}
          </ul>
          {result.unapplied > 0 ? (
            <p className="text-muted-ink">
              {fmtMoney(result.unapplied)}{translate(" kept as unapplied credit. It settles the company's next city-ledger bill.")}</p>
          ) : null}
        </div>
      ) : step === "confirm" ? (
        <div className="space-y-4 text-sm">
          <p>{translate("Record ")}<b>{fmtMoney(amt)}</b>{translate(" from ")}<b>{acc.company}</b>{translate(" by ")}{translate(mode)}
            {reference.trim() ? translate(" (ref {v0})", { v0: String(reference.trim()) }) : ""}
            {receivedOn ? translate(", received {v0}", { v0: receivedOn }) : ""}.
          </p>
          <div className="rounded-lg border border-line">
            <div className="border-b border-line-subtle px-4 py-2 text-xs uppercase text-muted-ink">
              {manual ? translate("Applied to the folios you picked") : translate("Applied oldest bill first")}
            </div>
            <ul className="divide-y divide-line-subtle">
              {plan.map((p) => (
                <li key={p.folio} className="flex justify-between px-4 py-2">
                  <span>{label(p.folio)}</span>
                  <span className="tabular-nums">{fmtMoney(p.amount)}</span>
                </li>
              ))}
              {!plan.length ? (
                <li className="px-4 py-2 text-muted-ink">{translate("No folio. All of it stays as credit.")}</li>
              ) : null}
            </ul>
          </div>
          {unapplied > 0 ? (
            <p className="text-warning-ink">
              {fmtMoney(unapplied)}{translate(" is more than the bills it pays. It stays on the account as unapplied credit.")}</p>
          ) : null}
          <p className="text-muted-ink">{translate("Balance after: ")}{fmtMoney(roundMoney(acc.balance - amt, cur))}
          </p>
          {acc.payment_pin_required ? (
            <label className="block max-w-xs">
              <span className="mb-1 block text-muted-ink">{translate("Your cashier PIN")}</span>
              <input type="password" inputMode="numeric" autoComplete="off"
                className="w-full rounded-lg border border-control px-2 py-1.5"
                value={pin} onChange={(e) => setPin(e.target.value)} />
            </label>
          ) : null}
        </div>
      ) : (
        <div className="space-y-5">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Stat label={translate("Balance")} value={acc.balance} />
            <Stat label={translate("Open bills")} value={acc.open_balance} />
            <Stat label={translate("Unapplied credit")} value={acc.unapplied_credit} />
            <Stat label={translate("Overdue")} value={acc.overdue} />
          </div>
          <p className="-mt-2 text-xs text-muted-ink">{translate("Terms: ")}{acc.credit_days ?? 0}{translate(" days from the invoice date")}{Number(acc.credit_limit || 0) > 0
              ? translate(" · Credit limit {v0}", { v0: String(fmtMoney(Number(acc.credit_limit))) })
              : translate(" · No credit limit")}
          </p>
          <div className="grid grid-cols-5 overflow-hidden rounded-lg border border-line text-center text-xs">
            {([
              ["Not due", acc.not_due],
              ["1 to 30 overdue", acc.d1_30],
              ["31 to 60", acc.d31_60],
              ["61 to 90", acc.d61_90],
              ["90+", acc.d90p],
            ] as const).map(([k, v]) => (
              <div key={k} className="border-r border-line-subtle px-2 py-2 last:border-r-0">
                <div className="text-muted-ink">{translate(k)}</div>
                <div className="font-medium tabular-nums">{fmtMoney(v)}</div>
              </div>
            ))}
          </div>

          <div className="grid gap-3 sm:grid-cols-4">
            <label className="text-sm">
              <span className="mb-1 block text-muted-ink">{translate("Amount received")}</span>
              <input
                type="number"
                min="0"
                step={inputStep}
                inputMode="decimal"
                className="w-full rounded-lg border border-control px-2 py-1.5"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                autoFocus
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-muted-ink">{translate("Mode")}</span>
              <select
                className="w-full rounded-lg border border-control px-2 py-1.5"
                value={translate(mode)}
                onChange={(e) => setMode(e.target.value)}
              >
                {modes().map((m) => <option key={m} value={m}>{translate(m)}</option>)}
              </select>
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-muted-ink">{translate("Reference (UTR / cheque no.)")}</span>
              <input
                className="w-full rounded-lg border border-control px-2 py-1.5"
                value={reference}
                maxLength={120}
                onChange={(e) => setReference(e.target.value)}
              />
            </label>
            <label className="text-sm">
              <span className="mb-1 block text-muted-ink">{translate("Received on")}</span>
              <input
                type="date"
                className="w-full rounded-lg border border-control px-2 py-1.5"
                value={receivedOn}
                onChange={(e) => setReceivedOn(e.target.value)}
                title={translate("Leave blank for today's business date")}
              />
            </label>
          </div>

          <div className="flex flex-wrap gap-2 text-sm" role="radiogroup">
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={!manual} onChange={() => setManual(false)} />{translate("Allocate oldest first")}</label>
            <label className="flex items-center gap-1.5">
              <input type="radio" checked={manual} onChange={() => setManual(true)} />{translate("Pick folios")}</label>
          </div>

          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <thead className="text-xs uppercase text-muted-ink">
                <tr>
                  {manual ? <th className="py-2 pr-2" /> : null}
                  <th className="py-2 pr-3">{translate("Bill")}</th>
                  <th className="py-2 pr-3">{translate("Guest")}</th>
                  <th className="py-2 pr-3">{translate("Billed on")}</th>
                  <th className="py-2 pr-3">{translate("Due")}</th>
                  <th className="py-2 pr-3 text-right">{translate("Owed")}</th>
                  <th className="py-2 text-right">{translate("This payment")}</th>
                </tr>
              </thead>
              <tbody>
                {items.map((it) => {
                  const p = plan.find((x) => x.folio === it.folio)
                  const picked = picks[it.folio] !== undefined
                  return (
                    <tr key={it.folio} className="border-t border-line-subtle">
                      {manual ? (
                        <td className="py-2 pr-2">
                          <input type="checkbox" checked={picked} onChange={() => toggle(it)}
                            aria-label={translate("Pay {v0}", { v0: String(it.invoice_number || it.folio) })} />
                        </td>
                      ) : null}
                      <td className="py-2 pr-3 font-mono text-xs">{it.invoice_number || it.folio}</td>
                      <td className="py-2 pr-3">{it.guest_name || "-"}</td>
                      <td className="py-2 pr-3">{it.invoice_date}</td>
                      <td className={`py-2 pr-3 ${it.days_overdue ? "text-danger-ink" : ""}`}>
                        {it.due_date || "-"}
                        {it.days_overdue ? (
                          <span className="ml-1 text-xs">({it.days_overdue}{translate("d late)")}</span>
                        ) : null}
                      </td>
                      <td className="py-2 pr-3 text-right tabular-nums">{fmtMoney(it.outstanding)}</td>
                      <td className="py-2 text-right tabular-nums">
                        {manual && picked ? (
                          <input
                            type="number"
                            min="0"
                            step={inputStep}
                            className="w-28 rounded border border-control px-1.5 py-1 text-right"
                            value={picks[it.folio]}
                            onChange={(e) =>
                              setPicks((curPicks) => ({ ...curPicks, [it.folio]: e.target.value }))}
                          />
                        ) : p ? fmtMoney(p.amount) : "-"}
                      </td>
                    </tr>
                  )
                })}
                {!items.length ? (
                  <tr>
                    <td colSpan={manual ? 7 : 6} className="py-6 text-center text-muted-ink">{translate("No open bills. A payment now is kept as credit.")}</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>

          <p className={`text-sm ${problem ? "text-danger-text" : "text-muted-ink"}`}>
            {problem ??
              `Applies ${fmtMoney(applied)}` +
                (unapplied > 0 ? translate(" · {v0} stays as unapplied credit", { v0: String(fmtMoney(unapplied)) }) : "")}
          </p>

          {acc.payments.length ? (
            <div>
              <p className="mb-1 text-xs uppercase text-muted-ink">{translate("Recent payments")}</p>
              <ul className="space-y-0.5 text-sm text-secondary-ink">
                {acc.payments.map((p) => (
                  <li key={p.name} className="flex justify-between">
                    <span>{p.posting_date} · {p.reference || "-"}</span>
                    <span className={`tabular-nums ${p.reverses ? "text-danger-ink" : ""}`}>{fmtMoney(p.reverses ? -(p.debit ?? 0) : p.credit)}</span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </Sheet>
  )
}

function Stat({ label, value }: { label: string; value: number }) {
  useUILanguage()
  useLocale()
  return (
    <div className="rounded-lg border border-line px-3 py-2">
      <div className="text-xs text-muted-ink">{translate(label)}</div>
      <div className="text-lg font-semibold tabular-nums">{fmtMoney(value)}</div>
    </div>
  )
}
