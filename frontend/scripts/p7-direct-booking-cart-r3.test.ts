import { expect, test } from "vitest"
import { fixturePage, withFixture } from "./white-label-routes.mjs"

test("a lost checkout response survives closing the cart, and changing the guest starts a new checkout", async () => {
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
      await route.fulfill({ status: 503, json: { message: "Response lost" } })
    })
    try {
      await page.goto("/hotel/book/2031-03-13/2031-03-15/2/0")
      await page.getByRole("button", { name: "Add to booking cart" }).first().click()
      for (const guest of ["First Guest", "First Guest", "Second Guest"]) {
        await page.getByRole("button", { name: "Booking cart (1)", exact: true }).click()
        const sheet = page.getByRole("dialog")
        await sheet.getByLabel("Full name", { exact: true }).fill(guest)
        await sheet.getByLabel("Phone", { exact: true }).fill("+256700000919")
        await sheet.getByRole("button", { name: "Complete your booking" }).click()
        await sheet.getByRole("alert").waitFor()
        await page.keyboard.press("Escape")
        await sheet.waitFor({ state: "detached" })
      }
      expect(keys).toHaveLength(3)
      expect(keys[0]).toBeTruthy()
      expect(keys[1]).toBe(keys[0])
      expect(keys[2]).not.toBe(keys[0])
    } finally {
      await page.context().close()
    }
  })
}, 90000)

test("the registration card verifies the ID of the displayed reservation", async () => {
  await withFixture(async (browser, origin) => {
    const page = await fixturePage(browser, origin, "Front Desk", { width: 1024, height: 900 }, {
      "kamra.api.property_locale": { currency: "INR", id_types: ["Passport"], integrations: { verify_guest_id: true } },
      "kamra.api.registration_card": {
        property: { property_name: "Fixture Hotel", checkin_time: "14:00", checkout_time: "11:00" },
        reservation: { name: "STAY-R3", status: "Confirmed", adults: 1, nights: 2,
          check_in_date: "2031-03-13", check_out_date: "2031-03-15" },
        guest: { guest_id: "G-R3", full_name: "Fixture Guest", id_file: "/private/files/example.jpg", id_type: "Passport" },
        occupants: [],
      },
      "kamra.api.verify_guest_id": { filled: {}, charged: 0 },
    })
    try {
      await page.goto("/hotel/grc/STAY-R3")
      await page.getByLabel("Guest told their ID is read by our verification partner").check()
      const sent = page.waitForRequest(req => req.url().endsWith(".verify_guest_id"))
      await page.getByRole("button", { name: "Read details" }).click()
      expect((await sent).postDataJSON()).toMatchObject({ guest: "G-R3", reservation: "STAY-R3", consent: 1 })
    } finally {
      await page.context().close()
    }
  })
}, 90000)
