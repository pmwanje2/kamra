// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { TariffNight, TariffRoomTotal } from "../src/components/ContractTariff"

vi.hoisted(() => {
  // Node's own localStorage stub hides jsdom's; the money module reads it on import.
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
})

// Round 6 (P0-1, OPERA Rate Changes During Stay): a contract stay over a tariff renewal shows each
// night's own rate and adds its nights up, rather than multiplying the check-in night.
const nights = [
  { date: "2026-12-30", nightly: 100, agreed_rate: 365000, item_price: "IP-2026" },
  { date: "2026-12-31", nightly: 100, agreed_rate: 365000, item_price: "IP-2026" },
  { date: "2027-01-01", nightly: 200, agreed_rate: 730000, item_price: "IP-2027" },
]
const tariff = { currency: "USD", nightly: 100, property_currency: "UGX", fx_rate: 3650, fx_date: "2026-12-20" }

describe("Contract tariff nights", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  it("adds up each night's own tariff for the room subtotal", async () => {
    await act(async () => root.render(<TariffRoomTotal tariff={{ ...tariff, nights }} nights={3} agreedRate={365000}
      locale="en" />))
    expect(host.textContent).toBe("Room tariff subtotal: USD 400.00 · UGX 1,460,000")
  })

  it("keeps the one-rate subtotal for a contract without nights", async () => {
    await act(async () => root.render(<TariffRoomTotal tariff={tariff} nights={3} agreedRate={365000} locale="en" />))
    expect(host.textContent).toBe("Room tariff subtotal: USD 300.00 · UGX 1,095,000")
  })

  it("lists the nights whose tariff differs from the check-in night", async () => {
    await act(async () => root.render(<TariffNight tariff={{ ...tariff, nights }} agreed_rate={365000} />))
    expect(host.textContent).toContain("USD 100.00 / night · UGX 365,000 at 3,650 (Dec 20, 2026)")
    expect(host.textContent).toContain("Jan 1, 2027: USD 200.00 · UGX 730,000")
    expect(host.textContent).not.toContain("Dec 31, 2026:")
  })
})
