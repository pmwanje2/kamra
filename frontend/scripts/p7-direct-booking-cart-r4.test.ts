import { expect, test } from "vitest"
import { fixturePage, withFixture } from "./white-label-routes.mjs"

test("checkout recovers the last room after a lost response and a sold-out re-quote", async () => {
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
    const keys: string[] = []
    await page.route("**/kamra.public_api.create_multi_room_booking", async route => {
      keys.push(route.request().postDataJSON().idempotency_key)
      if (keys.length === 1) return route.abort("failed")
      await route.fulfill({ json: { message: { confirmation: "ORIGINAL-BOOKING", currency: "USD",
        currency_decimals: 2, amount_after_tax: 40, advance_due: 0, reservations: [] } } })
    })
    try {
      await page.goto("/hotel/book/2031-03-13/2031-03-15/2/0")
      await page.getByRole("button", { name: "Add to booking cart" }).first().click()
      const open = page.getByRole("button", { name: "Booking cart (1)", exact: true })
      const sheet = page.getByRole("dialog")
      const name = sheet.getByLabel("Full name", { exact: true })
      const submit = sheet.getByRole("button", { name: "Complete your booking" })
      await open.click()
      await name.fill("First Guest")
      await sheet.getByLabel("Phone", { exact: true }).fill("+256700000919")
      await submit.click()
      await sheet.getByRole("alert").waitFor()
      await page.keyboard.press("Escape")
      await sheet.waitFor({ state: "detached" })
      await page.route("**/kamra.public_api.cart_quote", route =>
        route.fulfill({ status: 409, json: { message: "Sold out" } }))
      await open.click()
      await sheet.getByRole("alert").waitFor()
      await name.fill("Different Guest")
      await sheet.getByLabel("Phone", { exact: true }).fill("+256700000919")
      expect(await submit.isEnabled()).toBe(false)
      await name.fill("First Guest")
      expect(await submit.isEnabled()).toBe(true)
      await submit.click()
      await sheet.getByText("ORIGINAL-BOOKING").waitFor()
      expect(keys).toHaveLength(2)
      expect(keys[1]).toBe(keys[0])
    } finally {
      await page.context().close()
    }
  })
}, 90000)
