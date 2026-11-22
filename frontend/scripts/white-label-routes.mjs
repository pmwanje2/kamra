import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fileURLToPath } from "node:url"
import { chromium } from "playwright"
import { createServer } from "vite"
import { forbidden } from "./brand-leak-check.mjs"

export const brand = { product: "Maximus Hotel", assistant: "Eve", white_label: true, logo: null, favicon: null }
const authRoutes = ["marketplace", "developers", "setup", "finish-setup", "agents", "assistant"]
const gatedSettings = ["ai", "claude", "mcp-access", "verify"]
const root = fileURLToPath(new URL("../", import.meta.url))

export async function withFixture(run) {
  const server = await createServer({ root, server: { host: "127.0.0.1", port: 0, hmr: false, proxy: {} } })
  let browser
  try {
    await server.listen()
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM })
    const origin = `http://127.0.0.1:${server.httpServer.address().port}`
    await run(browser, origin)
  } finally {
    await browser?.close()
    await server.close()
  }
}

export async function fixturePage(browser, origin, role, viewport, replies = {}) {
  const context = await browser.newContext({ baseURL: origin, viewport })
  await context.addInitScript((brand) => { window.kamra_brand = brand }, brand)
  // No browser request can hit a real service (including Vite's dev proxy).
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url())
    if (url.origin !== origin) return route.abort()
    if (url.pathname.startsWith("/socket.io")) return route.abort()
    if (!url.pathname.startsWith("/api/")) return route.continue()
    const method = url.pathname.split("/").at(-1)
    const guest = role === "Guest"
    const defaults = {
      "kamra.api.whoami": guest ? { user: "Guest" } : { user: "staff@example.test", full_name: "Fixture Staff", roles: [role] },
      "kamra.api.my_properties": [{ name: "Fixture Hotel", property_name: "Fixture Hotel", business_date: "2031-03-12" }],
      "kamra.api.enabled_modules": [],
      "kamra.public_api.site_info": { brand, brand_name: null, brand_logo: null },
      "kamra.settings.settings_schema": { modules: [], enabled_modules: [], is_admin: role === "Hotel Admin", is_it_admin: role === "Hotel Admin" },
    }
    const message = replies[method] ?? defaults[method]
    if (message === undefined) {
      return route.fulfill({ status: 404, json: { message: "Not in the synthetic fixture." } })
    }
    return route.fulfill({ json: { message } })
  })
  const page = await context.newPage()
  page.setDefaultTimeout(6000)
  return page
}

export async function settled(page, url) {
  const response = await page.goto(url)
  assert.ok(response?.ok(), `SPA shell did not serve ${url}`)
  await page.waitForFunction(() => {
    const text = document.getElementById("root")?.innerText.trim()
    return text && !["Loading…", "Loading..."].includes(text)
  })
  // The shell can mount ahead of its lazy route. Wait out that fallback too.
  await page.getByText("Loading…", { exact: true }).first().waitFor({ state: "hidden" }).catch((e) => {
    throw new Error(`Route did not settle: ${url}`, { cause: e })
  })
}

async function sweep() {
  const source = readFileSync(new URL("../src/App.tsx", import.meta.url), "utf8")
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
              /^\/app(?:[/?#]|$)|(?:^|\/\/)github\.com(?:\/|$)/i.test(value))
            assert.deepEqual([...new Set(bad)].map((s) => s.slice(0, 150)), [])
            assert.match(values[0], /Maximus Hotel|Fixture Hotel/)
            assert.equal(await page.getByText("Something went wrong", { exact: true }).count(), 0)
            if (authRoutes.includes(route.slice(1)) || gatedSettings.some((p) => route === `/settings/${p}`)) {
              await page.getByText("Not available here", { exact: true }).waitFor()
            }
            assert.equal(await page.locator('a[href^="/marketplace"], a[href^="/developers"], a[href="/setup"], a[href="/finish-setup"], a[href="/agents"], a[href="/assistant"]').count(), 0)
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
    ]))
  })
  assert.equal(failures.length, 0, failures.join("\n"))
  console.log(`White-label sweep: ${routes.size * 4 + 12} route visits`)
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) await sweep()
