import { useEffect, useRef, useState } from "react"
import { Link } from "react-router-dom"
import { callGuest } from "../lib/api"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"
import { Sheet } from "./ui/sheet"

export interface CartLine {
  room_type: string
  room_name: string
  check_in_date: string
  check_out_date: string
  adults: number
  children: number
  meal_plan?: string
  rate_plan?: string
  child_ages?: number[]
  pets?: number
  voucher_code?: string
  upsells: string[]
}

export interface CartUpsell {
  name: string
  label: string
  price?: number
  basis: "Per stay" | "Per night" | "Per guest"
}

interface CartQuote {
  currency: string
  currency_decimals: number
  amount_after_tax: number
  tax: number
  advance_due: number
  lines?: { amount_after_tax: number }[]
}

interface CartConfirmation extends CartQuote {
  confirmation: string
  reservations: { reservation: string; status: string; precheckin_token?: string }[]
}

interface CartProps {
  lines: CartLine[]
  upsells: CartUpsell[]
  quote: CartQuote | null
  ratePlans?: { name: string; label: string }[]
  mealPlans?: { name: string; label: string }[]
  onUpsells: (index: number, upsells: string[]) => void
  onRemove: (index: number) => void
  onPlan?: (index: number, kind: "rate_plan" | "meal_plan" | "voucher_code", value: string) => void
  disabled?: boolean
}

const field = "min-w-0 w-full rounded-lg border border-control bg-surface px-3 py-2 text-base text-ink focus-visible:outline-2 focus-visible:outline-focus"
const card = "rounded-xl border border-line bg-canvas p-4 text-ink"

function useCartMoney(quote: CartQuote | null) {
  const { lang } = useT()
  const format = quote && new Intl.NumberFormat(lang, {
    style: "currency",
    currency: quote.currency,
    minimumFractionDigits: quote.currency_decimals,
    maximumFractionDigits: quote.currency_decimals,
  })
  return (amount: number) => format ? format.format(amount) : ""
}

export function DirectBookingCart({
  lines, upsells, quote, ratePlans = [], mealPlans = [], onUpsells, onRemove, onPlan, disabled,
}: CartProps) {
  const { t } = useT()
  const money = useCartMoney(quote)
  const basisLabels = { "Per stay": t("Per stay"), "Per night": t("Per night"), "Per guest": t("Per guest") }
  return (
    <div className="space-y-4">
      {lines.map((line, index) => (
        <fieldset key={index} disabled={disabled} className={card}>
          <legend className="px-1 text-sm font-semibold">{t("Room {n}", { n: index + 1 })}</legend>
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0">
              <h3 className="break-words font-semibold">{line.room_name}</h3>
              <p className="text-sm text-secondary-ink">
                <bdi>{line.check_in_date}</bdi> {t("to")} <bdi>{line.check_out_date}</bdi>
              </p>
              <p className="text-sm text-secondary-ink">{t("Adults")} {line.adults} · {t("Children")} {line.children}</p>
            </div>
            <div className="flex items-center gap-2">
              {quote?.lines?.[index] && <bdi className="font-semibold">{money(quote.lines[index].amount_after_tax)}</bdi>}
              <Button type="button" variant="ghost" onClick={() => onRemove(index)}>{t("Remove")}</Button>
            </div>
          </div>
          {onPlan && (
            <div className="mt-3 grid min-w-0 gap-3 sm:grid-cols-2">
              {ratePlans.length > 0 && (
                <label className="min-w-0 text-sm">
                  {t("Rate plan")}
                  <select className={field} value={line.rate_plan || ""} onChange={(e) => onPlan(index, "rate_plan", e.target.value)}>
                    <option value="">{t("Standard rate")}</option>
                    {ratePlans.map((plan) => <option key={plan.name} value={plan.name}>{plan.label}</option>)}
                  </select>
                </label>
              )}
              {mealPlans.length > 0 && (
                <label className="min-w-0 text-sm">
                  {t("Meal plan")}
                  <select className={field} value={line.meal_plan || ""} onChange={(e) => onPlan(index, "meal_plan", e.target.value)}>
                    <option value="">{t("Room only")}</option>
                    {mealPlans.map((plan) => <option key={plan.name} value={plan.name}>{plan.label}</option>)}
                  </select>
                </label>
              )}
            </div>
          )}
          {line.voucher_code && onPlan && (
            <div className="mt-3 flex flex-wrap items-center gap-2 text-sm">
              <span>{t("Promo code")} <bdi>{line.voucher_code}</bdi></span>
              <Button type="button" variant="ghost" onClick={() => onPlan(index, "voucher_code", "")}>
                {t("Remove")}
              </Button>
            </div>
          )}
          {upsells.length > 0 && (
            <div className="mt-3 border-t border-line pt-3">
              <h4 className="mb-2 text-sm font-semibold">{t("Booking add-ons")}</h4>
              <div className="grid gap-2 sm:grid-cols-2">
                {upsells.map((item) => (
                  <label key={item.name} className="flex min-w-0 items-start gap-2 rounded-lg border border-line bg-surface px-3 py-2 text-sm">
                    <input type="checkbox" className="mt-1 shrink-0 accent-primary-button" checked={line.upsells.includes(item.name)}
                      onChange={(e) => onUpsells(index, e.target.checked
                        ? [...line.upsells, item.name] : line.upsells.filter((id) => id !== item.name))} />
                    <span className="break-words">{item.label} <span className="block text-secondary-ink">{quote && item.price !== undefined ? `${money(item.price)} · ` : ""}{basisLabels[item.basis]}</span></span>
                  </label>
                ))}
              </div>
            </div>
          )}
        </fieldset>
      ))}
      {quote && (
        <dl className={card} aria-live="polite">
          <div className="flex justify-between gap-4 text-sm"><dt>{t("Taxes included")}</dt><dd><bdi>{money(quote.tax)}</bdi></dd></div>
          <div className="mt-2 flex justify-between gap-4 font-semibold"><dt>{t("Total")}</dt><dd><bdi>{money(quote.amount_after_tax)}</bdi></dd></div>
          {quote.advance_due > 0 &&
            <div className="mt-2 flex justify-between gap-4 text-sm"><dt>{t("Due now")}</dt><dd><bdi>{money(quote.advance_due)}</bdi></dd></div>}
        </dl>
      )}
    </div>
  )
}

