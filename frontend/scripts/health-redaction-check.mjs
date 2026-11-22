// Render the real screen and its fetch lifecycle at /hotel/health. Every
// request is fulfilled/blocked in the test browser; no server or bench runs.
import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { test } from "node:test"
import { chromium } from "playwright"
import { build } from "vite"

const origin = "http://hotel.test"
const hotelBrand = { white_label: true, product: "Maximus Hotel", assistant: "Eve" }
const legacyBrand = { white_label: false, product: "Independent Hotel", assistant: "Assistant" }
const entry = fileURLToPath(new URL("../health-test-entry.jsx", import.meta.url))
const [{ output }] = [].concat(await build({
  configFile: false,
  root: fileURLToPath(new URL("../", import.meta.url)),
  logLevel: "error",
  esbuild: { jsx: "automatic" },
  plugins: [{
    name: "health-test-entry",
    resolveId(id) { if (id === entry) return id },
    load(id) {
      if (id === entry) return `
        import { createRoot } from "react-dom/client"
        import SystemHealth from "./src/screens/SystemHealth"
        createRoot(document.querySelector("main")).render(<SystemHealth />)
      `
    },
  }],
  build: {
    write: false,
    rollupOptions: { input: entry, output: { inlineDynamicImports: true } },
  },
}))
const script = output.find((chunk) => chunk.type === "chunk" && chunk.isEntry).code

const redacted = {
  product: "Maximus Hotel", overall: "attention", checked_at: "2026-11-22T08:00:00+03:00",
  properties: [{
    property: "North Inn", status: "attention", business_date: "2026-11-20", calendar_date: "2026-11-22",
    night_audit: {
      last_business_date: "2026-11-19", status: "Completed", completed_at: "2026-11-21T02:05:00+03:00",
      next_due: "2026-11-23T02:00:00+03:00",
    },
  }],
  checks: [
    { id: "service", title: "Service", status: "passed", detail: "Responding." },
    { id: "background", title: "Background jobs", status: "passed", detail: "Background jobs are enabled." },
    { id: "night_audit", title: "Night audit", status: "attention", detail: "End of Day is pending." },
    { id: "property_clock", title: "Property clock", status: "passed", detail: "Matches the hotel time zone." },
    { id: "channels", title: "Channel connections", status: "attention", detail: "Channel updates need attention." },
  ],
}
const legacy = {
  overall: "passed", summary: { passed: 1, attention: 0, failed: 0, info: 0 },
  installed: { kamra: "2.8.0", frappe: "16.25.0", site: "private-host.test" },
  latest: { ok: true, tag: "v9.9.9", name: "New release", url: "https://github.com/example", published_at: null },
  checks: [{ id: "apps", title: "Installed apps", status: "passed", detail: "ERPNext is installed.", link: "/app/system-settings" }],
  upgrade: {
    docs: "https://kamrapms.com/docs", releases: "https://github.com/example", docker_latest: "kamra:latest",
    docker_nightly: "kamra:nightly", bench: "bench update", note: "Upgrade Kamra",
  },
}
const forbidden = /\b(?:kamra|frappe|erpnext|hrms|github|installed|release notes|docker|bench|disk|python|upgrade)\b|kamrapms|private-host|\bv?\d+\.\d+\.\d+\b|\/app\//i

const browser = await chromium.launch({ executablePath: process.env.CHROMIUM || undefined })
try {
  await test("System Health redaction", async (t) => {
    async function open(payload, brand, inspect) {
      const context = await browser.newContext()
      const requests = []
      const crashes = []
      const unexpected = []
      try {
        const page = await context.newPage()
        page.on("pageerror", (error) => crashes.push(error.message))
        await page.route("**/*", async (route) => {
          const url = route.request().url()
          if (url === `${origin}/hotel/health`) {
            return route.fulfill({ contentType: "text/html", body: `<main></main><script>window.kamra_brand = ${JSON.stringify(brand ?? null)}</script><script type="module" src="/screen.js"></script>` })
          }
          if (url === `${origin}/screen.js`) return route.fulfill({ contentType: "text/javascript", body: script })
          if (url === `${origin}/api/method/kamra.health.system_health`) {
            requests.push(route.request().postDataJSON())
            return payload instanceof Error
              ? route.fulfill({ status: 500, json: { message: payload.message } })
              : route.fulfill({ json: { message: payload } })
          }
          unexpected.push(url)
          return route.abort()
        })
        await Promise.all([
          page.waitForResponse(`${origin}/api/method/kamra.health.system_health`),
          page.goto(`${origin}/hotel/health`),
        ])
        await inspect(page, requests, crashes)
        assert.deepEqual(crashes, [])
        assert.deepEqual(unexpected, [])
      } finally {
        await context.close()
      }
    }

    async function assertRedacted(page) {
      const screen = page.locator("main")
      assert.doesNotMatch(await screen.innerText(), forbidden)
      assert.equal(await screen.locator("a[href]").count(), 0, "health must not expose diagnostic links")
      const attributes = await screen.locator("[title], [aria-label], [alt]").evaluateAll((nodes) =>
        nodes.flatMap((node) => ["title", "aria-label", "alt"].map((key) => node.getAttribute(key) ?? "")).join(" "),
      )
      assert.doesNotMatch(attributes, forbidden)
    }

    await t.test("renders operational data with no branded or technical details", async () => {
      await open(redacted, hotelBrand, async (page, requests, crashes) => {
        await page.waitForFunction(() => !document.querySelector("main").textContent.includes("Loading…"))
        assert.deepEqual(crashes, [], "redacted response must not crash the health screen")
        await page.getByRole("heading", { name: "System Health" }).waitFor({ timeout: 3000 })
        await assertRedacted(page)
        for (const text of ["North Inn", "2026-11-20", "2026-11-22", "Completed", "2026-11-23", "Background jobs", "Channel connections"]) {
          assert.match(await page.locator("main").innerText(), new RegExp(text))
        }
        await Promise.all([
          page.waitForResponse(`${origin}/api/method/kamra.health.system_health`),
          page.getByRole("button", { name: "Refresh" }).click(),
        ])
        assert.deepEqual(requests, [{ refresh: 0 }, { refresh: 1 }])
        await assertRedacted(page)
      })
    })

    await t.test("recognizes the redacted contract without the K1 boot flag", async () => {
      await open(redacted, null, async (page) => {
        await page.getByText("North Inn", { exact: true }).waitFor({ timeout: 3000 })
        await assertRedacted(page)
      })
    })

    await t.test("refuses a legacy payload on a white-labeled boot", async () => {
      await open(legacy, hotelBrand, async (page) => {
        await page.getByText("Could not load system health.").waitFor({ timeout: 3000 })
        await assertRedacted(page)
      })
    })

    await t.test("shows a neutral error rather than raw backend details", async () => {
      await open(new Error("Kamra: Frappe ERPNext private-host.test"), hotelBrand, async (page) => {
        await page.getByText("Could not load system health.").waitFor({ timeout: 3000 })
        await assertRedacted(page)
      })
    })

    await t.test("retains diagnostics when the flag is off", async () => {
      await open(legacy, legacyBrand, async (page) => {
        await page.getByText("private-host.test", { exact: true }).waitFor({ timeout: 3000 })
        assert.equal(await page.getByRole("link", { name: "Release notes" }).getAttribute("href"), legacy.latest.url)
        assert.match(await page.locator("main").innerText(), /Independent Hotel 2\.8\.0/)
      })
    })
  })
} finally {
  await browser.close()
}
