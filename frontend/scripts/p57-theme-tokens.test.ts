import { readdirSync, readFileSync } from "node:fs"
import { join, relative } from "node:path"
import { fileURLToPath } from "node:url"
import type { Locator } from "playwright"
import ts from "typescript"
import { expect, test } from "vitest"
import { fixturePage, withFixture } from "./white-label-routes.mjs"

const frontend = fileURLToPath(new URL("../", import.meta.url))
const src = fileURLToPath(new URL("../src/", import.meta.url))

function themeViolations(source: string) {
  const file = ts.createSourceFile("source.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const violations: string[] = []
  function visit(node: ts.Node) {
    if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node)) {
      const tokens = node.text.trim().split(/\s+/).map((token) => {
        const parts = token.split(/:(?![^\[]*\])/)
        const utility = parts.pop()!.replace(/^!|!$/g, "")
        const group = utility.match(/^(bg|text|border(?:-[trblsexy])?|divide|ring(?:-offset)?|outline|from|via|to|fill|stroke|accent|caret)-/)?.[0]
        const color = group ? utility.slice(group.length).split("/")[0] : ""
        return { token, parts, group, color }
      })
      const report = (token: string) => {
        const { line } = file.getLineAndCharacterOfPosition(node.getStart(file))
        violations.push(`${line + 1}: ${token}`)
      }
      for (const { token, parts, group, color } of tokens) {
        if (!group || parts.includes("dark") || parts.includes("print")) continue
        const lightOnly = /^\[(?:color:)?#[\da-f]{3,8}\]$/i.test(color) ||
          /^(gray|slate|neutral|stone|zinc)-\d+$/.test(color) ||
          color === "white" || (group === "text-" && color === "black") ||
          (/^(amber|blue|brand|cyan|emerald|fuchsia|green|indigo|lime|orange|pink|purple|red|rose|sky|teal|violet|yellow)-\d+$/.test(color) &&
            (/^(bg|from|via|to)-$/.test(group) ? /-(50|100|200)$/.test(color) :
              group === "text-" ? /-(500|600|700|800|900|950)$/.test(color) :
              /^(border|divide)/.test(group) && /-(50|100|200|300)$/.test(color)))
        if (lightOnly && !tokens.some((other) =>
          other.group === group && other.color && other.parts.includes("dark") &&
          other.parts.filter((part) => part !== "dark").join(":") === parts.join(":")
        )) report(token)
      }
      if (
        ts.isPropertyAssignment(node.parent) &&
        /^(background(?:Color|Image)?|color|borderColor|fill|stroke)$/.test(node.parent.name.getText(file).replaceAll(/["']/g, "")) &&
        /#[\da-f]{3,8}\b/i.test(node.text)
      ) report(node.text)
    }
    ts.forEachChild(node, visit)
  }
  visit(file)
  return violations
}

test("recognizes semantic, guarded, and explicitly print-only colors", () => {
  expect(themeViolations('<div className="bg-surface hover:bg-subtle print:bg-white text-gray-900 dark:text-gray-100 hover:border-gray-200 dark:hover:border-gray-800" />')).toEqual([])
  expect(themeViolations('<div className={ok ? "hover:bg-white !text-black" : "dark:bg-black"} style={{color: "#ddd"}} />'))
    .toEqual(["1: hover:bg-white", "1: !text-black", "1: #ddd"])
})

test("every TSX screen and component uses theme tokens or matching dark variants", () => {
  const violations = readdirSync(src, { recursive: true, encoding: "utf8" })
    .filter((file) => file.endsWith(".tsx")).sort()
    .flatMap((file) => themeViolations(readFileSync(join(src, file), "utf8"))
      .map((violation) => `${relative(frontend, join(src, file))}:${violation}`))
  expect(violations.length, violations.join("\n")).toBe(0)
})

const date = "2031-03-12"
const property = { name: "P57", property_name: "P57 Hotel", business_date: date, calendar_date: date }
const fixtures: Record<string, unknown> = {
  "kamra.api.whoami": { user: "Administrator", roles: ["System Manager", "Hotel Admin", "Finance", "Front Desk"] },
  "kamra.api.my_properties": [property],
  "kamra.api.enabled_modules": [],
  "kamra.api.property_locale": { currency: "EUR", locale: "en", integrations: { payment_gateways: [] } },
  "kamra.public_api.site_info": { demo_mode: false },
  "kamra.settings.settings_schema": { modules: [], is_admin: true },
  "kamra.dashboards.property_dashboard": {
    total_rooms: 1, occupancy_pct: 100, statistics: {}, housekeeping: { room_status: {} }, finance: {},
  },
  "kamra.api.tape_chart": {
    dates: [date], position: [], conflicts: [],
    rooms: [{ name: "P57-02", room_number: "P57-02", room_type: "Standard", housekeeping_status: "Clean", bookings: [] }],
  },
  "kamra.api.folio_invoice": {
    property: { name: property.property_name, currency: "EUR" },
    folio: {
      name: "P57-FOLIO", status: "Open", guest_name: "Theme Guest", charges: [], payments: [],
      charges_total: 0, tax_total: 0, grand_total: 0, payments_total: 0, balance: 0,
    },
    stay: { reservation: null }, gst_summary: [],
  },
  "kamra.api.room_board": {
    rooms: [{ name: "P57-01", room_number: "P57-01", housekeeping_status: "Dirty", occupancy_status: "Vacant", clean_tasks: [], task: null }],
  },
  "kamra.night_audit.eod_overview": [{
    property: property.name, property_name: property.property_name, business_date: date,
    property_now: `${date}T23:59:00`, arrivals_pending: 0, departures_pending: 0, open_cashiers: 0, open_pos_bills: 0,
  }],
}

async function assertContrast(target: Locator, theme: "light" | "dark", label: string, panel = false) {
  await target.waitFor({ state: "visible" })
  const colors = await target.evaluate((element) => {
    const canvas = document.createElement("canvas")
    canvas.width = canvas.height = 1
    const ctx = canvas.getContext("2d", { willReadFrequently: true })!
    function luminance(color: string) {
      ctx.clearRect(0, 0, 1, 1)
      ctx.fillStyle = color
      ctx.fillRect(0, 0, 1, 1)
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data
      return [r, g, b].reduce((sum, channel, i) => {
        const value = channel / 255
        return sum + [0.2126, 0.7152, 0.0722][i] *
          (value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4)
      }, 0)
    }
    const layers = []
    for (let el: Element | null = element; el; el = el.parentElement) {
      layers.unshift(getComputedStyle(el).backgroundColor)
    }
    const foreground = getComputedStyle(element).color
    const canvasColor = getComputedStyle(document.body).backgroundColor
    ctx.clearRect(0, 0, 1, 1)
    for (const layer of layers) {
      ctx.fillStyle = layer
      ctx.fillRect(0, 0, 1, 1)
    }
    const [r, g, b, alpha] = ctx.getImageData(0, 0, 1, 1).data
    const background = luminance(`rgb(${r}, ${g}, ${b})`)
    const text = luminance(foreground)
    return {
      alpha, background, canvas: luminance(canvasColor),
      ratio: (Math.max(text, background) + 0.05) / (Math.min(text, background) + 0.05),
    }
  })
  expect(colors.alpha, label).toBe(255)
  expect(colors.ratio, label).toBeGreaterThanOrEqual(4.5)
  if (theme === "light") expect(colors.background, label).toBeGreaterThan(0.65)
  else expect(colors.background, label).toBeLessThan(0.15)
  if (panel) expect(colors.background, label).not.toBeCloseTo(colors.canvas, 3)
}

test("persists theme choices and checks real route colors in Chromium", async () => {
  await withFixture(async (browser, origin) => {
    const page = await fixturePage(browser, origin, "IT", { width: 1440, height: 900 }, fixtures)
    try {
      await page.addInitScript(() => {
        localStorage.setItem("kamra_property", "P57")
        localStorage.setItem("kamra-lang", "en")
      })
      await page.emulateMedia({ colorScheme: "light" })
      page.setDefaultTimeout(6000)
      const errors: string[] = []
      page.on("pageerror", (error) => errors.push(error.message))
      const main = page.locator("main")
      const settings = "/hotel/settings/appearance"
      await page.goto(settings)
      const select = main.getByRole("combobox").filter({ has: page.locator('option[value="system"]') })
      const isDark = () => page.locator("html").evaluate((html) => html.classList.contains("dark"))
      for (const theme of ["dark", "light", "system"] as const) {
        await select.selectOption(theme)
        expect(await isDark()).toBe(theme === "dark")
        await page.reload()
        expect(await select.inputValue()).toBe(theme)
        expect(await isDark()).toBe(theme === "dark")
      }
      await page.emulateMedia({ colorScheme: "dark" })
      await expect.poll(isDark).toBe(true)
      await page.reload()
      expect(await select.inputValue()).toBe("system")
      expect(await isDark()).toBe(true)
      await page.emulateMedia({ colorScheme: "light" })
      await expect.poll(isDark).toBe(false)

      for (const theme of ["light", "dark"] as const) {
        await page.goto(settings)
        await select.selectOption(theme)
        for (const width of [390, 1024, 1440]) {
          await page.setViewportSize({ width, height: 900 })
          const screens: [string, string, Locator][] = [
            ["/hotel/dashboard", "Revenue Dashboard", main.getByText("Statistics (month to date)", { exact: true })],
            ["/hotel/tape", "Tape chart", main.getByLabel("Filter by room type")],
            ["/hotel/reservations", "Reservations", main.locator("input").filter({ visible: true }).first()],
            ["/hotel/billing/P57-FOLIO", "P57 Hotel", main.getByRole("heading", { name: "P57 Hotel", exact: true })],
            ["/hotel/housekeeping-board", "Housekeeping Board", main.getByText("P57-01", { exact: true })],
            ["/hotel/night-audit", "Night Audit", main.getByText("P57 Hotel", { exact: true })],
            ["/hotel/reports", "Reports", main.locator("section a").filter({ visible: true }).first()],
            [settings, "Appearance", select],
          ]
          for (const [path, title, surface] of screens) {
            await page.goto(path)
            const label = `${theme} ${width}px ${path}`
            const heading = main.getByRole("heading", { name: title, exact: true }).first()
            if (path !== settings) await assertContrast(heading, theme, label)
            await assertContrast(surface, theme, label, true)
          }
        }
      }
      await page.emulateMedia({ media: "print" })
      await page.goto("/hotel/billing/P57-FOLIO")
      await assertContrast(main.getByRole("heading", { name: "P57 Hotel", exact: true }), "light", "dark preference, printed folio")
      expect(errors).toEqual([])
    } finally {
      await page.context().close()
    }
  })
}, 180_000)
