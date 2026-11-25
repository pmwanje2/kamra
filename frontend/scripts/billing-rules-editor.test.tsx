// @vitest-environment jsdom
/* A company's billing rules (OPERA profile routing). A rule names a charge
   type or a transaction code; the editor ticks charge types and keeps the
   company's code rules when it saves, so a company that pays the food code
   and not the bar's keeps that set-up. There is no liquor rule to promise:
   liquor bills where the rules send its code, like any other charge. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import BillingRulesEditor from "../src/components/BillingRulesEditor"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Test Hotel",
}))

describe("Billing rules editor", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.get_billing_rules")
        return Promise.resolve([
          { charge_type: "Room", transaction_code: null, pay_by: "Company" },
          { charge_type: null, transaction_code: "2000", pay_by: "Company" },
        ])
      if (method === "kamra.api.company_billing_default")
        return Promise.resolve({ credit_allowed: true, has_rules: false, default: "Everything",
          charge_types: null, applies: true })
      return Promise.resolve({})
    })
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  async function render() {
    await act(async () => root.render(
      <BillingRulesEditor row={{ name: "UT Corp" } as never} reload={() => undefined} />))
    await vi.waitFor(() => expect(host.textContent).toContain("Everything"), { interval: 1 })
  }

  it("keeps the company's transaction code rules when it saves", async () => {
    await render()
    const save = [...host.querySelectorAll("button")].find((b) => b.textContent === "Save rules")
    await act(async () => save!.click())
    const sent = rpc.mock.calls.find(([method]) => method === "kamra.api.set_billing_rules")
    expect(sent?.[1]).toEqual({
      company: "UT Corp",
      rules: [
        { charge_type: "Room", pay_by: "Company" },
        { transaction_code: "2000", pay_by: "Company" },
      ],
    })
  })

  it("promises no liquor rule", async () => {
    await render()
    expect(host.textContent).toContain(" (every charge).")
    expect(host.textContent).not.toMatch(/liquor|alcohol/i)
  })
})
