// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async original => ({
  ...await original<typeof import("../src/lib/api")>(), call: rpc, getCurrentProperty: () => "Hotel",
}))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ roles: ["Finance"] }) }))
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks() })

async function openBill(currency: string, balance: number, status = "Checked Out", kind = "Guest") {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const folio = { name: "R11", property: "Hotel", status: "Open", folio_type: kind, balance: 0,
    window_no: 1, charges: [], payments: [], charges_total: 99, grand_total: 99, payments_total: 99, tax_total: 0 }
  const invoice = { folio, settlement_balance: balance, net_collectible_balance: Math.max(0, balance),
    property: { name: "Hotel", currency }, guest: { name: "Guest" }, stay: { reservation: "S1", status, nights: 1 },
    lines: [], summary_by_head: [], tax_summary: [], gst_summary: [] }
  rpc.mockImplementation(async (method: string) => {
    if (method.endsWith("folio_invoice")) return invoice
    if (method.endsWith("generate_proforma")) return { proforma: "PF-11", invoice: { ...invoice,
      projected_lines: [{ posting_date: "2031-01-02", charge_type: "Room", description: "Projected suite night", total: 20 }],
      estimate: { posted_total: 99, projected_total: 20, grand_total: 119, balance: 75 },
    } }
    if (method.endsWith("linked_records")) return { folios: [], reservations: [] }
    if (method.endsWith("folio_windows_overview")) return { windows: [folio] }
    return []
  })
  const host = document.createElement("div")
  const root = createRoot(host)
  await act(async () => root.render(<MemoryRouter initialEntries={["/billing/R11"]}><Routes>
    <Route path="/billing/:name" element={<FolioView />} />
  </Routes></MemoryRouter>))
  const button = (label: string) => [...host.querySelectorAll("button")].find(b => b.textContent === label)!
  return { host, button, close: () => act(async () => root.unmount()) }
}

it.each(["UGX", "USD", "KWD"])("prints the returned %s estimate and restores the live folio", async currency => {
  const view = await openBill(currency, 55)
  let printed = ""
  vi.spyOn(window, "print").mockImplementation(() => {
    printed = view.host.textContent ?? ""
    window.dispatchEvent(new Event("afterprint"))
  })
  try {
    await act(async () => view.button("Proforma").click())
    expect(printed).toContain("Projected suite night")
    expect(printed).toContain("119")
    expect(printed).toContain("75")
    expect(view.host.textContent).not.toContain("Projected suite night")
  } finally { await view.close() }
})

it.each(["UGX", "USD", "KWD"])("restricts final closure on %s settled and in-house bills", async currency => {
  for (const [balance, status, kind, disabled] of [
    [30, "Checked Out", "Guest", true], [25, "Checked Out", "Guest", true], [-15, "Checked Out", "Guest", true],
    [0, "Checked In", "Guest", true], [0, "Checked In", "Company", false], [0, "Checked Out", "Guest", false],
  ] as const) {
    const view = await openBill(currency, balance, status, kind)
    try {
      const close = view.button("Close & generate invoice")
      expect.soft(close.disabled).toBe(disabled)
      if (disabled) expect.soft(close.title || close.getAttribute("aria-describedby")).toBeTruthy()
    } finally { await view.close() }
  }
})

