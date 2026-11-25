/*  The refundable security (damage) deposit and card pre-authorization hold
    on a stay. Cash/bank deposits sit on the guest folio as a liability; card
    pre-authorization holds reserve funds without posting to the general ledger
    until captured for damage or released at check-out. Every action is one
    server call; cashier PIN is checked where money or holds move. */

import { useState } from "react"
import { CreditCard, ShieldCheck } from "lucide-react"
import { call, newIdempotencyKey } from "../lib/api"
import { useAuth } from "../lib/auth"
import { serverError } from "../lib/resource"
import { useCashierAuth } from "../lib/cashierAuth"
import { cur, fmtAmount, moneyDigits, moneyEps, moneyStep, useLocale } from "../lib/money"
import { t as translate, useT } from "../lib/i18n"
import { Button } from "./ui/button"

export interface SecurityDepositState {
  name: string | null
  status:
    | "None"
    | "Required"
    | "Waived"
    | "Authorized"
    | "Collected"
    | "Held"
    | "Withheld"
    | "Refunded"
    | "Released"
    | "Expired"
  method?: string | null
  policy_amount: number
  required: number
  authorized_amount?: number
  remaining_hold_amount?: number
  collected: number
  withheld: number
  refunded: number
  held: number
  due: number
  reason: string | null
  folio: string | null
  folio_credit: number
  preauth_hold?: string | null
  gateway_auth_ref?: string | null
  card_brand?: string | null
  card_last4?: string | null
  expires_on?: string | null
  payment_entry?: string | null
  pending_capture_payment_entry?: string | null
  preauth_available?: boolean
  history: { kind: string; date: string; amount: number; mode?: string; note?: string }[]
  can_collect: boolean
  can_waive: boolean
  can_withhold: boolean
  can_refund: boolean
  can_authorize?: boolean
  can_increment?: boolean
  can_capture?: boolean
  can_release?: boolean
  blocks_checkout: boolean
  blocks_checkout_card_hold?: boolean
}

type Mode = "collect" | "waive" | "withhold" | "refund" | "authorize" | "increment" | "capture" | "release"

const ADMIN = ["System Manager", "Administrator"]
// mirrors the server's gates (the server decides; this only hides buttons)
const GATES: Record<Mode, string[]> = {
  collect: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  waive: ["Front Desk", "Hotel Admin", ...ADMIN],
  withhold: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  refund: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  authorize: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  increment: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  capture: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
  release: ["Front Desk", "Finance", "Hotel Admin", ...ADMIN],
}

