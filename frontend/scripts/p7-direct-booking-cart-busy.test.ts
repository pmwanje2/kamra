import { expect, test } from "vitest"
import { fixturePage, withFixture } from "./white-label-routes.mjs"

test("Escape keeps in-flight checkout and its confirmation open", async () => {
  await withFixture(async (browser, origin) => {
    const page = await fixturePage(browser, origin, "Guest", { width: 1024, height: 900 }, {
      "kamra.public_api.showcase": {
        property: { name: "Hotel", property_name: "Hotel", city: "Kampala", checkin_time: "14:00",
          checkout_time: "11:00", faqs: [], gallery: [], amenities: [], minimum_nights: 1 },
        room_types: [{ name: "Standard", room_type_name: "Standard", adults_capacity: 2, amenities: [], media: [] }],
        locations: [], experiences: [], meal_plans: [],
      },
      "kamra.public_api.search_stay": [{ room_type: "Standard", available: true, rooms_left: 2,
        quote: { nights: 2, amount_after_tax: 40 }, plans: [{ meal_plan: "", amount_after_tax: 40 }] }],
      "kamra.public_api.cart_quote": { currency: "USD", currency_decimals: 2, amount_after_tax: 40,
        advance_due: 0, tax: 0, lines: [{ amount_after_tax: 40 }] },
    })
    let release!: () => void
    const pending = new Promise<void>(resolve => { release = resolve })
    await page.route("**/kamra.public_api.create_multi_room_booking", async route => {
      await pending
      await route.fulfill({ json: { message: { confirmation: "BOOKED", currency: "USD", currency_decimals: 2,
        amount_after_tax: 40, advance_due: 0, reservations: [{ reservation: "Stay", status: "Confirmed",
          precheckin_token: "fixture-link" }] } } })
    })
    try {
      await page.goto("/hotel/book/2031-03-13/2031-03-15/2/0")
      await page.getByRole("button", { name: "Add to booking cart" }).first().click()
      await page.getByRole("button", { name: "Booking cart (1)", exact: true }).click()
      const sheet = page.getByRole("dialog")
      await sheet.getByLabel("Full name", { exact: true }).fill("Checkout Guest")
      await sheet.getByLabel("Phone", { exact: true }).fill("+256700000919")
      const started = page.waitForRequest(r => r.url().endsWith(".create_multi_room_booking"))
      await sheet.getByRole("button", { name: "Complete your booking" }).click()
      await started
      await page.keyboard.press("Escape")
      expect(await sheet.count()).toBe(1)
      release()
      await sheet.getByText("BOOKED", { exact: true }).waitFor()
      expect(await sheet.getByRole("link").getAttribute("href")).toContain("/checkin/fixture-link")
      await page.keyboard.press("Escape")
      await sheet.waitFor({ state: "detached" })
    } finally {
      release()
      await page.context().close()
    }
  })
}, 90000)
