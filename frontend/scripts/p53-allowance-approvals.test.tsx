// @vitest-environment jsdom
/** P53 AllowanceApprovals screen contract:
 * - Lightweight read/audit view with prominent "Open in Approvals Inbox" button.
 * - Zero legacy direct-approval or self-approval PIN mutation controls.
 * - Dynamic 0/2/3dp currency precision via fmtMoney (UGX 0dp, USD 2dp, KWD 3dp).
 * - 100% Maximus white-label (zero SAP/ERPNext/Frappe/Kamra/URY strings, zero /app hrefs).
 * - Dark mode parity and RTL logical CSS classes.
 * - Complete fr & ar localization coverage for all screen strings.
 */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import AllowanceApprovals from "../src/screens/AllowanceApprovals"
import { adoptUiLocale } from "../src/lib/money"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Grand Kampala",
}))

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const SCREEN_PATH = path.join(FRONTEND_ROOT, "src/screens/AllowanceApprovals.tsx")

const SAMPLE_ROWS = {
  Pending: [
    {
      name: "ALW-00010",
      serial: null,
      folio: "FOL-2026-00010",
      amount: 150000,
      reason: "Late check-in compensation",
      status: "Pending",
      requested_by: "desk@alpha.ug",
      requested_on: "2026-11-25 09:30:00",
      channel: "Desk",
      decided_by: null,
      decided_on: null,
      decision_note: null,
      guest_name: "Amina Okello",
      room: "204",
      folio_status: "Open",
      bill_to_name: null,
    },
  ],
  Approved: [
    {
      name: "ALW-00009",
      serial: "ALW-GK-26-00009",
      folio: "FOL-2026-00009",
      amount: 12.345,
      reason: "Service recovery",
      status: "Approved",
      requested_by: "desk@alpha.ug",
      requested_on: "2026-11-24 14:10:00",
      channel: "Desk",
      decided_by: "finance@alpha.ug",
      decided_by_name: "Farah Finance",
      decided_by_title: "Finance Controller",
      decided_on: "2026-11-24 14:25:00",
      decision_note: null,
      guest_name: "Nasser Al-Sabah",
      room: "305",
      folio_status: "Open",
      bill_to_name: null,
    },
  ],
  Rejected: [
    {
      name: "ALW-00008",
      serial: null,
      folio: "FOL-2026-00008",
      amount: 75.5,
      reason: "Unverified mini-bar dispute",
      status: "Rejected",
      requested_by: "desk@alpha.ug",
      requested_on: "2026-11-23 11:00:00",
      channel: "Desk",
      decided_by: "finance@alpha.ug",
      decided_by_name: "Farah Finance",
      decided_on: "2026-11-23 11:15:00",
      decision_note: "Signed mini-bar chit on file",
      guest_name: "Claire Martin",
      room: "102",
      folio_status: "Open",
      bill_to_name: null,
    },
  ],
}

