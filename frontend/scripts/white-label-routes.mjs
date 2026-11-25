import assert from "node:assert/strict"
import { mkdtemp, readFile, rm } from "node:fs/promises"
import { createServer } from "node:http"
import { tmpdir } from "node:os"
import { extname, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"
import { chromium } from "playwright"
import { build } from "vite"
import { forbidden } from "./brand-leak-check.mjs"

export const brand = { product: "Maximus Hotel", assistant: "Eve", white_label: true, logo: null, favicon: null }
const authRoutes = ["marketplace", "developers", "setup", "finish-setup", "agents", "assistant"]
const gatedSettings = ["ai", "claude", "mcp-access", "verify"]
const root = fileURLToPath(new URL("../", import.meta.url))
const assetBase = "/assets/kamra/frontend/"
const mime = { ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".png": "image/png" }

export async function withFixture(run) {
  const output = await mkdtemp(resolve(tmpdir(), "hotel-route-fixture-"))
  let server
  let browser
  try {
    // Test the production code, not the dev server (which can reload active
    // pages when lazily loaded screens introduce dependencies).
    await build({ root, logLevel: "error", build: { outDir: output } })
    const shell = (await readFile(resolve(output, "index.html"), "utf8"))
      .replace(/<title>.*?<\/title>/, `<title>${brand.product}</title>`)
    server = createServer(async (request, response) => {
      const { pathname } = new URL(request.url, "http://127.0.0.1")
      if (pathname.startsWith(assetBase)) {
        const file = resolve(output, pathname.slice(assetBase.length))
        if (file.startsWith(output + sep)) {
          const body = await readFile(file).catch(() => null)
          if (body) return response.writeHead(200, { "content-type": mime[extname(file)] ?? "application/octet-stream" }).end(body)
        }
      } else if (/^\/(?:kamra|hotel)(?:\/|$)/.test(pathname)) {
        return response.writeHead(200, { "content-type": "text/html; charset=utf-8" }).end(shell)
      }
      response.writeHead(404).end()
    })
    await new Promise((ready) => server.listen(0, "127.0.0.1", ready))
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM })
    const origin = `http://127.0.0.1:${server.address().port}`
    await run(browser, origin)
  } finally {
    await browser?.close()
    if (server) await new Promise((closed) => server.close(closed))
    await rm(output, { recursive: true })
  }
}

export async function fixturePage(browser, origin, role, viewport, replies = {}) {
  const context = await browser.newContext({ baseURL: origin, viewport })
  const guest = role === "Guest"
  const properties = replies["kamra.api.my_properties"] ?? [
    { name: "Fixture Hotel", property_name: "Fixture Hotel", business_date: "2031-03-12" },
  ]
  // Start with the synthetic property selected so the shell does not remount
  // the route during a visibility assertion while my_properties resolves.
  await context.addInitScript(({ brand, property }) => {
    window.kamra_brand = brand
    if (property) localStorage.setItem("kamra_property", property)
  }, { brand, property: guest ? null : properties[0]?.name })
  // No browser request can hit a real service (including Vite's dev proxy).
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) return route.abort()
    if (url.pathname.startsWith("/socket.io")) return route.abort()
    if (!url.pathname.startsWith("/api/")) return route.continue()
    const method = url.pathname.split("/").at(-1)
    const defaults = {
      "kamra.api.whoami": guest ? { user: "Guest" } : { user: "staff@example.test", full_name: "Fixture Staff", roles: role === "IT" ? ["Hotel Admin", "System Manager"] : [role] },
      "kamra.api.my_properties": properties,
      "kamra.api.enabled_modules": [],
      "kamra.public_api.site_info": { brand, brand_name: null, brand_logo: null },
      "kamra.api.hk_queue": { tasks: [], rooms: [] },
      "kamra.health.system_health": {
        product: brand.product, overall: "passed", checked_at: "2031-03-12T08:00:00+03:00",
        properties: [{ property: "Health Fixture Property", status: "passed", business_date: "2031-03-12",
          calendar_date: "2031-03-12", night_audit: { last_business_date: "2031-03-11", status: "Completed",
            completed_at: "2031-03-12T02:00:00+03:00", next_due: null } }],
        checks: [{ id: "service", title: "Service", status: "passed", detail: "Responding." }],
      },
      "kamra.public_api.catalog_index": { mode: "hotel", property: "Fixture Hotel" },
      "kamra.public_api.showcase": { property: { name: "Fixture Hotel", property_name: "Fixture Hotel", city: "Fixture City",
        checkin_time: "14:00", checkout_time: "11:00", faqs: [], amenities: [] },
        room_types: [], meal_plans: [], experiences: [] },
      "kamra.settings.settings_schema": { modules: [], enabled_modules: [], is_admin: role === "Hotel Admin" || role === "IT", is_it_admin: role === "IT" },
    }
    const message = replies[method] ?? defaults[method]
    if (message === undefined) {
      return route.fulfill({ status: 404, json: { message: "Not in the synthetic fixture." } })
    }
    return route.fulfill({ json: { message } })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(20000)
  return page
}

