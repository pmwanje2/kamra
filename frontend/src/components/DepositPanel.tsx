import { SettlementPreview, useSettlementQuote } from "./SettlementPreview"
import TenderPicker, { TenderInput, resolveTender, tenderAmount, tenderModes, tenderReady, useTenderRates } from "./TenderPicker"
/*  Take a deposit on a booking before arrival (#114): record it as paid at
    the desk, or send the guest a payment link. Either way it lands on the
    guest folio as an Advance, so the balance at check-in is right. Used in
    the reservation drawer and on the booking-confirmed screen. */

import { useState } from "react"
import { Check, Copy, Link2, MessageCircle } from "lucide-react"
import { getCurrentProperty, call, newIdempotencyKey } from "../lib/api"
import { serverError } from "../lib/resource"
import { useCashierAuth } from "../lib/cashierAuth"
import { cur, useLocale, fmtAmount } from "../lib/money"
import { useT, t as translate } from "../lib/i18n"
import { Button } from "./ui/button"


export interface DepositState {
  pct: number
  expected: number
  paid: number
  due: number
  link_url: string | null
  link_amount: number | null
  can_take: boolean
}

const fmt = (n: number) =>
  `${cur()}${fmtAmount(n || 0)}`

const inputCls =
  "w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

export default function DepositPanel(props: {
  reservation: string
  deposit: DepositState
  guestPhone?: string | null
  onChanged: (d: DepositState) => void
}) {
  const { t } = useT()
  const loc = useLocale()
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const { deposit } = props
  const [mode, setMode] = useState<"record" | "link" | null>(null)
  const [amount, setAmount] = useState(String(deposit.due || ""))
  const [requested, setRequested] = useState({ mode: "", currency: "" })
  const property = getCurrentProperty()
  const { today: settlementRates } = useTenderRates(property, mode === "record")
  const modes = tenderModes(loc, loc.currency)
  const tender = resolveTender(modes, loc.currency, requested)
  const payCurrency = tender.currency
  const foreign = mode === "record" && !!payCurrency && payCurrency !== loc.currency
  const paymentReady = tenderReady(tender, loc.currency, settlementRates)
  const fx = useSettlementQuote(property, foreign && paymentReady ? payCurrency : "", Number(amount))
  const [reference, setReference] = useState("")
  // one key per "Take advance": a retry after a lost response replays
  const [advKey, setAdvKey] = useState(newIdempotencyKey)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [link, setLink] = useState<{ url: string; message: string; test_mode: boolean } | null>(
    null,
  )
  const [copied, setCopied] = useState(false)

  if (!deposit.can_take && !deposit.paid) return null

  function open(m: "record" | "link") {
    setRequested({ mode: "", currency: "" })
    setMode(m)
    setError(null)
    setAmount(String(deposit.due || ""))
  }

  async function record() {
    setBusy(true)
    setError(null)
    try {
      if (!paymentReady) throw new Error(t("Choose an available payment method and currency."))
      if (foreign && !fx?.quote) throw new Error(fx?.error || t("Wait for the settlement preview."))
      if (pinStatus?.required) await ensureUnlocked()
      const r = await call<{ deposit: DepositState }>("kamra.api.record_advance", {
        reservation: props.reservation,
        amount: Number(amount),
        mode: tender.mode, currency: foreign ? payCurrency : undefined, quote_id: fx?.quote?.quote_id,
        reference: reference.trim() || undefined,
        idempotency_key: advKey,
      })
      setAdvKey(newIdempotencyKey())
      props.onChanged(r.deposit)
      setMode(null)
      setReference("")
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function sendLink() {
    setBusy(true)
    setError(null)
    try {
      const r = await call<{
        url: string
        message: string
        test_mode: boolean
        deposit: DepositState
      }>("kamra.api.deposit_payment_link", {
        reservation: props.reservation,
        amount: Number(amount) || undefined,
      })
      setLink(r)
      props.onChanged(r.deposit)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function simulate() {
    setBusy(true)
    setError(null)
    try {
      const r = await call<{ deposit: DepositState }>("kamra.api.simulate_payment_link", {
        reservation: props.reservation,
      })
      props.onChanged(r.deposit)
      setLink(null)
      setMode(null)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const waPhone = (props.guestPhone || "").replace(/[^\d]/g, "")
  const secured = deposit.expected > 0 && deposit.due <= 0

  return (
    <div className="rounded-xl border border-line bg-surface p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-sm font-semibold text-ink">{t("Deposit")}</h3>
        <span
          className={
            "rounded-full px-2 py-0.5 text-xs font-medium " +
            (secured
              ? "bg-success-subtle text-success-ink"
              : deposit.paid > 0
                ? "bg-warning-subtle text-warning-ink"
                : "bg-subtle text-secondary-ink")
          }
        >
          {secured
            ? t("Secured")
            : deposit.paid > 0
              ? t("Part paid")
              : t("Not paid")}
        </span>
      </div>
      <dl className="mt-2 grid grid-cols-3 gap-2 text-sm">
        <div>
          <dt className="text-xs text-muted-ink">
            {deposit.pct > 0 ? t("Expected ({pct}%)", { pct: deposit.pct }) : t("Expected")}
          </dt>
          <dd className="font-semibold tabular-nums text-ink">
            {deposit.expected > 0 ? fmt(deposit.expected) : "-"}
          </dd>
        </div>
        <div>
          <dt className="text-xs text-muted-ink">{t("Received")}</dt>
          <dd className="font-semibold tabular-nums text-success-ink">{fmt(deposit.paid)}</dd>
        </div>
        <div>
          <dt className="text-xs text-muted-ink">{t("Still due")}</dt>
          <dd
            className={
              "font-semibold tabular-nums " +
              (deposit.due > 0 ? "text-danger-text" : "text-muted-ink")
            }
          >
            {fmt(deposit.due)}
          </dd>
        </div>
      </dl>
      <p className="mt-2 text-xs text-muted-ink">
        {t("Carried to the guest folio - at check-in it counts against the bill.")}
      </p>

      {deposit.link_url && !link && (
        <p className="mt-2 flex items-center gap-1.5 truncate text-xs text-muted-ink">
          <Link2 className="size-3.5 shrink-0" aria-hidden />
          {t("Link sent")}
          {deposit.link_amount ? ` · ${fmt(deposit.link_amount)}` : ""} ·{" "}
          <a href={deposit.link_url} target="_blank" rel="noreferrer" className="truncate text-accent-ink hover:underline">
            {deposit.link_url}
          </a>
        </p>
      )}

      {deposit.can_take && !mode && (
        <div className="mt-3 flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => open("record")}>
            {t("Record payment")}
          </Button>
          {loc.integrations.payment_gateways.length > 0 && (
            <Button variant="outline" onClick={() => open("link")}>
              <Link2 className="size-4" aria-hidden />
              {t("Send payment link")}
            </Button>
          )}
        </div>
      )}

      {mode && (
        <div className="mt-3 space-y-3 rounded-lg bg-canvas p-3">
          {mode === "record" && <TenderPicker home={loc.currency} chosen={tender} modes={modes} rates={settlementRates}
            onChange={(choice) => {
              setRequested(choice)
              if (choice.currency !== payCurrency) setAmount(tenderAmount(deposit.due, choice.currency, loc.currency, settlementRates))
            }} />}
          <TenderInput currency={mode === "record" ? payCurrency : loc.currency} value={amount}
            full={tenderAmount(deposit.due, mode === "record" ? payCurrency : loc.currency, loc.currency, settlementRates)}
            onChange={setAmount} />

          {mode === "record" && (
            <>
              {tender.mode && tender.mode !== "Cash" && (
                <input
                  className={inputCls}
                  placeholder={t("Card slip / transfer ref (optional)")}
                  value={reference}
                  onChange={(e) => setReference(e.target.value)}
                />
              )}
              {foreign && <SettlementPreview result={fx} amount={Number(amount)} balance={deposit.due} />}
              <div className="flex gap-2">
                <Button disabled={busy || !paymentReady || !(Number(amount) > 0) || (foreign && !fx?.quote)} onClick={record}>
                  {busy ? t("Saving…") : t("Record {amt}", { amt: foreign?`${payCurrency} ${amount}`:fmt(Number(amount) || 0) })}
                </Button>
                <Button variant="ghost" onClick={() => setMode(null)}>
                  {t("Cancel")}
                </Button>
              </div>
            </>
          )}

          {mode === "link" && !link && (
            <div className="flex gap-2">
              <Button disabled={busy || !(Number(amount) > 0)} onClick={sendLink}>
                {busy ? t("Creating…") : t("Create link for {amt}", { amt: foreign?`${payCurrency} ${amount}`:fmt(Number(amount) || 0) })}
              </Button>
              <Button variant="ghost" onClick={() => setMode(null)}>
                {t("Cancel")}
              </Button>
            </div>
          )}

          {mode === "link" && link && (
            <div className="space-y-2">
              <p className="break-all rounded-md border border-line bg-surface px-2.5 py-2 text-xs text-secondary-ink">
                {translate(link.message)}
              </p>
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  onClick={async () => {
                    try {
                      await navigator.clipboard.writeText(link.message)
                      setCopied(true)
                      setTimeout(() => setCopied(false), 1500)
                    } catch {
                      /* clipboard blocked - the text is selectable */
                    }
                  }}
                >
                  {copied ? <Check className="size-4" aria-hidden /> : <Copy className="size-4" aria-hidden />}
                  {copied ? t("Copied") : t("Copy message")}
                </Button>
                {waPhone && (
                  <a
                    className="inline-flex items-center gap-1.5 rounded-lg border border-control px-3 py-2 text-sm font-medium text-secondary-ink hover:bg-canvas"
                    href={`https://wa.me/${waPhone}?text=${encodeURIComponent(link.message)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    <MessageCircle className="size-4" aria-hidden />{translate("WhatsApp")}</a>
                )}
                {link.test_mode && (
                  <Button variant="ghost" disabled={busy} onClick={simulate}>
                    {t("Simulate payment (test mode)")}
                  </Button>
                )}
              </div>
              <p className="text-xs text-muted-ink">
                {t("When the guest pays, the deposit posts here automatically.")}
              </p>
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="mt-3 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
          {translate(error)}
        </div>
      )}
    </div>
  )
}
