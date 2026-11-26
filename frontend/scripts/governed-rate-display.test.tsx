// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { expect, it, vi } from "vitest"
import { RateOptions } from "../src/components/RateOptions"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})
vi.mock("../src/lib/api", async original => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: rpc,
  getCurrentProperty: () => "Hotel",
}))

// A catalog can have a foreign contract even when another active plan uses the card.
const policies = [
  { policy: "mixed", required: false, governed: false },
  { policy: "mandatory", required: true, governed: false },
  { policy: "tariffs", required: false, governed: true },
]
const currencies = [
  { currency: "USD", expected: "150.00" },
  { currency: "UGX", expected: "150" },
  { currency: "KWD", expected: "150.000" },
]
const cases = policies.flatMap(policy => currencies.flatMap(currency =>
  ["Standard", "Default", "Negotiated", "Complimentary", "House Use"].map(category => ({
    ...policy, ...currency, category,
  })),
))

it.each(cases)("renders $currency for a $category plan in a $policy catalog", async ({
  currency, expected, category, required, governed,
}) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  vi.useFakeTimers()
  const host = document.createElement("div")
  const root = createRoot(host)
  const zero = category === "Complimentary" || category === "House Use"
  const negotiated = category === "Negotiated"
  const fx = currency === "UGX" ? 1 : currency === "USD" ? 4200 : 12000
  const local = zero ? 0 : 150 * fx
  const tariff = { currency, nightly: zero ? 0 : 150, property_currency: "UGX", fx_rate: fx, fx_date: "2026-11-01" }
  rpc.mockResolvedValue({
    rate_plan_required: required, all_active_plans_tariff: governed,
    company_plan: negotiated ? "TEST" : null, voucher: null, nights: [],
    plain: { room_total: 410619, amount_after_tax: 410619 },
    plans: [{ name: "TEST", label: "Published plan", tariff, agreed_rate: local,
      is_default: category === "Default", rate_category: zero ? category : "Standard" }],
  })
  try {
    await act(async () => root.render(<RateOptions roomType="STD" checkIn="2026-11-26" checkOut="2026-11-27"
      adults={2} children={0} ratePlan="TEST" onPickPlan={() => {}} nightPrices={{}}
      onNightPrices={() => {}} canSetPrice={false} />))
    await act(async () => { await vi.advanceTimersByTimeAsync(350) })
    const plan = host.querySelector('button[aria-pressed="true"]')
    expect(plan?.textContent).toContain(`${currency} ${zero ? expected.replace("150", "0") : expected} / night`)
    expect(plan?.textContent).toContain(`≈ UGX ${local.toLocaleString("en-US")}`)
    if (category !== "Standard") expect(plan?.textContent).toContain(category)
    expect(host.textContent?.includes("Standard rate")).toBe(!governed && !required && !negotiated)
  } finally {
    await act(async () => root.unmount())
    vi.useRealTimers()
    vi.unstubAllGlobals()
  }
})
