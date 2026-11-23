// @vitest-environment jsdom
/* The discount rules (lib/reservationDiscount) and the shared DiscountPanel:
 * OPERA Cloud Discount % OR Amount per night + mandatory reason, Daily
 * Details with Apply to future nights, the SAP OVB2 bounds and the inline
 * supervisor PIN when the cut is over the front desk's limit. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import DiscountPanel from "../src/components/DiscountPanel"
import {
  applyToFutureNights,
  discountArgs,
  discountEligible,
  discountError,
  discountReady,
  emptyDiscount,
  supervisorArgs,
  type DiscountGovernance,
  type DiscountState,
} from "../src/lib/reservationDiscount"

const NIGHTS = ["2031-03-17", "2031-03-18"]
const REASONS = [
  { name: "RATE-PROMO", code: "RATE-PROMO", description: "Promotional rate change", category: "Rate Change", requires_supervisor: 0 },
  { name: "RATE-MGR", code: "RATE-MGR", description: "Manager rate override", category: "Rate Change", requires_supervisor: 1 },
  { name: "ADJ-RATE", code: "ADJ-RATE", description: "Rate correction", category: "Adjust", requires_supervisor: 0 },
]
const governance = (over: Partial<DiscountGovernance> = {}): DiscountGovernance => ({
  base_total: 2000, discount_total: 200, net_total: 1800, effective_pct: 10, max_pct: 10,
  limit_pct: 10, needs_supervisor: false, can_set_rate: false, agent: false, eligible: true,
  comp: false, floor_breach: [], reason_requires_supervisor: false,
  supervisors: [{ user: "gm@test", full_name: "General Manager" }], ...over,
})
const state = (over: Partial<DiscountState> = {}): DiscountState => ({ ...emptyDiscount(), ...over })

describe("discount rules (lib/reservationDiscount)", () => {
  it("builds the payload the server takes, and nothing when no discount is entered", () => {
    expect(discountArgs(emptyDiscount(), NIGHTS)).toEqual({})
    expect(discountArgs(state({ type: "Percent", value: "10", reason: "RATE-PROMO" }), NIGHTS)).toEqual({
      discount_type: "Percent", discount_value: 10, discount_reason: "RATE-PROMO",
    })
    expect(discountArgs(state({
      type: "Amount", value: "", reason: "ADJ-RATE",
      nights: { "2031-03-18": { type: "Amount", value: "150" }, "2031-03-25": { type: "Percent", value: "5" } },
    }), NIGHTS)).toEqual({
      discount_reason: "ADJ-RATE",
      night_discounts: [{ date: "2031-03-18", discount_type: "Amount", discount_value: 150 }],
    })
  })

  it("copies a night's discount to every later night (Apply to future nights)", () => {
    const nights = applyToFutureNights({}, ["a", "b", "c"], "b", { type: "Percent", value: "20" })
    expect(nights).toEqual({ b: { type: "Percent", value: "20" }, c: { type: "Percent", value: "20" } })
  })

  it("keeps the OVB2 bounds: 0..100% and never more than the night's rate", () => {
    expect(discountError(state({ type: "Percent", value: "101" }), 1000)).toBe("A percentage is between 0 and 100.")
    expect(discountError(state({ type: "Amount", value: "1200" }), 1000)).toBe("The discount is more than the night's rate.")
    expect(discountError(state({ type: "Amount", value: "-1" }), 1000)).toBe("Enter a discount of zero or more.")
    expect(discountError(state({ type: "Percent", value: "100" }), 1000)).toBeNull()
  })

  it("is ready only with a reason, and with a supervisor PIN when the server asks for one", () => {
    const args = { discount_type: "Percent", discount_value: 25 } as const
    expect(discountReady({ args: {}, governance: null, supervisor: "", pin: "" })).toBe(true)
    expect(discountReady({ args, governance: governance(), supervisor: "", pin: "" })).toBe(false)
    const withReason = { ...args, discount_reason: "RATE-PROMO" }
    expect(discountReady({ args: withReason, governance: governance(), supervisor: "", pin: "" })).toBe(true)
    const over = governance({ needs_supervisor: true })
    expect(discountReady({ args: withReason, governance: over, supervisor: "gm@test", pin: "" })).toBe(false)
    expect(discountReady({ args: withReason, governance: over, supervisor: "gm@test", pin: "4321" })).toBe(true)
    expect(supervisorArgs(over, "gm@test", " 4321 ")).toEqual({ supervisor_user: "gm@test", supervisor_pin: "4321" })
    expect(supervisorArgs(governance(), "gm@test", "4321")).toEqual({})
  })

  it("follows the rate plan's Discount flag (OPERA Rate Code Discount)", () => {
    const plans = [{ name: "BAR", discount_eligible: 1 }, { name: "NRF", discount_eligible: 0 }]
    expect(discountEligible(plans, "BAR")).toBe(true)
    expect(discountEligible(plans, "NRF")).toBe(false)
    expect(discountEligible(plans, "")).toBe(true)
    expect(discountEligible([{ name: "OLD" }], "OLD")).toBe(true)
  })
})

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

const text = () => host.textContent ?? ""
const byLabel = <T extends Element>(label: string, scope: ParentNode = host) =>
  scope.querySelector<T>(`[aria-label="${label}"]`)
async function typeInto(node: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
  await act(async () => {
    setter.call(node, value)
    node.dispatchEvent(new Event("input", { bubbles: true }))
  })
}
async function choose(node: HTMLSelectElement, value: string) {
  await act(async () => {
    node.value = value
    node.dispatchEvent(new Event("change", { bubbles: true }))
  })
}
async function press(label: string, scope: ParentNode = host) {
  const button = Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
  expect(button, `Missing button: ${label}`).toBeDefined()
  await act(async () => button!.click())
}
const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 0)))

describe("DiscountPanel", () => {
  function Harness(props: Partial<Parameters<typeof DiscountPanel>[0]> & { initial?: DiscountState }) {
    return <DiscountPanel value={props.initial ?? emptyDiscount()} onChange={props.onChange ?? (() => {})}
      reasons={REASONS} eligible={props.eligible ?? true} governance={props.governance ?? null}
      nights={props.nights} supervisor="" pin="" onSupervisor={() => {}} onPin={() => {}}
      money={(n) => `UGX ${n.toLocaleString("en")}`} />
  }

  it("offers % or an amount per night, a value and a mandatory reason", async () => {
    const onChange = vi.fn()
    await act(async () => root.render(<Harness onChange={onChange} />))
    expect(byLabel("Discount", host)).not.toBeNull()
    const pct = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "% off")!
    const amount = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Amount / night")!
    expect(pct.getAttribute("aria-pressed")).toBe("true")
    expect(amount.getAttribute("aria-pressed")).toBe("false")
    await act(async () => amount.click())
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ type: "Amount" }))
    const reason = byLabel<HTMLSelectElement>("Discount reason")!
    expect(Array.from(reason.options).map((o) => o.value)).toEqual(["", "RATE-PROMO", "RATE-MGR", "ADJ-RATE"])
    expect(reason.required).toBe(true)
    await typeInto(byLabel<HTMLInputElement>("Discount value")!, "12")
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ value: "12" }))
    expect(text()).toContain("Comes off the room rate only - meals, extras and tax keep their own price.")
  })

  it("is disabled with a hint for a rate plan that cannot be discounted", async () => {
    await act(async () => root.render(<Harness eligible={false} />))
    expect(byLabel<HTMLInputElement>("Discount value")!.disabled).toBe(true)
    expect(byLabel<HTMLSelectElement>("Discount reason")!.disabled).toBe(true)
    expect(text()).toContain("This rate plan does not allow discounts.")
  })

  it("asks for a supervisor and their PIN inline when the cut is over the limit", async () => {
    await act(async () => root.render(<Harness initial={state({ value: "25", reason: "RATE-PROMO" })}
      governance={governance({ needs_supervisor: true, effective_pct: 25, max_pct: 25 })} />))
    expect(text()).toContain("A cut of 25% is over the front desk's limit of 10%.")
    expect(text()).toContain("A manager signs this off with their PIN.")
    expect(byLabel("Manager PIN")).not.toBeNull()
    expect(Array.from(byLabel<HTMLSelectElement>("Manager")!.options).map((o) => o.value)).toEqual(["", "gm@test"])
  })

  it("shows per-night Daily Details with Apply to future nights", async () => {
    const onChange = vi.fn()
    await act(async () => root.render(<Harness onChange={onChange} initial={state({ reason: "RATE-PROMO" })}
      nights={[{ date: NIGHTS[0], base_rate: 1000 }, { date: NIGHTS[1], base_rate: 1000, charged: true }]} />))
    await press("Per-night daily details")
    const rows = host.querySelectorAll("tr[data-night]")
    expect(rows).toHaveLength(2)
    expect(rows[1].querySelector<HTMLInputElement>("input[type=number]")!.disabled).toBe(true)
    const future = byLabel<HTMLInputElement>("Apply to future nights")!
    expect(future.type).toBe("checkbox")
    await typeInto(rows[0].querySelector<HTMLInputElement>("input[type=number]")!, "100")
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({
      nights: { [NIGHTS[0]]: { type: "Percent", value: "100" } },
    }))
  })
})

