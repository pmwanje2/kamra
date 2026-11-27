// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import { DirectBookingCheckout } from "../src/components/DirectBookingCart"
import ar from "../src/i18n/locales/ar.json"
import es from "../src/i18n/locales/es.json"
import fr from "../src/i18n/locales/fr.json"

const rpc = vi.hoisted(() => vi.fn())
vi.mock("../src/lib/api", async (importOriginal) => ({ ...await importOriginal<typeof import("../src/lib/api")>(), callGuest: rpc }))

it("keeps the same checkout key after a dropped response", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const quote = { currency: "USD", currency_decimals: 2, amount_after_tax: 100, tax: 0, advance_due: 0 }
  rpc.mockImplementation((method: string) => method.endsWith(".cart_quote")
    ? Promise.resolve(quote) : Promise.reject(new Error("Network response lost")))
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  const input = { room_type: "STD", room_name: "Standard", check_in_date: "2026-12-01", check_out_date: "2026-12-03", adults: 1, children: 0, upsells: [] }
  try {
    await act(async () => root.render(<MemoryRouter>
      <DirectBookingCheckout property="P7" lines={[input]} upsells={[]} privacy={null}
        onChange={vi.fn()} onClose={vi.fn()} onBooked={vi.fn()} />
    </MemoryRouter>))
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 300)) })
    const form = host.querySelector("form")!
    form.querySelector<HTMLInputElement>('[name="guest_name"]')!.value = "Guest"
    form.querySelector<HTMLInputElement>('[name="phone"]')!.value = "+256700000919"
    for (let n = 0; n < 2; n++) {
      await act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })) })
    }
    const calls = rpc.mock.calls.filter(([method]) => method.endsWith(".create_multi_room_booking"))
    expect(calls).toHaveLength(2)
    expect(calls[0][1].idempotency_key).toEqual(expect.any(String))
    expect(calls[1][1].idempotency_key).toBe(calls[0][1].idempotency_key)
  } finally {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
    rpc.mockReset()
  }
})

it("ships all original P7 strings in Spanish, French, and Arabic", () => {
  const keys = [
    "A selected add-on is no longer available.",
    "A selected meal plan is no longer available.",
    "A selected room type is no longer available.",
    "Add to booking cart",
    "Billing address (optional)",
    "Booking add-ons",
    "Booking cart",
    "Booking cart ({n})",
    "Check-in details were already verified by the desk.",
    "Checking prices and availability…",
    "Choose a future stay of one to 30 nights.",
    "Choose between one and eight rooms.",
    "Direct booking price basis",
    "Direct booking transaction code",
    "Enter a valid arrival time.",
    "Enter an age for each child.",
    "Enter valid stay dates.",
    "Invalid booking add-ons.",
    "Invalid number of guests or pets.",
    "Invalid occupant details.",
    "Invalid room cart.",
    "One or more rooms in your cart are no longer available for the selected dates.",
    "Online booking is unavailable for this property.",
    "Per guest",
    "Please accept the registration declaration.",
    "Please fill in the required details.",
    "Select a room that fits all guests.",
    "Show online pre-check-in on Desk Arrivals",
    "Some booking details are invalid.",
    "Taxes included",
    "The ID photo is invalid.",
    "The signature is invalid.",
    "These dates need a stay of at least {0} nights.",
    "This check-in link is not valid anymore.",
    "This property does not allow pets.",
    "Your booking cart is empty."
  ]
  expect(keys).toHaveLength(36)
  for (const locale of [es, fr, ar]) {
    const dictionary: Record<string, string> = locale
    expect(keys.filter((key) => !dictionary[key]?.trim() || dictionary[key] === key)).toEqual([])
  }
})
