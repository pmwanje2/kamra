import type { Locator } from "playwright"
import { expect, test } from "vitest"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const property = "Fixture Hotel"
const room = "STD-101"
const today = new Date().toISOString().slice(0, 10)
const departure = new Date(Date.now() + 3 * 86400000).toISOString().slice(0, 10)
const stay = {
  name: "RES-R3", guest_name: "Fixture Sharer", property, room, room_number: "101",
  room_type: "STD", room_type_name: "Standard", status: "Confirmed", adults: 1, children: 0,
  check_in_date: today, check_out_date: departure, is_day_use: 0, nights: 3,
  share_group_id: "R3-SHARE", is_share_primary: 1,
}
const other = { ...stay, name: "RES-R3B", guest_name: "Second Sharer", is_share_primary: 0 }
const replies = {
  "kamra.api.property_locale": { currency: "USD", locale: "en", payment_modes: [] },
  "kamra.api.tape_chart": {
    start: today, days: 1, dates: [today], position: [], conflicts: [],
    rooms: [{ name: room, room_number: "101", room_type: "STD", room_type_name: "Standard",
      housekeeping_status: "Clean", bookings: [stay, other], blocks: [] }],
  },
  "kamra.tape_queue.room_metadata": { floors: [], rooms: {} },
  "kamra.tape_queue.unassigned_queue": { items: [] },
  "kamra.api.reservation_detail": {
    ...stay, business_date: today, guest: { name: "G-1", full_name: stay.guest_name },
    actions: {}, warnings: {}, night_rates: [], occupants: [],
    money: { total: 100, paid: 0, due: 100, has_folio: true, source: "booking" },
    share: {
      group_id: stay.share_group_id, mode: "Split Equal", currency: "USD", can_manage: true,
      members: [stay, other].map((member) => ({
        reservation: member.name, guest_name: member.guest_name, primary: !!member.is_share_primary,
        folio: null, custom_rate: 0, check_in: today, check_out: departure,
      })),
    },
  },
}

async function checkTheme(element: Locator, theme: "light" | "dark", location: string) {
  try {
    await element.waitFor({ timeout: 6000 })
  } catch (error) {
    throw new Error(`${location}: ${await element.page().locator("main").innerText()}`, { cause: error })
  }
  const colors = await element.evaluate((node) => {
    const style = getComputedStyle(node)
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 1
    const context = canvas.getContext("2d", { willReadFrequently: true })!
    function luminance(color: string) {
      context.clearRect(0, 0, 1, 1)
      context.fillStyle = color
      context.fillRect(0, 0, 1, 1)
      const [r, g, b, alpha] = context.getImageData(0, 0, 1, 1).data
      return {
        alpha,
        luminance: [r, g, b].reduce((total, byte, index) => {
          const value = byte / 255
          return total + [0.2126, 0.7152, 0.0722][index] *
            (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
        }, 0),
      }
    }
    const foreground = luminance(style.color).luminance
    const background = luminance(style.backgroundColor)
    const ratio = (Math.max(foreground, background.luminance) + 0.05) /
      (Math.min(foreground, background.luminance) + 0.05)
    return { ...background, ratio }
  })
  expect.soft(colors.alpha, location).toBe(255)
  expect.soft(colors.ratio, location).toBeGreaterThanOrEqual(4.5)
  if (theme === "dark") expect.soft(colors.luminance, location).toBeLessThan(0.15)
  else expect.soft(colors.luminance, location).toBeGreaterThan(0.65)
}

test("the Share panel, controls and mobile and desktop badges follow light and dark themes in RTL", async () => {
  await withFixture(async (browser, origin) => {
    for (const theme of ["light", "dark"] as const) {
      for (const width of [390, 1024, 1440]) {
        const page = await fixturePage(browser, origin, "Front Desk", { width, height: 900 }, replies)
        try {
          await page.context().addInitScript((preference) => {
            localStorage.setItem("kamra-lang", "ar")
            localStorage.setItem("kamra-theme", preference)
          }, theme)
          await page.context().route("**/api/resource/Reservation?*", (route) =>
            route.fulfill({ json: { data: [stay] } }))
          const location = `${theme}, ${width}px`
          await settled(page, "/hotel/tape")
          const badge = page.getByTitle("غرفة مشتركة بين 2 حجوزات").filter({ visible: true })
          await checkTheme(badge, theme, `${location} Tape Chart badge`)
          expect(await page.locator("html").getAttribute("dir")).toBe("rtl")

          await settled(page, "/hotel/reservations")
          await page.getByText(stay.guest_name, { exact: true }).filter({ visible: true }).first().click()
          const panel = page.getByRole("region", { name: "حجوزات الغرفة المشتركة" })
          await checkTheme(panel, theme, `${location} Share panel`)
          await checkTheme(panel.getByRole("combobox", { name: "طريقة تقسيم السعر" }), theme, `${location} rate mode`)
          await panel.getByRole("button", { name: "إضافة نزيل مشارك", exact: true }).click()
          await checkTheme(panel.getByRole("textbox", { name: "ابحث عن نزيل أو أدخل اسمًا جديدًا" }),
            theme, `${location} sharer input`)
          expect(await panel.evaluate((node) => node.scrollWidth <= node.clientWidth + 1)).toBe(true)
        } finally {
          await page.context().close()
        }
      }
    }
  })
}, 180_000)
