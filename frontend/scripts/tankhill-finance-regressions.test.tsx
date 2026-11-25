// @vitest-environment jsdom
import { readFileSync } from "node:fs"
import { act, type ReactNode } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"
import Billing from "../src/screens/Billing"
import ConfiguredScreen from "../src/screens/ConfiguredScreen"
import { ModulePage } from "../src/screens/settings/ModulePage"
import type { SettingsModule } from "../src/screens/settings/api"

vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ roles: ["Finance"] }) }))

const { rpc, list } = vi.hoisted(() => ({ rpc: vi.fn(), list: vi.fn() }))
vi.mock("../src/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/api")>(),
  call: rpc,
  getCurrentProperty: () => "Tankhill Inn By SEB",
}))
vi.mock("../src/lib/resource", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/resource")>(),
  listResource: list,
}))

async function render(node: ReactNode, check: (host: HTMLElement) => void | Promise<void>) {
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  try {
    await act(async () => root.render(<MemoryRouter>{node}</MemoryRouter>))
    await check(host)
  } finally {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  }
}

const finance: SettingsModule = {
  id: "finance", title: "Finance", description: "", group: "modules", toggle: false,
  enabled: true, editors: ["Finance"], can_edit: true, related: [],
  values: { allowance_approval: "", allowance_approval_above: 0, rate_override_limit_pct: 0 },
  sections: [{
    id: "cashier", title: "Cashier controls", description: "",
    settings: [
      { key: "allowance_approval", label: "Allowances need a manager's approval", default: "Off",
        type: "select", options: ["Off", "Hotel Admin", "Hotel Admin or Finance"], store: "kv", hint: "" },
      { key: "allowance_approval_above", label: "Only allowances above", default: 0,
        type: "number", store: "kv", hint: "0 means every allowance needs approval." },
    ],
  }],
}
const financePage = (module = finance) => <ModulePage module={module} sources={{ countries: [], languages: [], locales: [], timezones: [] }}
  onSaved={() => {}} onDirtyChange={() => {}} />

describe("hotel audit regressions", () => {
  it("shows the accounting invoice, and identifies credited folios", async () => {
    const row = { name: "FOLIO-1", sales_invoice: "ACC-SINV-2026-00009", invoice_number: "",
      status: "Closed", credit_status: "Credited", balance: 100, net_collectible_balance: 0 }
    list.mockResolvedValue([row])
    rpc.mockImplementation(async (method) => method.includes("billing_folios") ? [row] : [])
    await render(<Billing />, (host) => {
      expect(host.textContent).toContain("ACC-SINV-2026-00009")
      expect(host.textContent).toContain("Credited")
    })
  })

  it.each(["reservationsConfig", "roomBlocksConfig"] as const)("shows room numbers in %s", async (config) => {
    list.mockImplementation(async (doctype) => doctype === "Room"
      ? [{ name: "Tankhill Inn By SEB-111", room_number: "111" }]
      : [{ name: "Desk record", room: "Tankhill Inn By SEB-111" }])
    rpc.mockResolvedValue([])
    await render(<ConfiguredScreen config={config} />, (host) => {
      expect(host.textContent).toContain("111")
      expect(host.textContent).not.toContain("Tankhill Inn By SEB-111")
    })
  })

  it("keeps the business date label visible at mobile widths", () => {
    const shell = readFileSync("src/AppShell.tsx", "utf8")
    const label = shell.match(/<span[^>]*>\{t\("Business date"\)\}/)?.[0]
    expect(label).toBeDefined()
    expect(label).not.toContain("sr-only")
  })

  it("offers to enable approval when the front desk rate limit is restricted", async () => {
    await render(financePage(), async (host) => {
      const alert = host.querySelector('[role="alert"]')
      expect(alert?.textContent ?? "").toContain("Control gap")
      const button = alert?.querySelector("button")
      expect(button).toBeTruthy()
      await act(async () => button?.click())
      expect(host.querySelector<HTMLSelectElement>("#setting-allowance_approval")?.value).toBe("Hotel Admin or Finance")
      expect(host.querySelector<HTMLInputElement>("#setting-allowance_approval_above")?.disabled).toBe(false)
    })
  })

  it("shows cashier readiness and a direct configuration link", async () => {
    const module = { ...finance, cashier_readiness: { configured: 4, pin_enrolled: 3 },
      values: { ...finance.values, require_cashier_pin: 1, till_variance_tolerance: 0 } }
    await render(financePage(module), (host) => {
      expect(host.textContent).toContain("Configured cashiers")
      expect(host.textContent).toContain("PINs enrolled")
      expect(host.querySelector('a[href="#cashier-limits"]')).toBeTruthy()
    })
  })

  it("uses a white-label name when presenting the service role", async () => {
    await render(financePage({ ...finance, can_edit: false, editors: ["Kamra Agent"] }), (host) => {
      expect(host.textContent).not.toContain("Kamra Agent")
    })
  })

  it("disables the allowance threshold while the master switch is off", async () => {
    await render(financePage(), (host) => {
      expect(host.querySelector<HTMLInputElement>("#setting-allowance_approval_above")?.disabled).toBe(true)
      expect(host.textContent).not.toContain("0 means every allowance needs approval.")
    })
  })
})