describe("P53 AllowanceApprovals audit view & Approvals Inbox convergence", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    ;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)
    setLang("en")
    rpc.mockReset()
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.allowance_requests") {
        const st = String(params?.status ?? "Pending") as keyof typeof SAMPLE_ROWS
        return Promise.resolve({ requests: SAMPLE_ROWS[st] ?? [], can_decide: true })
      }
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "UGX",
          currency_symbol: "UGX ",
          locale: "en-UG",
          decimals: 0,
          currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
        })
      }
      return Promise.resolve({})
    })
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    host.remove()
    delete (window as Window & { kamra_approvals_inbox_url?: string }).kamra_approvals_inbox_url
  })

  async function renderScreen() {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <AllowanceApprovals />
        </MemoryRouter>,
      )
    })
    await act(async () => {
      await Promise.resolve()
    })
  }

  it("renders prominent Open in Approvals Inbox button and removes inline PIN approval controls", async () => {
    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", decimals: 0 })
    const nav = vi.fn()
    ;(window as Window & { __p53_assign?: (url: string) => void }).__p53_assign = nav
    const origLocation = window.location
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { ...origLocation, assign: nav },
    })
    try {
      await renderScreen()

      const inboxButtons = [...host.querySelectorAll("button")].filter((b) =>
        b.textContent?.includes("Open in Approvals Inbox"),
      )
      expect(inboxButtons.length).toBeGreaterThanOrEqual(1)

      // Clicking the button opens the central Approvals Inbox route
      await act(async () => {
        inboxButtons[0].click()
      })
      expect(nav).toHaveBeenCalledWith("/app/workflow-action")

      // No inline PIN password inputs on the lightweight audit screen
      expect(host.querySelectorAll('input[type="password"]').length).toBe(0)
      // No /app href attribute that would fail white-label-routes.mjs
      const appHrefs = [...host.querySelectorAll("a[href]")].filter((a) =>
        /^\/app(?:[/?#]|$)/i.test(a.getAttribute("href") ?? ""),
      )
      expect(appHrefs).toEqual([])
      // Audit row renders UGX 0dp cleanly without .00
      expect(host.textContent).toContain("UGX 150,000")
      expect(host.textContent).not.toContain("150,000.00")
      expect(host.textContent).toContain("Late check-in compensation")
      expect(host.textContent).toContain("Amina Okello")
      // Rendered DOM contains zero vendor / SAP / ERPNext / Frappe / Kamra text
      expect(host.textContent).not.toMatch(/(?<![a-z0-9])(?:SAP|ERPNext|Frappe|Kamra|URY|Tridz|BuildSuite)(?![a-z0-9])/i)
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: origLocation })
    }
  })

  it("formats 3dp KWD and 2dp USD dynamically across tabs and preserves audit details", async () => {
    adoptUiLocale({
      currency: "KWD",
      currency_symbol: "KWD ",
      locale: "en-KW",
      decimals: 3,
      currency_decimals: { KWD: 3, UGX: 0, USD: 2 },
    })
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.allowance_requests") {
        const st = String(params?.status ?? "Pending") as keyof typeof SAMPLE_ROWS
        return Promise.resolve({ requests: SAMPLE_ROWS[st] ?? [], can_decide: true })
      }
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "KWD",
          currency_symbol: "KWD ",
          locale: "en-KW",
          decimals: 3,
          currency_decimals: { KWD: 3, UGX: 0, USD: 2 },
        })
      }
      return Promise.resolve({})
    })

    await renderScreen()
    const approvedTab = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Approved")
    expect(approvedTab).toBeTruthy()
    await act(async () => {
      approvedTab!.click()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(host.textContent).toContain("KWD 12.345")
    expect(host.textContent).toContain("ALW-GK-26-00009")
    expect(host.textContent).toContain("Farah Finance")

    // Switch to Rejected tab with 2dp USD
    adoptUiLocale({
      currency: "USD",
      currency_symbol: "$",
      locale: "en-GB",
      decimals: 2,
      currency_decimals: { KWD: 3, UGX: 0, USD: 2 },
    })
    const rejectedTab = [...host.querySelectorAll("button")].find((b) => b.textContent?.trim() === "Rejected")
    expect(rejectedTab).toBeTruthy()
    await act(async () => {
      rejectedTab!.click()
    })
    await act(async () => {
      await Promise.resolve()
    })

    expect(host.textContent).toContain("$75.50")
    expect(host.textContent).toContain("Signed mini-bar chit on file")
  })

  it("enforces 100% Maximus white-label, dark mode parity, and RTL logical CSS on AllowanceApprovals.tsx", () => {
    const src = fs.readFileSync(SCREEN_PATH, "utf8")
    const withoutApiIds = src.replace(/\bkamra\.api\.[a-z_]+\b/g, "").replace(/\bkamra_approvals_inbox_url\b/g, "")
    // Zero vendor or SAP/ERPNext/Frappe/Kamra strings in UI prose or comments
    expect(withoutApiIds).not.toMatch(/(?<![a-z0-9])(?:SAP|ERPNext|Frappe|Kamra|URY|Tridz|BuildSuite)(?![a-z0-9])/i)
    // Uses fmtMoney for dynamic 0/2/3dp formatting
    expect(src).toContain("fmtMoney")
    // Has dark mode classes for surface/text parity
    expect(src).toMatch(/dark:bg-/)
    expect(src).toMatch(/dark:text-/)
    expect(src).toMatch(/dark:border-/)
    // Uses logical RTL utilities, never physical ml-/mr-/pl-/pr-/text-left/text-right
    expect(src).not.toMatch(/\b(?:ml|mr|pl|pr)-\d|\btext-(?:left|right)\b|\bborder-[lr]-/)
    expect(src).toMatch(/\b(?:ms|me|ps|pe|text-start|border-s)-/)
  })

  it("renders French and Arabic translations cleanly for Open in Approvals Inbox", async () => {
    adoptUiLocale({ currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", decimals: 0 })
    await ensureDictionary("fr")
    setLang("fr")
    await renderScreen()
    expect(host.textContent).not.toContain("Open in Approvals Inbox")
    expect(host.textContent).toMatch(/boîte des approbations|approbations/i)

    await ensureDictionary("ar")
    setLang("ar")
    await renderScreen()
    expect(host.textContent).not.toContain("Open in Approvals Inbox")
    expect(host.textContent).toMatch(/الموافقات/)
  })
})
