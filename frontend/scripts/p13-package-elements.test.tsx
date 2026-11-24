// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import PackageElementsEditor from "../src/components/PackageElementsEditor"
import { adoptUiLocale } from "../src/lib/money"

const { rpc } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn() }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Royal Kuwait",
}))

describe("PackageElementsEditor (P13)", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  it("renders package elements, KWD 3dp live net room revenue preview, and saves Inclusive vs Separate Add-on mode", async () => {
    adoptUiLocale({ currency: "KWD", currency_symbol: "KD ", locale: "en-US" })
    rpc.mockReset().mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "kamra.packages.list_package_elements") {
        return {
          holiday_package: "PKG-00001",
          package_name: "Royal Wellness 2N",
          package_nights: 2,
          currency: "KWD",
          folio_display_mode: "Inclusive",
          price_1_pax: 500,
          price_2_pax: 600,
          price_3_pax: 750,
          price_4_pax: 900,
          elements: [
            {
              name: "PE-1",
              element_name: "Breakfast",
              element_type: "Meal",
              transaction_code: "1100",
              charge_type: "Meal Plan",
              allowance_basis: "Per Adult Night",
              unit_allowance: 20,
              posting_rhythm: "Every Night",
              included_in_rate: 1,
              folio_display_mode: "Inclusive",
            },
            {
              name: "PE-2",
              element_name: "Spa Credit",
              element_type: "Spa",
              transaction_code: "3100",
              charge_type: "Spa",
              allowance_basis: "Per Room Night",
              unit_allowance: 20,
              posting_rhythm: "Every Night",
              included_in_rate: 1,
              folio_display_mode: "Inclusive",
            },
          ],
          transaction_codes: [
            { code: "1100", description: "Meal Plan", charge_type: "Meal Plan" },
            { code: "3100", description: "Spa", charge_type: "Spa" },
            { code: "3200", description: "Taxi", charge_type: "Taxi" },
          ],
        }
      }
      if (method === "kamra.packages.save_package_elements") {
        return { ok: true, folio_display_mode: params.folio_display_mode, elements: params.elements }
      }
      return {}
    })

    const reload = vi.fn()
    await act(async () => {
      root.render(
        <PackageElementsEditor
          row={{ name: "PKG-00001", package_name: "Royal Wellness 2N", package_nights: 2, price_2_pax: 600 }}
          reload={reload}
        />,
      )
    })

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Package elements")
      expect(host.textContent).toContain("240.000")
    })

    // Switch folio display mode to Separate Add-on and save
    const sepBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Separate Add-on"),
    )
    expect(sepBtn).toBeDefined()
    await act(async () => sepBtn!.click())

    const saveBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Save elements"),
    )
    expect(saveBtn).toBeDefined()
    await act(async () => saveBtn!.click())

    expect(rpc).toHaveBeenCalledWith(
      "kamra.packages.save_package_elements",
      expect.objectContaining({
        holiday_package: "PKG-00001",
        folio_display_mode: "Separate Add-on",
      }),
    )
  })
})