const LAST4_RE = /^[0-9]{4}$/
const CVV_RE = /(?:^|[^a-z0-9])(?:cvv2|cvc2|cvv|cvc|cid|csc)[\s:=#./-]*[0-9]{3,4}(?![0-9])/i
const TRACK_RE = /%b|;[0-9 -]{4,}=/i

function hasPanSequence(value: string): boolean {
  let run = 0
  for (const ch of value) {
    if (ch >= "0" && ch <= "9") {
      run += 1
    } else if ((ch === " " || ch === "-") && run > 0) {
      continue
    } else {
      if (run >= 13 && run <= 19) return true
      run = 0
    }
  }
  return run >= 13 && run <= 19
}

function hasPciViolation(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  return hasPanSequence(trimmed) || CVV_RE.test(trimmed) || TRACK_RE.test(trimmed)
}

function defaultExpiryLocal(): string {
  const dt = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000)
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`
}

const inputCls =
  "min-h-11 w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm text-ink " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

const TONE: Record<string, string> = {
  Authorized: "bg-info-subtle text-info-ink dark:bg-sky-50 dark:text-sky-700",
  Held: "bg-violet-subtle text-violet-ink dark:bg-violet-950/60 dark:text-violet-200",
  Collected: "bg-violet-subtle text-violet-ink dark:bg-violet-950/60 dark:text-violet-200",
  Required: "bg-warning-subtle text-warning-ink dark:bg-amber-50 dark:text-amber-800",
  Waived: "bg-subtle text-secondary-ink",
  Withheld: "bg-danger-subtle text-danger-ink dark:bg-rose-50 dark:text-rose-700",
  Refunded: "bg-success-subtle text-success-ink dark:bg-emerald-50 dark:text-emerald-700",
  Released: "bg-success-subtle text-success-ink dark:bg-emerald-50 dark:text-emerald-700",
  Expired: "bg-warning-subtle text-warning-ink dark:bg-amber-50 dark:text-amber-800",
  None: "bg-subtle text-muted-ink",
}

export default function SecurityDepositPanel(props: {
  reservation: string
  deposit: SecurityDepositState
  /** only an in-house or upcoming stay offers to take one when none is set */
  canStart: boolean
  onChanged: () => void
}) {
  const { t } = useT()
  const loc = useLocale()
  const { roles } = useAuth()
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const d = props.deposit
  const currency = loc.currency || undefined
  const digits = moneyDigits(currency)
  const step = moneyStep(currency)
  const eps = moneyEps(currency)
  const fmt = (n: number) => `${cur()}${fmtAmount(n || 0, currency, digits)}`

  const [mode, setMode] = useState<Mode | null>(null)
  const [amount, setAmount] = useState("")
  const [payMode, setPayMode] = useState(loc.payment_modes[0] ?? "Cash")
  const [text, setText] = useState("")
  const [gatewayRef, setGatewayRef] = useState("")
  const [cardBrand, setCardBrand] = useState("")
  const [cardLast4, setCardLast4] = useState("")
  const [expiresOn, setExpiresOn] = useState("")
  const [evidence, setEvidence] = useState("")
  const [busy, setBusy] = useState(false)
  const [newCaptureDraft, setNewCaptureDraft] = useState<{ deposit: SecurityDepositState; name: string } | null>(null)
  const pendingCapture = d.pending_capture_payment_entry ??
    (newCaptureDraft?.deposit === d ? newCaptureDraft.name : null)
  const [error, setError] = useState<string | null>(null)
  // one key per Collect press; reused on retry (#550)
  const [collectKey, setCollectKey] = useState(newIdempotencyKey)

  const allowed = (m: Mode) => GATES[m].some((r) => roles.includes(r))
  const authorizedAmt = Number(d.authorized_amount ?? 0)
  const remainingHold = Number(d.remaining_hold_amount ?? (d.status === "Authorized" ? authorizedAmt : 0))
  const hasPreauth = Boolean(
    d.preauth_hold || authorizedAmt > eps || d.status === "Authorized" || d.status === "Released" || d.status === "Expired",
  )
  const nothingYet = d.status === "None" && d.history.length === 0 && !hasPreauth
  if (nothingYet && !props.canStart) return null

  function open(m: Mode) {
    setMode(m)
    setError(null)
    setText("")
    setEvidence("")
    setGatewayRef("")
    setCardBrand("")
    setCardLast4("")
    setExpiresOn("")
    const last = [...d.history].reverse().find((h) => h.kind === "Collected")?.mode
    if (m === "collect") setAmount(String(d.due || d.policy_amount || ""))
    if (m === "authorize") {
      setAmount(String(d.due || d.policy_amount || ""))
      setExpiresOn(defaultExpiryLocal())
    }
    if (m === "increment") {
      setAmount("")
      setExpiresOn("")
    }
    if (m === "capture") setAmount(String(remainingHold || authorizedAmt || ""))
    if (m === "withhold") setAmount("")
    if (m === "refund") {
      setAmount(String(Math.min(d.held, d.folio_credit) || ""))
      if (last && loc.payment_modes.includes(last)) setPayMode(last)
    }
  }

  const last4Trimmed = cardLast4.trim()
  const last4Invalid = mode === "authorize" && last4Trimmed.length > 0 && !LAST4_RE.test(last4Trimmed)
  const pciBlocked =
    last4Invalid ||
    hasPciViolation(cardLast4) ||
    hasPciViolation(gatewayRef) ||
    hasPciViolation(cardBrand) ||
    hasPciViolation(text) ||
    hasPciViolation(evidence)

  async function submit() {
    if (!mode || busy || pciBlocked || (mode === "capture" && pendingCapture)) return
    setBusy(true)
    setError(null)
    try {
      if (mode !== "waive" && pinStatus?.required) await ensureUnlocked()
      const args: Record<string, unknown> = { reservation: props.reservation }
      let method = ""
      if (mode === "collect") {
        method = "kamra.api.collect_security_deposit"
        Object.assign(args, {
          amount: Number(amount),
          mode: payMode,
          reference: text.trim() || undefined,
          idempotency_key: collectKey,
        })
      } else if (mode === "waive") {
        method = "kamra.api.waive_security_deposit"
        args.reason = text.trim()
      } else if (mode === "withhold") {
        method = "kamra.api.withhold_security_deposit"
        Object.assign(args, { amount: Number(amount), reason: text.trim() })
      } else if (mode === "refund") {
        method = "kamra.api.refund_security_deposit"
        Object.assign(args, { amount: Number(amount), mode: payMode, note: text.trim() || undefined })
      } else if (mode === "authorize") {
        method = "kamra.deposit.authorize_deposit"
        Object.assign(args, {
          amount: Number(amount),
          gateway_auth_ref: gatewayRef.trim(),
          card_brand: cardBrand.trim() || undefined,
          card_last4: last4Trimmed,
          expires_on: expiresOn.trim(),
        })
      } else if (mode === "increment") {
        method = "kamra.deposit.increment_deposit_auth"
        Object.assign(args, {
          additional_amount: Number(amount),
          gateway_auth_ref: gatewayRef.trim() || undefined,
          expires_on: expiresOn.trim() || undefined,
        })
      } else if (mode === "capture") {
        method = "kamra.deposit.capture_deposit_auth"
        Object.assign(args, {
          amount: Number(amount),
          reason: text.trim(),
          evidence: evidence.trim() || undefined,
        })
      } else {
        method = "kamra.deposit.release_deposit_auth"
        Object.assign(args, { reason: text.trim() || undefined })
      }
      const result = await call<{ capture_mode?: string; pending_payment_entry?: string }>(method, args)
      if (mode === "capture" && result?.capture_mode === "draft_pending_submit" && result.pending_payment_entry) {
        setNewCaptureDraft({ deposit: d, name: result.pending_payment_entry })
      }
      if (mode === "collect") setCollectKey(newIdempotencyKey())
      setMode(null)
      props.onChanged()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const amt = Number(amount) || 0
  const maxCapture = remainingHold > eps ? remainingHold : authorizedAmt
  const ready =
    !pciBlocked &&
    (mode === "waive"
      ? text.trim().length > 0
      : mode === "withhold"
        ? amt > 0 && amt <= d.held + eps && text.trim().length > 0
        : mode === "refund"
          ? amt > 0 && amt <= d.held + eps
          : mode === "authorize"
            ? amt > 0 &&
              gatewayRef.trim().length > 0 &&
              LAST4_RE.test(last4Trimmed) &&
              expiresOn.trim().length > 0
            : mode === "increment"
              ? amt > 0
              : mode === "capture"
                ? !pendingCapture && amt > 0 && amt <= maxCapture + eps && text.trim().length > 0
                : mode === "release"
                  ? true
                  : amt > 0)

  const label: Record<Mode, string> = {
    collect: t("Collect {amt}", { amt: fmt(amt) }),
    waive: t("Waive deposit"),
    withhold: t("Withhold {amt}", { amt: fmt(amt) }),
    refund: t("Refund {amt}", { amt: fmt(amt) }),
    authorize: t("Authorize {amt}", { amt: fmt(amt) }),
    increment: t("Increment hold by {amt}", { amt: fmt(amt) }),
    capture: t("Capture {amt}", { amt: fmt(amt) }),
    release: t("Confirm release"),
  }

  const maskedCard =
    d.card_brand && d.card_last4
      ? `${d.card_brand} •••• ${d.card_last4}`
      : d.card_last4
        ? `•••• ${d.card_last4}`
        : null

  return (
    <div
      className={
        "rounded-xl border bg-surface p-4 text-start " +
        (d.blocks_checkout
          ? "border-warning-line-strong dark:border-amber-600/70"
          : "border-line")
      }
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="flex items-center gap-2 text-sm font-semibold text-ink">
          <ShieldCheck className="size-4 text-muted-ink" aria-hidden />
          {t("Security deposit")}
        </h3>
        <span className={"rounded-full px-2.5 py-0.5 text-xs font-medium " + (TONE[d.status] ?? TONE.None)}>
          {d.status === "None" ? t("None taken") : t(d.status)}
        </span>
      </div>

      {!nothingYet && (
        <dl className="mt-2 grid grid-cols-2 gap-2 text-sm sm:grid-cols-5">
          <div>
            <dt className="text-xs text-muted-ink">{t("Required")}</dt>
            <dd className="font-semibold tabular-nums text-ink">
              {d.required > 0 ? fmt(d.required) : "-"}
            </dd>
          </div>
          {hasPreauth && (
            <div>
              <dt className="text-xs text-muted-ink">{t("Authorized hold")}</dt>
              <dd className="font-semibold tabular-nums text-info-ink">{fmt(authorizedAmt)}</dd>
            </div>
          )}
          <div>
            <dt className="text-xs text-muted-ink">{t("Held")}</dt>
            <dd className="font-semibold tabular-nums text-violet-ink dark:text-violet-300">{fmt(d.held)}</dd>
          </div>
          <div>
            <dt className="text-xs text-muted-ink">{t("Withheld")}</dt>
            <dd className={"font-semibold tabular-nums " + (d.withheld > 0 ? "text-danger-text" : "text-muted-ink")}>
              {fmt(d.withheld)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-muted-ink">{t("Refunded")}</dt>
            <dd className={"font-semibold tabular-nums " + (d.refunded > 0 ? "text-success-ink" : "text-muted-ink")}>
              {fmt(d.refunded)}
            </dd>
          </div>
        </dl>
      )}

      {hasPreauth && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border border-info-line bg-info-subtle px-3 py-2 text-xs text-info-ink dark:border-sky-200 dark:bg-sky-50 dark:text-sky-900">
          <span className="inline-flex items-center gap-1.5 font-medium">
            <CreditCard className="size-3.5 shrink-0" aria-hidden />
            {maskedCard ?? t("Card pre-authorization")}
          </span>
          {d.preauth_hold && (
            <span className="font-mono">{d.preauth_hold}</span>
          )}
          {d.gateway_auth_ref && (
            <span>
              {t("Auth ref")}: <span className="font-mono">{d.gateway_auth_ref}</span>
            </span>
          )}
          {d.expires_on && (
            <span className="tabular-nums">
              {t("Expires")}: {d.expires_on}
            </span>
          )}
          {d.payment_entry && (
            <span className="font-mono">
              {t("Receipt")}: {d.payment_entry}
            </span>
          )}
        </div>
      )}

      <p className="mt-2 text-xs text-muted-ink">
        {nothingYet
          ? t("Refundable damage deposit - held for the guest, not revenue. Refunded or withheld at check-out.")
          : d.status === "Waived" && d.reason
            ? t("Waived: {reason}", { reason: d.reason })
            : d.status === "Authorized"
              ? t("Card hold reserved without posting to the general ledger until captured.")
              : d.status === "Released"
                ? t("Card pre-authorization hold released with no charge posted.")
                : d.status === "Expired"
                  ? t("Card pre-authorization hold expired without capture.")
                  : t("Held on the guest folio as a liability - not revenue.")}
      </p>

      {d.history.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs text-secondary-ink">
          {d.history.map((h, i) => (
            <li key={i} className="flex justify-between gap-3">
              <span className="min-w-0 truncate">
                {h.date} · {t(h.kind)}
                {h.mode ? ` · ${t(h.mode)}` : ""}
                {h.note ? ` · ${h.note}` : ""}
              </span>
              <span className="shrink-0 tabular-nums">{fmt(h.amount)}</span>
            </li>
          ))}
        </ul>
      )}

      {pendingCapture && (
        <p role="status" className="mt-2 rounded-lg bg-warning-subtle px-3 py-2 text-xs font-medium text-warning-ink">
          {t("Capture draft {pe} created — awaiting Finance submission", { pe: pendingCapture })}
        </p>
      )}

      {d.blocks_checkout && (
        <p className="mt-2 rounded-lg bg-warning-subtle px-3 py-2 text-xs font-medium text-warning-ink dark:bg-amber-50 dark:text-amber-800">
          {d.status === "Authorized"
            ? t("Before check-out: capture any damage from the card hold or release the hold.")
            : t("Before check-out: withhold anything for damage, then refund the rest.")}
        </p>
      )}

      {!mode && (
        <div className="mt-3 flex flex-wrap gap-2">
          {d.can_authorize && allowed("authorize") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("authorize")}>
              {t("Authorize hold")}
            </Button>
          )}
          {d.can_increment && allowed("increment") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("increment")}>
              {t("Increment hold")}
            </Button>
          )}
          {d.can_capture && !pendingCapture && allowed("capture") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("capture")}>
              {t("Capture for damage")}
            </Button>
          )}
          {d.can_release && allowed("release") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("release")}>
              {t("Release hold")}
            </Button>
          )}
          {d.can_collect && allowed("collect") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("collect")}>
              {d.held > 0 ? t("Collect more") : t("Collect deposit")}
            </Button>
          )}
          {d.can_withhold && allowed("withhold") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("withhold")}>
              {t("Withhold for damage")}
            </Button>
          )}
          {d.can_refund && allowed("refund") && (
            <Button variant="outline" className="min-h-11" onClick={() => open("refund")}>
              {t("Refund deposit")}
            </Button>
          )}
          {d.can_waive && allowed("waive") && (
            <Button variant="ghost" className="min-h-11" onClick={() => open("waive")}>
              {t("Waive")}
            </Button>
          )}
        </div>
      )}

      {mode && (
        <div className="mt-3 space-y-3 rounded-lg bg-canvas p-3">
          {mode !== "waive" && mode !== "release" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-secondary-ink">
                {mode === "withhold" || mode === "refund"
                  ? t("Amount (held {amt})", { amt: fmt(d.held) })
                  : mode === "capture"
                    ? t("Amount (authorized {amt})", { amt: fmt(maxCapture) })
                    : mode === "increment"
                      ? t("Additional amount")
                      : t("Amount")}
              </span>
              <input
                type="number"
                name="preauth_amount"
                min={0}
                step={step}
                inputMode={digits === 0 ? "numeric" : "decimal"}
                className={inputCls}
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            </label>
          )}

          {(mode === "authorize" || mode === "increment") && (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label className="block">
                <span className="mb-1 block text-xs font-medium text-secondary-ink">
                  {mode === "authorize"
                    ? t("Gateway authorization ref")
                    : t("Gateway reference (optional)")}
                </span>
                <input
                  name="gateway_auth_ref"
                  autoComplete="off"
                  className={inputCls}
                  value={gatewayRef}
                  placeholder={t("e.g. AUTH-98412")}
                  onChange={(e) => setGatewayRef(e.target.value)}
                />
              </label>

              <label className="block">
                <span className="mb-1 block text-xs font-medium text-secondary-ink">
                  {mode === "authorize"
                    ? t("Hold expires on")
                    : t("New expiry (optional)")}
                </span>
                <input
                  type="datetime-local"
                  name="expires_on"
                  className={inputCls}
                  value={expiresOn}
                  onChange={(e) => setExpiresOn(e.target.value)}
                />
              </label>

              {mode === "authorize" && (
                <>
                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-secondary-ink">
                      {t("Card brand")}
                    </span>
                    <input
                      name="card_brand"
                      autoComplete="off"
                      maxLength={32}
                      className={inputCls}
                      value={cardBrand}
                      placeholder={t("e.g. Visa, Mastercard")}
                      onChange={(e) => setCardBrand(e.target.value)}
                    />
                  </label>

                  <label className="block">
                    <span className="mb-1 block text-xs font-medium text-secondary-ink">
                      {t("Card last 4 digits")}
                    </span>
                    <input
                      name="card_last4"
                      autoComplete="off"
                      inputMode="numeric"
                      maxLength={4}
                      pattern="[0-9]{4}"
                      className={inputCls}
                      value={cardLast4}
                      placeholder="4242"
                      onChange={(e) => setCardLast4(e.target.value)}
                    />
                  </label>
                </>
              )}
            </div>
          )}

          {(mode === "collect" || mode === "refund") && (
            <div role="radiogroup" aria-label={t("Payment mode")} className="flex flex-wrap gap-2">
              {loc.payment_modes.map((m) => (
                <button
                  key={m}
                  type="button"
                  role="radio"
                  aria-checked={payMode === m}
                  onClick={() => setPayMode(m)}
                  className={
                    "min-h-11 rounded-lg border px-3 py-1.5 text-sm font-medium " +
                    (payMode === m
                      ? "border-accent-control bg-accent-subtle text-accent-ink"
                      : "border-line bg-surface text-secondary-ink")
                  }
                >
                  {t(m)}
                </button>
              ))}
            </div>
          )}

          {mode !== "authorize" && mode !== "increment" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-secondary-ink">
                {mode === "collect"
                  ? t("Card slip / transfer ref (optional)")
                  : mode === "refund" || mode === "release"
                    ? t("Note (optional)")
                    : t("Reason (required)")}
              </span>
              <input
                name="deposit_text"
                className={inputCls}
                value={text}
                placeholder={
                  mode === "withhold" || mode === "capture"
                    ? t("e.g. Broken glass table, photos on file")
                    : undefined
                }
                onChange={(e) => setText(e.target.value)}
              />
            </label>
          )}

          {mode === "capture" && (
            <label className="block">
              <span className="mb-1 block text-xs font-medium text-secondary-ink">
                {t("Evidence reference (optional)")}
              </span>
              <input
                name="deposit_evidence"
                className={inputCls}
                value={evidence}
                placeholder={t("Photo or incident report link")}
                onChange={(e) => setEvidence(e.target.value)}
              />
            </label>
          )}

          {pciBlocked && (
            <p
              role="alert"
              className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-xs font-medium text-danger-ink"
            >
              {t("Enter only the last 4 digits of the card. Never enter a full card number or security code.")}
            </p>
          )}

          {mode === "authorize" && !pciBlocked && (
            <p className="text-xs text-muted-ink">
              {t("Reserves a hold on the card without posting to the general ledger until captured.")}
            </p>
          )}

          {mode === "capture" && (
            <p className="text-xs text-muted-ink">
              {t("Posts a damage charge to the folio and captures the card hold into a posted receipt.")}
            </p>
          )}

          {mode === "release" && (
            <p className="text-xs text-muted-ink">
              {t("Releases the uncaptured card hold with zero accounting entry posted.")}
            </p>
          )}

          {mode === "withhold" && (
            <p className="text-xs text-muted-ink">
              {t("Posts a damage charge to the folio; the held deposit pays it.")}
            </p>
          )}

          {mode === "refund" && d.folio_credit < d.held && (
            <p className="text-xs text-warning-ink">
              {t("The folio only has {amt} in credit - settle the bill first, or let the deposit cover it.", {
                amt: fmt(d.folio_credit),
              })}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button className="min-h-11" disabled={busy || !ready} onClick={submit}>
              {busy ? t("Saving…") : label[mode]}
            </Button>
            <Button className="min-h-11" variant="ghost" onClick={() => setMode(null)}>
              {t("Cancel")}
            </Button>
          </div>
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
