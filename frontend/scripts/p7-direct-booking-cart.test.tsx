// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { afterEach, expect, it, vi } from "vitest"
import { DirectBookingCart } from "../src/components/DirectBookingCart"

afterEach(() => vi.unstubAllGlobals())

it("lists independent stays, sends add-on selections, and formats the authoritative quote", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  const onUpsells = vi.fn()
  const lines = [
    { room_type: "STD", room_name: "Standard", check_in_date: "2026-12-01", check_out_date: "2026-12-03", adults: 1, children: 0, upsells: [] },
    { room_type: "FAM", room_name: "Family", check_in_date: "2026-12-02", check_out_date: "2026-12-05", adults: 2, children: 1, upsells: [] },
  ]
  try {
    await act(async () => root.render(
      <DirectBookingCart lines={lines} upsells={[{ name: "TRANSFER", label: "Airport transfer", basis: "Per stay" }]}
        quote={{ currency: "KWD", currency_decimals: 3, amount_after_tax: 40.125, tax: 2.125, advance_due: 0 }}
        onRemove={vi.fn()} onUpsells={onUpsells} />,
    ))
    expect(host.textContent).toContain("Standard")
    expect(host.textContent).toContain("Family")
    expect(host.textContent).toContain("2026-12-05")
    expect(host.textContent).toContain("40.125")
    const checkboxes = host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')
    expect(checkboxes).toHaveLength(2)
    await act(async () => checkboxes[1].click())
    expect(onUpsells).toHaveBeenCalledWith(1, ["TRANSFER"])
  } finally {
    await act(async () => root.unmount())
    host.remove()
  }
})

