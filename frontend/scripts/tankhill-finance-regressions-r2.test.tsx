// @vitest-environment jsdom
import { act, type ReactNode } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { describe, expect, it, vi } from "vitest"
import Billing from "../src/screens/Billing"
import { ModulePage } from "../src/screens/settings/ModulePage"
import type { SettingsModule } from "../src/screens/settings/api"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/api")>(),
  call: rpc,
  getCurrentProperty: () => "Tankhill Inn By SEB",
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

function finance(limit: number, approval = "Off"): SettingsModule {
  return {
    id: "finance", title: "Finance", description: "", group: "modules", toggle: false,
    enabled: true, editors: ["Finance"], can_edit: true, related: [],
    values: { rate_override_limit_pct: limit, allowance_approval: approval, allowance_approval_above: 0 },
    sections: [{
      id: "cashier", title: "Cashier controls", description: "",
      settings: [
        { key: "allowance_approval", label: "Allowances need a manager's approval", default: "Off",
          type: "select", options: ["Off", "Hotel Admin or Finance"], store: "kv", hint: "" },
        { key: "allowance_approval_above", label: "Only allowances above", default: 0,
          type: "number", store: "kv", hint: "" },
      ],
    }],
  }
}

const financePage = (module: SettingsModule) => <ModulePage module={module}
  sources={{ countries: [], languages: [], locales: [], timezones: [] }}
  onSaved={() => {}} onDirtyChange={() => {}} />

describe("R2 folio and approval regressions", () => {
  it("shows each permitted submitted credit note beside its linked invoice", async () => {
    const invoice = "ACC-SINV-2026-00009"
    const numbers = ["ACC-CN-00001", "ACC-CN-00002"]
    const row = { name: "FOLIO-1", sales_invoice: invoice, status: "Closed",
      credit_status: "Partially credited", balance: 100, net_collectible_balance: 75, credit_notes: numbers }
    rpc.mockImplementation(async (method) => method.includes("billing_folios") ? [row] : [])
    await render(<Billing />, (host) => {
      const cell = [...host.querySelectorAll("td")].find((td) => td.textContent?.includes(invoice))
      expect(cell?.textContent).toContain(invoice)
      for (const number of numbers) expect(cell?.textContent).toContain(number)
    })
  })

  it.each([0, 50, 100])("warns when approval is Off at a %s%% rate limit", async (limit) => {
    await render(financePage(finance(limit)), async (host) => {
      const alert = host.querySelector('[role="alert"]')
      expect(alert?.textContent ?? "").toContain("Control gap")
      expect(alert?.textContent).toContain("folio allowances")
      expect(host.querySelector<HTMLInputElement>("#setting-allowance_approval_above")?.disabled).toBe(true)
      await act(async () => alert?.querySelector("button")?.click())
      expect(host.querySelector<HTMLSelectElement>("#setting-allowance_approval")?.value).toBe("Hotel Admin or Finance")
      expect(host.querySelector('[role="alert"]')).toBeNull()
    })
  })
})
