// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import { StayBilling } from "../src/components/RoomStayPanels"
import { call } from "../src/lib/api"
import type { RoomDetail } from "../src/lib/rooms"

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: vi.fn(),
}))

it.each([
  ["UGX", 0, 75], ["USD", 2, 74.88], ["KWD", 3, 74.877],
] as const)("shows collectible amounts and the credit status on %s stay windows", async (currency, decimals, net) => {
  const base = { status: "Open", balance: 100, grand_total: 100, payments_total: 0 }
  vi.mocked(call).mockImplementation(async (method) => {
    if (method === "kamra.api.property_locale") return { currency, decimals, currency_symbol: currency + " " }
    if (method === "kamra.api.reservation_folios") return [
      { ...base, name: "GUEST", folio_type: "Guest", net_collectible_balance: net, credit_status: "Partially credited" },
      { ...base, name: "GROUP", folio_type: "Group", net_collectible_balance: 0, credit_status: "Credited" },
    ]
    return null
  })
  const host = document.createElement("div")
  const root = createRoot(host)
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  try {
    await act(async () => root.render(<MemoryRouter>
      <StayBilling d={{ stay: { name: "STAY" } } as RoomDetail} onChanged={() => {}} />
    </MemoryRouter>))
    const rows = host.querySelectorAll("li")
    expect(rows[0].textContent).toContain(currency + " " + net.toFixed(decimals))
    expect(rows[0].textContent).toContain("Partially credited")
    expect(rows[1].textContent).toContain(currency + " " + (0).toFixed(decimals))
    expect(rows[1].textContent).toContain("Credited")
    expect(host.querySelector<HTMLInputElement>('input[inputmode="decimal"][value="' + net + '"]')).toBeTruthy()
  } finally {
    await act(async () => root.unmount())
    vi.unstubAllGlobals()
  }
})
