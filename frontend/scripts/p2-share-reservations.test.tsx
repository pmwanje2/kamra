// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import ShareReservations, { type ShareSummary } from "../src/components/ShareReservations"
import { shareRoomBars } from "../src/lib/shareRoomBars"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(async (_method: string, _params: unknown) => ({})) }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
}))

it("renders desk actions, independent folios and a three-decimal custom rate", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  const summary: ShareSummary = {
    group_id: "G1", mode: "Custom", currency: "KWD", can_manage: true,
    members: [
      { reservation: "A", guest_name: "First Guest", folio: "FA", primary: true, custom_rate: 20.001,
        check_in: "2026-11-25", check_out: "2026-11-29" },
      { reservation: "B", guest_name: "Second Guest", folio: "FB", primary: false, custom_rate: 30.002,
        check_in: "2026-11-26", check_out: "2026-11-28" },
    ],
  }
  const reload = vi.fn()
  try {
    await act(async () => root.render(<MemoryRouter>
      <ShareReservations reservation="A" summary={summary} reload={reload} />
    </MemoryRouter>))
    const button = (label: string) => Array.from(host.querySelectorAll("button"))
      .find((item) => item.textContent === label)
    expect(button("Add Sharer")).toBeDefined()
    expect(button("Combine Existing Reservation")).toBeDefined()
    expect(button("Break Share")).toBeDefined()
    expect(host.querySelector('a[href="/billing/FB"]')?.textContent).toContain("Folio")
    expect(host.textContent).toContain("30.002")
    const mode = host.querySelector<HTMLSelectElement>('select[aria-label="Rate Split Mode"]')!
    expect(Array.from(mode.options, (option) => option.text)).toEqual([
      "Split Equal", "Primary Pays", "Full Rate Each", "Custom",
    ])
    await act(async () => {
      mode.value = "Primary Pays"
      mode.dispatchEvent(new Event("change", { bubbles: true }))
    })
    await act(async () => button("Save Rate Split")!.click())
    expect(rpc).toHaveBeenCalledWith("kamra.shares.update_share_rate_mode", {
      reservation: "A", mode: "Primary Pays",
    })
    expect(reload).toHaveBeenCalled()
  } finally {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  }
})

it("shows one bar per room segment and preserves the full union of sharer nights", () => {
  const bars = shareRoomBars([
    { name: "B", share_group_id: "G1", check_in_date: "2026-11-24", check_out_date: "2026-11-27" },
    { name: "A", share_group_id: "G1", is_share_primary: 1,
      check_in_date: "2026-11-25", check_out_date: "2026-11-28" },
    { name: "X", check_in_date: "2026-11-28", check_out_date: "2026-11-30" },
  ])
  expect(bars).toHaveLength(2)
  expect(bars[0]).toMatchObject({
    name: "A", check_in_date: "2026-11-24", check_out_date: "2026-11-28", share_count: 2,
  })
  expect(bars[1].name).toBe("X")
})
