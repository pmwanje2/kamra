import assert from "node:assert/strict"
import { test } from "node:test"
import { withDesk } from "./tariff-test-fixture.mjs"

test("Room Types shows the contract tariff first and the card rate in a collapsed section, with inclusive VAT", async () => {
  await withDesk(async (page, requests) => {
    await page.goto("/room-types")
    await page.getByRole("row", { name: /Standard Room/ }).click()
    const dialog = page.getByRole("dialog")
    await dialog.getByText("Code (e.g. DLX)", { exact: false }).waitFor()
    const panel = dialog.getByRole("region", { name: "Contract tariffs" })
    await panel.getByRole("table").getByRole("row", { name: /Corporate tariff/ }).waitFor()
    await panel.getByText(/Published selling rate: 1 USD = 3,650 UGX/).waitFor()
    // the local equivalent is the server's (local_room, rounded as the plan rounds), never 130 x 3,650 = 474,500
    for (const text of [/USD 130\.00/, /USD 100\.00/, /USD 20\.00/, /UGX 475,000/, /Approved/, /1 Nov/, /card rate is not used/i]) {
      await panel.getByText(text).first().waitFor()
    }
    assert.equal(await panel.getByText(/UGX 474,500/).count(), 0, "the panel must not convert the tariff itself")
    assert.equal(await panel.locator("input, select, button").count(), 0)
    // the card rate (BAR) and extra-person prices sit in a collapsed, labelled section
    const card = dialog.locator("details", { has: page.getByText("Card rate (BAR) and extra-person prices", { exact: true }) })
    assert.equal(await card.getAttribute("open"), null, "the card rate section starts collapsed for an existing room type")
    await card.locator("summary").click()
    assert.ok(await card.getByText(/^Card rate \/ night \(UGX\)(?: \*)?$/).isVisible(), "card rate label must name the property currency")
    await card.getByText("Prices stays booked without a rate plan, and rate plans with no contract tariff for this room type.").waitFor()
    await dialog.getByText(/VAT 18% included/).waitFor()
    assert.equal(await page.getByText("Tax %", { exact: true }).count(), 0)
    assert.ok(requests.some(r => r.method === "kamra.rates.room_type_tariffs" && r.args.room_type === "STANDARD"))
  })
})
