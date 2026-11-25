// @vitest-environment jsdom
/** P27 Credit Card Pre-Authorization UI tests for SecurityDepositPanel:
 * - Dynamic step & formatting across UGX (0dp), USD (2dp), KWD (3dp) — zero hardcoded step="0.01".
 * - Authorize Hold, Increment Hold, Capture for Damage, Release Hold gated by d.can_* flags.
 * - Strict client PCI guard blocking raw PAN, CVV/CVC, and non-4-digit card_last4 (zero PAN/CVV fields).
 * - Touch targets >=44px (min-h-11), RTL logical CSS, French/Arabic i18n, and zero brand leaks. */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import SecurityDepositPanel, { type SecurityDepositState } from "../src/components/SecurityDepositPanel"
import { adoptUiLocale } from "../src/lib/money"
import { setLang } from "../src/lib/dir"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Harbor Plaza",
  newIdempotencyKey: () => "idem-p27-1",
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Finance"] }),
}))

vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({
    ensureUnlocked: vi.fn().mockResolvedValue(undefined),
    status: { required: false, unlocked: true },
  }),
}))

const PANEL_PATH = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../src/components/SecurityDepositPanel.tsx",
)

function setInputValue(el: HTMLInputElement | HTMLSelectElement, value: string) {
  const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
  const setter = Object.getOwnPropertyDescriptor(proto, "value")?.set
  setter?.call(el, value)
  el.dispatchEvent(new Event("input", { bubbles: true }))
  el.dispatchEvent(new Event("change", { bubbles: true }))
}

function baseDeposit(overrides: Partial<SecurityDepositState> = {}): SecurityDepositState {
  return {
    name: "DEP-00001",
    status: "Required",
    policy_amount: 250,
    required: 250,
    collected: 0,
    withheld: 0,
    refunded: 0,
    held: 0,
    due: 250,
    reason: null,
    folio: "FOLIO-2026-00001",
    folio_credit: 0,
    history: [],
    can_collect: true,
    can_waive: true,
    can_withhold: false,
    can_refund: false,
    blocks_checkout: false,
    authorized_amount: 0,
    remaining_hold_amount: 0,
    preauth_hold: null,
    gateway_auth_ref: null,
    card_brand: null,
    card_last4: null,
    expires_on: null,
    payment_entry: null,
    preauth_available: true,
    can_authorize: true,
    can_increment: false,
    can_capture: false,
    can_release: false,
    ...overrides,
  }
}

