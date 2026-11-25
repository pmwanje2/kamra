import assert from "node:assert/strict"
import { test } from "node:test"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const labels = {
  fr: ["Devise comptable de l’établissement", "Devise tarifaire", "Total en devise tarifaire"],
  ar: ["عملة المحاسبة للمنشأة", "عملة السعر", "الإجمالي بعملة السعر"],
}
const propertyName = "Quotation Fixture Hotel"
const email = "verylongpropertycontactaddressforthereservationandaccountsdepartment@example.test"
const quote = {
  property: { property_name: propertyName, address: "5 Fixture Street", email, currency: "USD", locale: "en-US", tax_label: "Tax" },
  booking: {
    name: "QUOTE-1", guest_name: "Fixture Guest", check_in_date: "2031-03-12",
    check_out_date: "2031-03-13", nights: 1,
  },
  nights: [{ date: "2031-03-12", rate: 4.01, meal: 0 }],
  lines: [{ component: "accommodation", description: "Room nights", amount: 4.01 }],
  totals: { tax: 0, total: 4.01, deposit_due: 0 },
  terms: [], email_to: "booker@example.test", issued_on: "2031-03-01",
  currency_reference: {
    contract_currency: "KWD", fx_rate: 3.25,
    nights: [{ date: "2031-03-12", contract_rate: 1.235 }], contract_totals: { total: 1.234 },
  },
}

test("renders and prints a dual-currency booking quotation at 390px", async (t) => {
  await withFixture(async (browser, origin) => {
    for (const [lang, headings] of Object.entries(labels)) {
      const page = await fixturePage(browser, origin, "Front Desk", { width: 390, height: 844 }, {
        "kamra.api.booking_quotation": quote,
        "kamra.api.email_booking_quotation": { sent_to: quote.email_to, status: "Queued" },
      })
      try {
        await page.addInitScript((lang) => localStorage.setItem("kamra-lang", lang), lang)
        await settled(page, "/booking-quote/QUOTE-1")
        const header = page.locator("main header").filter({ hasText: propertyName })
        await header.waitFor()
        await t.test(`${lang} wraps property contacts without horizontal overflow`, async () => {
          assert.ok(await header.getByText(email).isVisible())
          const viewport = await page.evaluate(() => ({
            width: document.documentElement.clientWidth, content: document.documentElement.scrollWidth,
          }))
          assert.ok(viewport.content <= viewport.width, JSON.stringify(viewport))
        })
        await t.test(`${lang} keeps the quotation header visible in print and hides shell chrome`, async () => {
          await page.emulateMedia({ media: "print" })
          assert.ok(await header.isVisible(), "quotation header prints")
          assert.equal(await page.locator("header").filter({ hasNotText: propertyName }).first().isVisible(), false)
          await page.emulateMedia({ media: "screen" })
        })
        await page.emulateMedia({ media: "screen" })
        await t.test(`${lang} translates the currency headings and forwards the selected language for email`, async () => {
          assert.equal(await page.locator("html").getAttribute("dir"), lang === "ar" ? "rtl" : "ltr")
          for (const heading of headings) assert.ok(await page.locator("main").getByText(heading, { exact: false }).first().isVisible(), heading)
          const [request] = await Promise.all([
            page.waitForRequest((r) => r.url().endsWith("/kamra.api.email_booking_quotation")),
            page.locator("main button[title='booker@example.test']").click(),
          ])
          assert.deepEqual(request.postDataJSON(), { reservation: "QUOTE-1", _lang: lang })
        })
      } finally {
        await page.context().close()
      }
    }
  })
})