export async function settled(page, url) {
  const path = url.startsWith("/hotel/") ? url : `/kamra${url}`
  const response = await page.goto(path, { waitUntil: "domcontentloaded" })
  assert.ok(response?.ok(), `SPA shell did not serve ${url}`)
  await page.waitForFunction(() => {
    const text = document.getElementById("root")?.innerText.trim()
    return text && !["Loading…", "Loading..."].includes(text)
  }, undefined, { polling: 50 })
  // Screens may intentionally retain their own loading/empty states in the
  // synthetic fixture. The condition above waits for the app/route shell.
}

async function sweep() {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8")
  const variants = { checkin: "2031-03-12", checkout: "2031-03-13", adults: "2", children: "0", kind: "quote" }
  const routes = new Set(["/", "/unknown-test-route", ...gatedSettings.map((p) => `/settings/${p}`)])
  const declared = [...source.matchAll(/<Route\b\s+path="([^"]+)"/g)]
  assert.ok(declared.length > 75, "Route discovery must cover App.tsx")
  for (const [, pattern] of declared) {
    if (pattern === "*") continue
    routes.add("/" + pattern.replace(/:(\w+)\??/g, (_, key) => variants[key] ?? "fixture")
      .replace(/\/\*$/, "/fixture"))
  }
  const failures = []
  await withFixture(async (browser, origin) => {
    async function visit(role, viewport, paths) {
      const page = await fixturePage(browser, origin, role, viewport)
      try {
        for (const route of paths) {
          try {
            await settled(page, route)
            if (route === "/health") {
              await page.getByText("Health Fixture Property", { exact: true }).waitFor()
              await page.getByText("Passed", { exact: true }).first().waitFor()
            }
            if (/^\/book(?:\/|$)/.test(route)) await page.getByRole("heading", { name: "Fixture Hotel", exact: true }).first().waitFor()
            const values = await page.evaluate(() => {
              const values = [document.title, document.body.innerText]
              const attributes = ["alt", "title", "aria-label", "placeholder", "href", "src"]
              for (const node of document.querySelectorAll(attributes.map((a) => `[${a}]`).join(","))) {
                for (const a of attributes) {
                  const value = node.getAttribute(a)
                  if (!value) continue
                  // In-app route and asset namespaces are machine identifiers;
                  // a branded logo filename is still forbidden.
                  values.push(["href", "src"].includes(a) ?
                    value.replace(/^\/(?:assets\/)?kamra(?=\/|$)/, "") : value)
                }
              }
              return values
            })
            const bad = values.filter((value) => forbidden.test(value) ||
              /^(?:https?:\/\/[^/]+)?\/app(?:[/?#]|$)|(?:^|\/\/)github\.com(?:\/|$)/i.test(value))
            assert.deepEqual([...new Set(bad)].map((s) => s.slice(0, 150)), [])
            assert.match(values[0], /Maximus Hotel|Fixture Hotel|Places to stay/)
            assert.equal(await page.getByText("This page hit a problem", { exact: true }).count(), 0, "Screen hit the React error boundary")
            if (authRoutes.includes(route.slice(1)) || gatedSettings.some((p) => route === `/settings/${p}`)) {
              await page.getByText("Not available here", { exact: true }).waitFor()
            }
            assert.equal(await page.locator(authRoutes.map((route) => `a[href="/kamra/${route}"]`).join(",")).count(), 0)
          } catch (error) {
            failures.push(`${role} ${viewport.width}px ${route}: ${error.message.slice(0, 360)}`)
            if (process.env.KAMRA_ROUTE_FAIL_FAST === "1") throw new Error(failures.at(-1))
          }
        }
      } finally {
        await page.context().close()
      }
    }
    await Promise.all([ { width: 1280, height: 800 }, { width: 412, height: 915 } ].flatMap((viewport) => [
      visit("Hotel Admin", viewport, routes), visit("Front Desk", viewport, routes),
      visit("Guest", viewport, ["/login", "/update-password", "/hk", "/book", "/stay/fixture", "/checkin/fixture"]),
      visit("IT", viewport, ["/settings/appearance", "/apps", "/marketplace", "/developers", "/settings/ai"]),
    ]))
  })
  assert.equal(failures.length, 0, failures.join("\n"))
  console.log(`White-label sweep: ${routes.size * 4 + 22} route visits`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await sweep()