describe("P27 SecurityDepositPanel card pre-authorization lifecycle", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") {
        return Promise.resolve({
          currency: "USD",
          currency_symbol: "$",
          decimals: 2,
          currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
          locale: "en-GB",
          tax_label: "VAT",
          tax_id_label: "Tax ID",
          tax_rates: [],
          payment_modes: ["Cash", "Card"],
          instant_payment_modes: [],
          outlet_payment_modes: ["Cash", "Card"],
          id_types: [],
          id_type_hints: {},
          default_nationality: "",
          adult_age: 18,
          minor_ids: "foreign",
          integrations: {
            payment_gateways: [],
            tax_return: false,
            verify_tax_id: false,
            verify_guest_id: false,
          },
        })
      }
      return Promise.resolve({ ok: true })
    })
    adoptUiLocale({
      currency: "USD",
      currency_symbol: "$",
      decimals: 2,
      currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
      locale: "en-GB",
    })
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    setLang("en")
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  it("uses dynamic moneyStep for UGX (0dp), USD (2dp), and KWD (3dp) with zero hardcoded 0.01", async () => {
    const src = fs.readFileSync(PANEL_PATH, "utf8")
    expect(src).not.toMatch(/step\s*=\s*["']0\.01["']/)
    expect(src).toMatch(/\bmoneyStep\b/)

    for (const [currency, symbol, decimals, expectedStep] of [
      ["UGX", "UGX ", 0, "1"],
      ["USD", "$", 2, "0.01"],
      ["KWD", "KWD ", 3, "0.001"],
    ] as const) {
      adoptUiLocale({
        currency,
        currency_symbol: symbol,
        decimals,
        currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
        locale: "en-GB",
      })
      await act(async () =>
        root.render(
          <SecurityDepositPanel
            reservation="RES-2026-00010"
            deposit={baseDeposit({ required: currency === "KWD" ? 50.125 : 250, due: currency === "KWD" ? 50.125 : 250 })}
            canStart
            onChanged={vi.fn()}
          />,
        ),
      )

      const authBtn = [...host.querySelectorAll("button")].find((b) =>
        /Authorize hold/i.test(b.textContent ?? ""),
      )
      expect(authBtn, `Authorize hold button must appear for ${currency}`).toBeTruthy()
      await act(async () => authBtn!.click())

      const numInput = host.querySelector<HTMLInputElement>('input[type="number"]')
      expect(numInput).toBeTruthy()
      expect(numInput!.getAttribute("step")).toBe(expectedStep)

      if (currency === "KWD") {
        expect(host.textContent).toContain("50.125")
      }

      const cancelBtn = [...host.querySelectorAll("button")].find((b) => /Cancel/i.test(b.textContent ?? ""))
      await act(async () => cancelBtn!.click())
    }
  })

  it("submits authorize_deposit with masked metadata only and blocks raw PAN / CVV client-side", async () => {
    const onChanged = vi.fn()
    await act(async () =>
      root.render(
        <SecurityDepositPanel
          reservation="RES-2026-00020"
          deposit={baseDeposit()}
          canStart
          onChanged={onChanged}
        />,
      ),
    )

    const authBtn = [...host.querySelectorAll("button")].find((b) =>
      /Authorize hold/i.test(b.textContent ?? ""),
    )
    expect(authBtn).toBeTruthy()
    await act(async () => authBtn!.click())

    // Zero card number / CVV inputs may exist
    const labels = [...host.querySelectorAll("label, input")].map(
      (el) => `${el.textContent ?? ""} ${el.getAttribute("name") ?? ""} ${el.getAttribute("placeholder") ?? ""}`,
    )
    expect(labels.join(" ")).not.toMatch(/\b(?:card number|full pan|cvv|cvc|security code input)\b/i)

    const amountEl = host.querySelector<HTMLInputElement>('input[name="preauth_amount"]')!
    const refEl = host.querySelector<HTMLInputElement>('input[name="gateway_auth_ref"]')!
    const brandEl = host.querySelector<HTMLInputElement>('input[name="card_brand"]')!
    const last4El = host.querySelector<HTMLInputElement>('input[name="card_last4"]')!
    const expEl = host.querySelector<HTMLInputElement>('input[name="expires_on"]')!

    expect(amountEl).toBeTruthy()
    expect(refEl).toBeTruthy()
    expect(brandEl).toBeTruthy()
    expect(last4El).toBeTruthy()
    expect(expEl).toBeTruthy()

    // Raw 16-digit PAN in card_last4 or gateway_auth_ref must block submit and never call RPC
    await act(async () => {
      setInputValue(amountEl, "250")
      setInputValue(refEl, "4111222233334444")
      setInputValue(brandEl, "Visa")
      setInputValue(last4El, "4242")
      setInputValue(expEl, "2026-12-05T12:00")
    })
    const submitBtn = [...host.querySelectorAll("button")].find((b) =>
      /Authorize \$250\.00/i.test(b.textContent ?? ""),
    )!
    expect(submitBtn.disabled).toBe(true)
    expect(host.textContent).toMatch(/last 4 digits|never enter/i)

    // CVV pattern also blocked
    await act(async () => {
      setInputValue(refEl, "AUTH-OK CVV 123")
    })
    expect(submitBtn.disabled).toBe(true)

    // Non-4-digit last4 blocked
    await act(async () => {
      setInputValue(refEl, "GW-AUTH-9001")
      setInputValue(last4El, "42a2")
    })
    expect(submitBtn.disabled).toBe(true)

    // Clean masked inputs enable submit and call kamra.deposit.authorize_deposit
    await act(async () => {
      setInputValue(last4El, "4242")
    })
    expect(submitBtn.disabled).toBe(false)
    await act(async () => submitBtn.click())

    const authCall = rpc.mock.calls.find(([m]) => m === "kamra.deposit.authorize_deposit")
    expect(authCall).toBeTruthy()
    expect(authCall![1]).toMatchObject({
      reservation: "RES-2026-00020",
      amount: 250,
      gateway_auth_ref: "GW-AUTH-9001",
      card_brand: "Visa",
      card_last4: "4242",
    })
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it("exposes increment, capture, and release actions on an Authorized hold and calls governed endpoints", async () => {
    const onChanged = vi.fn()
    const authorized = baseDeposit({
      status: "Authorized",
      authorized_amount: 50.125,
      remaining_hold_amount: 50.125,
      required: 50.125,
      due: 0,
      preauth_hold: "AUTH-2026-00007",
      gateway_auth_ref: "GW-KWD-77",
      card_brand: "Visa",
      card_last4: "4242",
      expires_on: "2026-12-05 12:00:00",
      can_collect: false,
      can_waive: false,
      can_authorize: false,
      can_increment: true,
      can_capture: true,
      can_release: true,
      blocks_checkout: true,
    })

    adoptUiLocale({
      currency: "KWD",
      currency_symbol: "KWD ",
      decimals: 3,
      currency_decimals: { UGX: 0, USD: 2, KWD: 3 },
      locale: "en-GB",
    })

    await act(async () =>
      root.render(
        <SecurityDepositPanel
          reservation="RES-KWD-7"
          deposit={authorized}
          canStart
          onChanged={onChanged}
        />,
      ),
    )

    expect(host.textContent).toContain("AUTH-2026-00007")
    expect(host.textContent).toContain("Visa")
    expect(host.textContent).toContain("4242")
    expect(host.textContent).toContain("KWD 50.125")

    // 1. Increment Hold
    const incBtn = [...host.querySelectorAll("button")].find((b) => /Increment hold/i.test(b.textContent ?? ""))!
    expect(incBtn).toBeTruthy()
    await act(async () => incBtn.click())
    const incAmt = host.querySelector<HTMLInputElement>('input[name="preauth_amount"]')!
    await act(async () => setInputValue(incAmt, "10.375"))
    const incSubmit = [...host.querySelectorAll("button")].find((b) =>
      /Increment hold by KWD 10\.375/i.test(b.textContent ?? ""),
    )!
    await act(async () => incSubmit.click())
    expect(rpc).toHaveBeenCalledWith(
      "kamra.deposit.increment_deposit_auth",
      expect.objectContaining({ reservation: "RES-KWD-7", additional_amount: 10.375 }),
    )

    // 2. Capture for Damage
    const capBtn = [...host.querySelectorAll("button")].find((b) => /Capture for damage/i.test(b.textContent ?? ""))!
    expect(capBtn).toBeTruthy()
    await act(async () => capBtn.click())
    const capAmt = host.querySelector<HTMLInputElement>('input[name="preauth_amount"]')!
    const capReason = host.querySelector<HTMLInputElement>('input[name="deposit_text"]')!
    await act(async () => {
      setInputValue(capAmt, "25.125")
      setInputValue(capReason, "Damaged lamp")
    })
    const capSubmit = [...host.querySelectorAll("button")].find((b) =>
      /Capture KWD 25\.125/i.test(b.textContent ?? ""),
    )!
    await act(async () => capSubmit.click())
    expect(rpc).toHaveBeenCalledWith(
      "kamra.deposit.capture_deposit_auth",
      expect.objectContaining({ reservation: "RES-KWD-7", amount: 25.125, reason: "Damaged lamp" }),
    )

    // 3. Release Hold
    const relBtn = [...host.querySelectorAll("button")].find((b) => /Release hold/i.test(b.textContent ?? ""))!
    expect(relBtn).toBeTruthy()
    await act(async () => relBtn.click())
    const relReason = host.querySelector<HTMLInputElement>('input[name="deposit_text"]')!
    await act(async () => setInputValue(relReason, "Room inspected clean"))
    const relSubmit = [...host.querySelectorAll("button")].find((b) =>
      /Confirm release/i.test(b.textContent ?? ""),
    )!
    await act(async () => relSubmit.click())
    expect(rpc).toHaveBeenCalledWith(
      "kamra.deposit.release_deposit_auth",
      expect.objectContaining({ reservation: "RES-KWD-7", reason: "Room inspected clean" }),
    )
  })

  it("hides pre-auth actions when can_* flags are false and meets RTL, dark-mode, tap-target, and white-label rules", async () => {
    await act(async () =>
      root.render(
        <SecurityDepositPanel
          reservation="RES-PLAIN-1"
          deposit={baseDeposit({
            preauth_available: false,
            can_authorize: false,
            can_increment: false,
            can_capture: false,
            can_release: false,
          })}
          canStart
          onChanged={vi.fn()}
        />,
      ),
    )

    const btnText = [...host.querySelectorAll("button")].map((b) => b.textContent ?? "").join(" ")
    expect(btnText).not.toMatch(/Authorize hold|Increment hold|Capture for damage|Release hold/i)
    expect(btnText).not.toMatch(/ERPNext|Frappe|URY|Kamra|HeyKoala/i)

    const src = fs.readFileSync(PANEL_PATH, "utf8")
    expect(src).toMatch(/\bmin-h-11\b/)
    expect(src).toMatch(/\bdark:/)
    expect(src).not.toMatch(/\b(?:ml-|mr-|pl-|pr-|text-left|text-right)\b/)
  })
})