function checkoutGuest(form?: HTMLFormElement) {
  const data = form ? new FormData(form) : undefined
  return {
    guest_name: String(data?.get("guest_name") ?? ""),
    phone: String(data?.get("phone") ?? ""),
    email: String(data?.get("email") ?? ""),
    special_requests: String(data?.get("special_requests") ?? ""),
    marketing_consent: data?.get("marketing_consent") === "on" ? 1 : 0,
  }
}

export type CheckoutAttempt = {
  cart: string
  guest: ReturnType<typeof checkoutGuest>
  payload: string
  key: string
}

export function DirectBookingCheckout({
  property, lines, upsells, ratePlans, mealPlans, onChange, onClose, onBooked, privacy, checkoutAttempt,
}: {
  checkoutAttempt?: React.RefObject<CheckoutAttempt | null>
  property: string
  lines: CartLine[]
  upsells: CartUpsell[]
  ratePlans?: CartProps["ratePlans"]
  mealPlans?: CartProps["mealPlans"]
  onChange: (lines: CartLine[]) => void
  onClose: () => void
  onBooked: () => void
  privacy: React.ReactNode
}) {
  const { t } = useT()
  const [pricing, setPricing] = useState<{ lines: CartLine[]; quote?: CartQuote; error?: string }>()
  const [done, setDone] = useState<CartConfirmation>()
  const [busy, setBusy] = useState(false)
  const [submitError, setSubmitError] = useState("")
  const localAttempt = useRef<CheckoutAttempt | null>(null)
  const checkout = checkoutAttempt ?? localAttempt
  const cart = JSON.stringify({ property, lines })
  const [guest, setGuest] = useState(() => checkout.current?.cart === cart ? checkout.current.guest : checkoutGuest())
  const canReplay = checkout.current?.payload === JSON.stringify({ property, lines, ...guest })
  const quote = pricing?.lines === lines ? pricing.quote ?? null : null
  const money = useCartMoney(done ?? quote)

  useEffect(() => {
    if (lines.length === 0 || done) return
    let active = true
    const timer = window.setTimeout(() => {
      callGuest<CartQuote>("kamra.public_api.cart_quote", { property, lines })
        .then((quote) => { if (active) setPricing({ lines, quote }) })
        .catch((error) => { if (active) setPricing({ lines, error: serverError(error) }) })
    }, 250)
    return () => { active = false; window.clearTimeout(timer) }
  }, [property, lines, done])

  function change(next: CartLine[]) {
    setSubmitError("")
    checkout.current = null
    onChange(next)
  }

  function close() {
    if (!busy) onClose()
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (busy) return
    const guest = checkoutGuest(event.currentTarget)
    const payload = { property, lines, ...guest }
    const identity = JSON.stringify(payload)
    if (checkout.current?.payload !== identity) {
      if (!quote) return
      const key = Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, "0")).join("")
      checkout.current = { cart, guest, payload: identity, key }
    }
    setSubmitError("")
    setGuest(guest)
    setBusy(true)
    try {
      const booked = await callGuest<CartConfirmation>("kamra.public_api.create_multi_room_booking", {
        ...payload, idempotency_key: checkout.current.key,
      })
      checkout.current = null
      setDone(booked)
      onBooked()
    } catch (error) {
      setSubmitError(serverError(error))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Sheet wide title={done ? t("Booking number") : t("Booking cart")} onClose={close}>
      {done ? (
        <div className="space-y-4">
          <p className="text-2xl font-semibold"><bdi>{done.confirmation}</bdi></p>
          <p>{t("Total")} <bdi>{money(done.amount_after_tax)}</bdi></p>
          {done.advance_due > 0 && <p>{t("Due now")} <bdi>{money(done.advance_due)}</bdi></p>}
          <ul className="space-y-2">
            {done.reservations.map((room, index) => (
              <li key={room.reservation} className={card}>
                <div>{t("Room {n}", { n: index + 1 })} · <bdi>{room.reservation}</bdi> · {t(room.status)}</div>
                {room.precheckin_token &&
                  <Link reloadDocument className="mt-1 inline-block font-medium text-accent-ink underline"
                    to={`/checkin/${encodeURIComponent(room.precheckin_token)}`}>
                    {t("Pre-check-in")}
                  </Link>}
              </li>
            ))}
          </ul>
          {done.reservations.some((room) => room.status === "Pending Payment") &&
            <p className="text-sm text-secondary-ink">{t("Your rooms are held until the advance is received.")}</p>}
          {done.reservations.some((room) => room.status === "Requested") &&
            <p className="text-sm text-secondary-ink">{t("Your request goes to the property to confirm.")}</p>}
          <Button type="button" onClick={close}>{t("Done")}</Button>
        </div>
      ) : lines.length === 0 ? (
        <p>{t("Your booking cart is empty.")}</p>
      ) : (
        <form className="space-y-5" onSubmit={submit} onChange={(event) => setGuest(checkoutGuest(event.currentTarget))}>
          <DirectBookingCart lines={lines} upsells={upsells} quote={quote} ratePlans={ratePlans} mealPlans={mealPlans}
            disabled={busy} onRemove={(index) => change(lines.filter((_, i) => i !== index))}
            onUpsells={(index, upsells) => change(lines.map((line, i) => i === index ? { ...line, upsells } : line))}
            onPlan={(index, key, value) => change(lines.map((line, i) => i === index ? { ...line, [key]: value } : line))} />
          {pricing?.lines !== lines && <p role="status">{t("Checking prices and availability…")}</p>}
          {pricing?.lines === lines && pricing.error && <p role="alert" className="text-danger-text">{t(pricing.error)}</p>}
          <fieldset disabled={busy} className="grid min-w-0 gap-3 sm:grid-cols-2">
            <legend className="mb-2 font-semibold">{t("Your details")}</legend>
            <label>{t("Full name")}<input className={field} name="guest_name" defaultValue={guest.guest_name} required maxLength={140} autoComplete="name" /></label>
            <label>{t("Phone")}<input className={field} name="phone" defaultValue={guest.phone} type="tel" required maxLength={50} autoComplete="tel" /></label>
            <label>{t("Email (optional)")}<input className={field} name="email" defaultValue={guest.email} type="email" maxLength={140} autoComplete="email" /></label>
            <label>{t("Special requests")}<textarea className={field} name="special_requests" defaultValue={guest.special_requests} rows={2} maxLength={2000} /></label>
            <label className="flex items-start gap-2 text-sm sm:col-span-2">
              <input type="checkbox" name="marketing_consent" defaultChecked={Boolean(guest.marketing_consent)} className="mt-1 shrink-0 accent-primary-button" />
              {t("Send me offers on WhatsApp. Reply STOP at any time to opt out.")}
            </label>
          </fieldset>
          {privacy}
          {submitError && <p role="alert" className="text-danger-text">{t(submitError)}</p>}
          <Button type="submit" disabled={(!quote && !canReplay) || busy}>{busy ? t("Booking…") : t("Complete your booking")}</Button>
        </form>
      )}
    </Sheet>
  )
}
