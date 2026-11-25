import type { Locator } from "playwright"
import { expect, test } from "vitest"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const number = "+12025550114"
const guest = "Fixture Guest"
const message = "Your booking is confirmed."
const sentAt = "2031-03-12 14:35:00"
const template = "booking_confirmation"
const status = "Delivered"

const replies = {
  "kamra.whatsapp.threads": [{
    number, guest_name: guest, last_message: message,
    last_direction: "Outbound", last_status: status, last_at: sentAt,
  }],
  "kamra.whatsapp.thread": {
    session_open: true,
    messages: [{
      name: "WHATSAPP-FIXTURE", direction: "Outbound", message_type: "Template",
      template_name: template, content: message, status, creation: sentAt,
    }],
  },
}

async function contrastAgainstBubble(text: Locator) {
  return text.evaluate((element) => {
    const bubble = element.closest(".bg-accent-fill")!
    const swatch = document.createElement("canvas")
    swatch.width = swatch.height = 1
    const ctx = swatch.getContext("2d", { willReadFrequently: true })!

    function luminance() {
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
      const linear = (channel: number) => {
        const srgb = channel / 255
        return srgb <= 0.04045 ? srgb / 12.92 : ((srgb + 0.055) / 1.055) ** 2.4
      }
      return 0.2126 * linear(r) + 0.7152 * linear(g) + 0.0722 * linear(b)
    }

    ctx.fillStyle = getComputedStyle(bubble).backgroundColor
    ctx.fillRect(0, 0, 1, 1)
    if (ctx.getImageData(0, 0, 1, 1).data[3] !== 255) {
      throw new Error("Expected an opaque outbound bubble")
    }
    const background = luminance()
    ctx.fillStyle = getComputedStyle(element).color
    ctx.fillRect(0, 0, 1, 1)
    const foreground = luminance()
    return (Math.max(background, foreground) + 0.05) / (Math.min(background, foreground) + 0.05)
  })
}

test("outbound WhatsApp metadata meets AA contrast in light and dark", async () => {
  await withFixture(async (browser, origin) => {
    const page = await fixturePage(browser, origin, "Front Desk", { width: 1024, height: 768 }, replies)
    try {
      await page.addInitScript(() => {
        localStorage.setItem("kamra-lang", "en")
        localStorage.setItem("kamra-theme", "system")
      })

      for (const theme of ["light", "dark"] as const) {
        await page.emulateMedia({ colorScheme: theme })
        await settled(page, "/hotel/whatsapp")
        expect(await page.locator("html").evaluate((html) => html.classList.contains("dark")))
          .toBe(theme === "dark")
        const bodyText = page.getByText(message, { exact: true })
        const bubble = page.locator("main .bg-accent-fill").filter({ has: bodyText })
        await bubble.waitFor({ state: "visible" })
        const samples = {
          "template label": bubble.getByText(template, { exact: false }),
          "timestamp and delivery status": bubble.getByText(/^.+ · Delivered$/),
          "message body": bubble.getByText(message, { exact: true }),
        }
        for (const [label, sample] of Object.entries(samples)) {
          await sample.waitFor({ state: "visible" })
          const ratio = await contrastAgainstBubble(sample)
          console.info(`WhatsApp contrast ${theme} ${label} ${ratio.toFixed(2)}:1`)
          expect.soft(ratio, `${theme} ${label}`).toBeGreaterThanOrEqual(4.5)
        }
      }
    } finally {
      await page.context().close()
    }
  })
}, 90_000)
