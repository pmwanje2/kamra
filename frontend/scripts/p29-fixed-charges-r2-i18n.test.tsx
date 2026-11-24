// @vitest-environment jsdom
import fs from "node:fs"
import path from "node:path"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FixedChargesPanel from "../src/components/FixedChargesPanel"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
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

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const

describe("FixedChargesPanel R2 weekday i18n (Finding 6)", () => {
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
    setLang("en")
    vi.unstubAllGlobals()
  })

  it("renders localized Arabic RTL weekday badge and dropdown options with zero English weekday leaks", async () => {
    adoptUiLocale({ currency: "KWD", currency_symbol: "د.ك", locale: "ar-KW" })
    setLang("ar")
    await ensureDictionary("ar")

    rpc.mockReset().mockResolvedValue({
      reservation: "RES-2026-00021",
      property: "Royal Kuwait",
      currency: "KWD",
      check_in_date: "2026-11-25",
      check_out_date: "2026-11-28",
      rows: [
        {
          name: "FC-WEEKLY-1",
          transaction_code: "3400",
          charge_type: "Misc",
          description: "Weekly Transfer",
          qty: 1,
          unit_price: 30,
          frequency: "Weekly",
          day_of_week: "Wednesday",
          start_date: "2026-11-25",
          end_date: "2026-11-28",
          window_no: 2,
          active: 1,
          post_count: 1,
        },
      ],
      transaction_codes: [
        { code: "3400", description: "Resort Fee", charge_type: "Misc" },
      ],
    })

    await act(async () => {
      root.render(<FixedChargesPanel reservation="RES-2026-00021" status="Checked In" />)
    })

    await vi.waitFor(() => {
      expect(host.textContent).toContain("Weekly Transfer")
      expect(host.textContent).toContain("أسبوعي · الأربعاء")
      expect(host.textContent).not.toContain("Wednesday")
    })

    // Open Add form, switch frequency to Weekly, verify all 7 Arabic weekdays appear
    const addBtn = host.querySelector<HTMLButtonElement>("button")
    expect(addBtn).not.toBeNull()
    await act(async () => addBtn!.click())

    const freqSelect = host.querySelector<HTMLSelectElement>("select[name='fc_frequency']")
    expect(freqSelect).not.toBeNull()
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")!.set!.call(freqSelect!, "Weekly")
      freqSelect!.dispatchEvent(new Event("change", { bubbles: true }))
    })

    const dowSelect = host.querySelector<HTMLSelectElement>("select[name='fc_day_of_week']")
    expect(dowSelect).not.toBeNull()
    const labels = Array.from(dowSelect!.options).map((o) => o.textContent || "")
    for (const arDay of ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]) {
      expect(labels).toContain(arDay)
    }
    for (const enDay of WEEKDAYS) {
      expect(labels).not.toContain(enDay)
    }
  })

  it("uses static literal weekday translate() calls and ships all 7 weekdays across ar, es, pt, zh-CN and catalog.csv", () => {
    const frontendRoot = path.resolve(import.meta.dirname, "..")
    const src = fs.readFileSync(path.join(frontendRoot, "src/components/FixedChargesPanel.tsx"), "utf8")

    expect(src).not.toContain("translate(r.day_of_week)")
    expect(src).not.toContain("translate(w)")
    for (const day of WEEKDAYS) {
      expect(src).toContain(`translate("${day}")`)
    }

    for (const lang of ["ar", "es", "pt", "zh-CN"]) {
      const dict = JSON.parse(
        fs.readFileSync(path.join(frontendRoot, `src/i18n/locales/${lang}.json`), "utf8"),
      ) as Record<string, string>
      for (const day of WEEKDAYS) {
        expect(typeof dict[day], `${lang}.json missing ${day}`).toBe("string")
        expect(dict[day]?.trim().length, `${lang}.json empty ${day}`).toBeGreaterThan(0)
        expect(dict[day], `${lang}.json untranslated ${day}`).not.toBe(day)
      }
    }

    const csv = fs.readFileSync(path.join(frontendRoot, "src/i18n/catalog.csv"), "utf8")
    for (const day of WEEKDAYS) {
      expect(csv).toContain(`\n${day},${day},`)
    }
  })
})
