import assert from "node:assert/strict"
import { test } from "node:test"
import { withDesk } from "./tariff-test-fixture.mjs"

test("Room Types separates the card rate from approved, read-only tariffs and inclusive VAT", async () => {
  await withDesk(async (page, requests) => {
    await page.goto("/room-types")
    await page.getByRole("row", { name: /Standard Room/ }).click()
    const dialog = page.getByRole("dialog")
    await dialog.getByText("Code (e.g. DLX)", { exact: false }).waitFor()
    assert.ok(await dialog.getByText(/^Card rate \/ night \(UGX\)(?: \*)?$/).isVisible(), "card rate label must name the property currency")
    await dialog.getByText("Used only by rate plans without a contract tariff").waitFor()
    const panel = dialog.getByRole("region", { name: "Contract tariffs" })
    await panel.getByText("Corporate tariff").waitFor()
    for (const text of [/USD 130\.00/, /USD 100\.00/, /USD 20\.00/, /UGX 474,500/, /Approved/, /1 Nov/, /card rate is not used/i]) {
      await panel.getByText(text).first().waitFor()
    }
    assert.equal(await panel.locator("input, select, button").count(), 0)
    await dialog.getByText(/VAT 18% included/).waitFor()
    assert.equal(await page.getByText("Tax %", { exact: true }).count(), 0)
    assert.ok(requests.some(r => r.method === "kamra.rates.room_type_tariffs" && r.args.room_type === "STANDARD"))
  })
})
