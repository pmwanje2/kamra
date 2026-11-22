import assert from "node:assert/strict"
import { test } from "node:test"
import { withDesk } from "./tariff-test-fixture.mjs"

const reference = /USD 130\.00(?: \/ night)? · UGX 474,500 at 3,650\.00 \(21 Nov(?: 2026)?\)/

for (const [mode, width] of [["New booking", 1280], ["Walk-in", 412]]) {
  test(`${mode}: preselects the contract, shows both currencies and never offers Standard rate`, async () => {
    await withDesk(async (page, requests) => {
      await page.setViewportSize({ width, height: 900 })
      await page.goto("/rooms")
      await page.getByRole("button", { name: "New booking", exact: true }).click()
      const dialog = page.getByRole("dialog")
      if (mode === "Walk-in") await dialog.getByRole("radio", { name: "Walk-in", exact: true }).click()
      await dialog.getByText("Rates for these dates", { exact: true }).waitFor()
      assert.equal(await dialog.locator("label").filter({ has: page.getByText("Rate plan", { exact: true }) }).locator("select").inputValue(), "CONTRACT", "default contract plan should be selected")
      await dialog.getByRole("button", { name: /Corporate tariff.*USD 130\.00/ }).waitFor()
      await dialog.getByText(reference).first().waitFor()
      await dialog.getByText(/Includes VAT 18%/).first().waitFor()
      assert.equal(await dialog.getByText(/Standard rate|Tax 0%|410,619/).count(), 0)
      assert.ok(requests.some(r => r.method === "kamra.api.get_quote" && r.args.rate_plan === "CONTRACT"))
    })
  })
}

test("Folio shows the stored pricing-date reference, not a live re-quote", async () => {
  await withDesk(async (page, requests) => {
    await page.goto("/billing/FOLIO-K3")
    await page.getByText("Contract Guest", { exact: true }).first().waitFor()
    assert.ok(await page.getByText(reference).first().isVisible(), "frozen USD/UGX tariff is missing from the folio")
    assert.ok(!requests.some(r => r.method === "kamra.api.get_quote"))
  })
})
