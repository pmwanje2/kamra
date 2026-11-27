import { expect, test } from "vitest"
import { withFixture, fixturePage, settled } from "./white-label-routes.mjs"

test("R2 accepts decimal keystrokes at the property's currency precision", async () => {
  await withFixture(async (browser, origin) => {
    for (const [currency, typed] of [["USD", "1.25"], ["KWD", "1.025"]]) {
      const page = await fixturePage(browser, origin, "Hotel Admin", { width: 768, height: 900 }, {
        "kamra.api.property_locale": { currency, locale: "en-GB" },
        "kamra.cashier.tills_board": { tills: [], pending_transfers: [], variance_review: [] },
        "kamra.cashier.terminals": [], "kamra.printing.printers": [], "kamra.pos.outlets": [],
      })
      await settled(page, "/hotel/cashier/board")
      try { await page.getByRole("button", { name: "Add terminal" }).click({timeout:6000}) } catch (e) { throw new Error(await page.locator("body").innerText(), {cause:e}) }
      const input = page.getByLabel("Default opening float (" + currency + ")")
      await input.fill("")
      await input.pressSequentially(typed)
      expect.soft(await input.inputValue()).toBe(typed)
      await page.context().close()
    }
  })
}, 180000)

test("R2 FX remediation opens read-only and requests the named currency", async () => {
  await withFixture(async (browser, origin) => {
    for (const role of ["Night Auditor", "Revenue Manager"]) {
      const page = await fixturePage(browser, origin, role, { width: 768, height: 900 }, {
        "kamra.api.property_locale": { currency: "UGX", locale: "en-GB" },
        "kamra.ledger.list_exchange_rates": { rates: [], posting_rates: [{ currency: "KWD", rate: null, status: "Missing" }] },
      })
      const request = page.waitForRequest(r => r.url().includes("kamra.ledger.list_exchange_rates"))
      await settled(page, "/hotel/cashier/fx?currency=KWD")
      const read = await request
      const args = {...Object.fromEntries(new URL(read.url()).searchParams), ...read.postDataJSON()}
      expect.soft(args.currency).toBe("KWD")
      await page.getByText(/KWD → UGX/).waitFor()
      expect.soft(await page.getByRole("button", { name: "Save", exact: true }).count()).toBe(0)
      expect.soft(await page.locator("main").innerText()).toMatch(/read.only/i)
      await page.context().close()
    }
  })
}, 180000)

test("R2 preserves a provider-imported mapping currency on save", async () => {
  await withFixture(async (browser, origin) => {
    const page = await fixturePage(browser, origin, "Hotel Admin", { width: 1024, height: 900 })
    const row = { name: "MAPPING", connection: "Provider", room_type: "Room", external_room_id: "101", external_currency: "KWD" }
    const writes: Record<string, unknown>[] = []
    await page.route("**/api/v2/doctype/**/meta", route =>
      route.fulfill({ json: { data: { fields: [{ fieldname: "external_currency", read_only: 1 }] } } }))
    await page.route("**/api/resource/**", route => {
      const url = decodeURIComponent(new URL(route.request().url()).pathname)
      if (route.request().method() === "PUT") writes.push(route.request().postDataJSON())
      const data = url.endsWith("/MAPPING") ? row : url.endsWith("/Channel Room Mapping") ? [row]
        : ["Provider", "Room", "KWD"].map(name => ({ name }))
      return route.fulfill({ json: { data } })
    })
    await settled(page, "/hotel/ota-mappings?open=MAPPING")
    await page.getByRole("button", { name: "Save", exact: true }).waitFor()
    await page.getByRole("button", { name: "Save", exact: true }).click()
    await expect.poll(() => writes.length).toBe(1)
    expect(writes[0]).not.toHaveProperty("external_currency")
    expect(writes[0].external_room_id).toBe("101")
    await page.context().close()
  })
}, 180000)
