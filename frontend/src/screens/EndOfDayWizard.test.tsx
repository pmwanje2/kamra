// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import type { EodState, Step } from "../lib/eod"

const step = (index: number, key: string, title: string, status: Step["status"], count: number): Step => ({
  index,
  key,
  title,
  checks: [],
  status,
  count,
  blocking: status === "blocked" ? [key] : [],
})

const STATE: EodState = {
  property: "Tank Hill",
  property_name: "Tank Hill",
  business_date: "2026-11-19",
  server_date: "2026-11-20",
  server_time: "2026-11-20 09:00:00",
  property_now: "2026-11-20 09:00:00",
  require_cashier_pin: 0,
  blind_count: 0,
  high_balance_threshold: 0,
  auto_audit_with_blockers: "Skip",
  reports: [],
  audit: "NA-0001",
  ran: false,
  status: "Draft",
  current_step: 1,
  acknowledgements: [],
  checks: [],
  steps: [
    step(1, "prepare", "Prepare End of Day", "pending", 0),
    step(4, "folios", "Open folios and high balances", "blocked", 7),
    step(6, "rates", "Rate check", "pending", 2),
    step(10, "run", "Post room and tax and roll the business date", "pending", 0),
  ],
  sessions: [],
}

vi.mock("../lib/eod", () => ({
  eodSession: vi.fn(async () => STATE),
  eodAcknowledge: vi.fn(),
  eodMarkNoShow: vi.fn(),
  eodProgress: vi.fn(),
  recoverStuckBusinessDate: vi.fn(),
  runNightAudit: vi.fn(),
  auditPack: vi.fn(),
  rebuildAuditPack: vi.fn(),
}))

const { default: EndOfDayWizard } = await import("./EndOfDayWizard")

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root

beforeEach(async () => {
  host = document.createElement("div")
  document.body.appendChild(host)
  root = createRoot(host)
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={["/end-of-day/Tank%20Hill"]}>
        <Routes>
          <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
        </Routes>
      </MemoryRouter>,
    )
  })
})

afterEach(() => {
  act(() => root.unmount())
  host.remove()
})

const stepButton = (title: string) => {
  const button = [...host.querySelectorAll("ol button")].find((b) => b.textContent?.includes(title))
  if (!button) throw new Error(`no step row for ${title}`)
  return button as HTMLButtonElement
}

describe("End of Day step list", () => {
  it("shows a blocked step's open count as a rose badge labelled 'N open'", () => {
    const badge = stepButton("Open folios and high balances").querySelector('[aria-label="7 open"]')
    expect(badge).not.toBeNull()
    expect(badge!.textContent).toBe("7")
    expect(badge!.className).toContain("bg-rose-50")
    expect(badge!.className).toContain("ring-inset")
  })

  it("shows a warning step's open count as an amber badge", () => {
    const badge = stepButton("Rate check").querySelector('[aria-label="2 open"]')
    expect(badge).not.toBeNull()
    expect(badge!.className).toContain("bg-amber-50")
  })

  it("shows no badge for a step with nothing open", () => {
    expect(stepButton("Prepare End of Day").querySelector('[aria-label$=" open"]')).toBeNull()
  })

  it("keeps the step number, title and count in separate elements", () => {
    const row = stepButton("Open folios and high balances")
    const number = row.querySelector('[data-step-number]')
    const title = row.querySelector('[data-step-title]')
    expect(number?.textContent).toBe("4")
    expect(title?.textContent).toBe("Open folios and high balances")
    // long titles wrap inside the 280px column instead of pushing the badge out
    expect(title!.className).toContain("min-w-0")
    expect(title!.className).toContain("break-words")
  })
})
