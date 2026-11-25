import assert from "node:assert/strict"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const staff = {
  enabled: 1, roles: ["Front Desk"], properties: null, editable: true,
  is_self: false, last_active: null, last_login: null,
}
const failures = []
function check(label, value, threshold) {
  console.log(`${label}: ${value.toFixed(2)} (minimum ${threshold})`)
  if (value < threshold) failures.push(label)
}

function contrast(locator, foreground) {
  return locator.evaluate((element, property) => {
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 1
    const context = canvas.getContext("2d")
    function luminance(color) {
      context.clearRect(0, 0, 1, 1)
      context.fillStyle = color
      context.fillRect(0, 0, 1, 1)
      const rgb = [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)]
      const linear = rgb.map((byte) => {
        const channel = byte / 255
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
      })
      return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722
    }
    const style = getComputedStyle(element)
    const container = getComputedStyle(element.closest("dialog")).backgroundColor
    const color = luminance(style[property])
    const backgrounds = property === "borderTopColor" ? [style.backgroundColor, container] : [container]
    return Math.min(...backgrounds.map((background) => {
      const bg = luminance(background)
      return (Math.max(bg, color) + 0.05) / (Math.min(bg, color) + 0.05)
    }))
  }, foreground)
}

await withFixture(async (browser, origin) => {
  for (const theme of ["light", "dark"]) {
    const page = await fixturePage(browser, origin, "Hotel Admin", { width: 960, height: 850 }, {
      "kamra.api.whoami": { user: "staff@example.test", roles: ["Hotel Admin"], can_manage_approval_pin: true },
      "kamra.api.my_pin_status": { has_pin: false, must_reset: false, locked: false, locked_until: null },
      "kamra.staff.list_staff": [
        { ...staff, user: "enrolled@example.test", email: "enrolled@example.test", full_name: "Enrolled Staff", pin: "set" },
        { ...staff, user: "new@example.test", email: "new@example.test", full_name: "New Staff", pin: "none" },
      ],
    })
    try {
      await page.addInitScript((value) => localStorage.setItem("kamra-theme", value), theme)
      let submissions = 0
      await page.route("**/api/method/kamra.api.set_cashier_pin", (route) => {
        submissions += 1
        return route.fulfill(submissions === 1
          ? { status: 417, json: { exception: "Test password rejected" } }
          : { json: { message: { ok: true } } })
      })
      await settled(page, "/settings/staff")
      await page.getByText("No PIN", { exact: true }).waitFor()
      const reset = page.getByRole("button", { name: "Reset cashier PIN" })
      await reset.waitFor()
      check(`${theme} staff reset height`, (await reset.boundingBox()).height, 44)
      await page.getByRole("button", { name: "Account menu" }).click()
      const entry = page.getByRole("menuitem", { name: "My Approval PIN" })
      await entry.waitFor()
      check(`${theme} account menu PIN height`, (await entry.boundingBox()).height, 44)
      await entry.click()
      const dialog = page.getByRole("dialog", { name: "My Approval PIN" })
      for (const label of ["Account password", "New PIN (4–8 digits)", "Confirm PIN"]) {
        const input = dialog.getByLabel(label, { exact: true })
        await input.waitFor()
        check(`${theme} ${label} border`, await contrast(input, "borderTopColor"), 3)
        await input.fill(label === "Account password" ? "synthetic-password" : "4067")
      }
      const save = dialog.getByRole("button", { name: "Save PIN" })
      await save.click()
      const error = dialog.getByRole("alert")
      await error.waitFor()
      check(`${theme} error text`, await contrast(error, "color"), 4.5)
      await dialog.getByLabel("Account password", { exact: true }).fill("synthetic-password")
      await save.click()
      const success = dialog.getByText("Approval PIN saved.", { exact: true })
      await success.waitFor()
      check(`${theme} success text`, await contrast(success, "color"), 4.5)
    } finally {
      await page.context().close()
    }
  }
})
assert.deepEqual(failures, [], "Approval PIN contrast and touch targets")
