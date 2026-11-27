// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import { DirectBookingCheckout } from "../src/components/DirectBookingCart"

const rpc = vi.hoisted(() => vi.fn())
vi.mock("../src/lib/api", async (load) => ({ ...await load<typeof import("../src/lib/api")>(), callGuest: rpc }))

it("requires another submit after showing the refreshed price and preserves guest details", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const original = { currency: "USD", currency_decimals: 2, amount_after_tax: 549.90, tax: 0, advance_due: 0 }
  const refreshed = { ...original, amount_after_tax: 1299.64 }
  const onBooked = vi.fn()
  const quote = vi.fn().mockResolvedValue(original)
  const checkout = vi.fn().mockRejectedValueOnce(new Error("Connection lost"))
    .mockResolvedValueOnce({ price_changed: true, quote: refreshed })
    .mockResolvedValueOnce({ ...refreshed, reservations: [], confirmation: "REVIEWED" })
  rpc.mockImplementation((method, args) => method.endsWith(".cart_quote") ? quote() : checkout(args))
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  const line = { room_type: "STD", room_name: "Standard", check_in_date: "2031-04-01",
    check_out_date: "2031-04-03", adults: 1, children: 0, upsells: [] }
  try {
    await act(async () => root.render(<MemoryRouter><DirectBookingCheckout property="P" lines={[line]}
      upsells={[]} onChange={vi.fn()} onClose={vi.fn()} onBooked={onBooked} privacy={null} /></MemoryRouter>))
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 300)) })
    const form = host.querySelector("form")!
    form.querySelector<HTMLInputElement>('[name="guest_name"]')!.value = "Guest"
    form.querySelector<HTMLInputElement>('[name="phone"]')!.value = "555"
    const submit = () => act(async () => { form.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })) })
    expect(host.textContent).toContain("549.90")
    // Verify the outbound terms first, including on a client that has not seen a price-change response.
    await submit()
    expect(checkout.mock.calls[0][0].expected_quote).toEqual(original)
    await submit()
    expect(onBooked).not.toHaveBeenCalled()
    expect(checkout).toHaveBeenCalledTimes(2)
    expect(host.querySelector('[role="alert"]')?.textContent).toMatch(/price/i)
    expect(host.textContent).toContain("1,299.64")
    expect(form.querySelector<HTMLInputElement>('[name="guest_name"]')!.value).toBe("Guest")
    await submit()
    expect(checkout.mock.calls[2][0].expected_quote).toEqual(refreshed)
    expect(onBooked).toHaveBeenCalledOnce()
    expect(host.textContent).toContain("REVIEWED")
  } finally {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
    rpc.mockReset()
  }
})
