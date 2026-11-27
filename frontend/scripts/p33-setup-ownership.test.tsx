// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { expect, it, vi } from "vitest"
import Setup from "../src/screens/Setup"

vi.mock("../src/lib/api", async (original) => ({
  ...(await original<object>()),
  call: async (method: string) => method === "kamra.api.localization_countries"
    ? [{ country: "Uganda", currency: "UGX", timezone: Intl.DateTimeFormat().resolvedOptions().timeZone }]
    : { property: "Fixture Hotel" },
  setCurrentProperty: vi.fn(),
}))
vi.mock("../src/lib/money", async (original) => ({ ...(await original<object>()), loadLocale: vi.fn() }))
vi.mock("../src/screens/GoLiveImport", () => ({ default: () => <section>CSV uploader</section> }))

it.each([true, false])("guided import respects assistant disabled = %s", async (disabled) => {
  window.kamra_mcp_disabled = disabled
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  async function click(text: string) {
    const button = [...host.querySelectorAll("button")].find(b => b.textContent?.includes(text))
    expect(button?.disabled, text).toBe(false)
    await act(async () => { button!.click() })
  }
  try {
    await act(async () => { root.render(<MemoryRouter><Setup /></MemoryRouter>) })
    await click("Guided setup")
    await click("Continue")
    const name = host.querySelector<HTMLInputElement>('input[placeholder="Sunrise Residency"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(name, "Fixture Hotel")
      name.dispatchEvent(new Event("input", { bubbles: true }))
    })
    for (let step = 0; step < 8; step++) {
      if (![...host.querySelectorAll("button")].some(b => b.textContent === "Continue")) break
      await click("Continue")
    }
    await click("Create property")
    expect(host.textContent).toMatch(/CSV templates/)
    expect(host.textContent).toContain("CSV uploader")
    expect.soft(host.textContent?.includes("AI migration assistant")).toBe(!disabled)
    expect.soft(host.textContent?.includes("guest concierge on WhatsApp")).toBe(!disabled)
    expect.soft(host.textContent?.includes("Connect Claude")).toBe(!disabled)
  } finally {
    await act(async () => root.unmount())
    host.remove()
    delete window.kamra_mcp_disabled
  }
})
