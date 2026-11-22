// Real Walk-in, Folio, and Deposit screens; all RPCs intercepted in Chromium.
import assert from "node:assert/strict"
import { fileURLToPath } from "node:url"
import { chromium } from "playwright"
import { createServer, transformWithEsbuild } from "vite"

const businessDate = "2031-03-11"
const home = 474501
const deposit = { pct: 100, expected: home, paid: 0, due: home, can_take: true }
const locale = {
  currency: "UGX", currency_symbol: "UGX ", locale: "en-UG", default_nationality: "Ugandan",
  payment_modes: ["Cash", "Card", "Bank Transfer", "Mobile Money"],
  tender_modes: [
    { mode: "Cash", currencies: ["UGX", "USD"] },
    { mode: "Card", currencies: ["UGX"] },
    { mode: "Mobile Money", currencies: ["UGX"] },
  ],
  fx_currencies: ["USD", "EUR"],
}
const boot = `
import React from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Routes, Route } from "react-router-dom"
import { AuthProvider } from "/src/lib/auth.tsx"
import { loadLocale } from "/src/lib/money.ts"
import { BookingDialog } from "/src/components/BookingDialog.tsx"
import DepositPanel from "/src/components/DepositPanel.tsx"
import FolioView from "/src/screens/FolioView.tsx"
import "/src/index.css"
await loadLocale()
const screen = new URLSearchParams(location.search).get("screen")
createRoot(document.getElementById("root")).render(
  <MemoryRouter initialEntries={["/billing/F-1"]}><AuthProvider>
    {screen === "walk-in" ? <BookingDialog initial={{walkIn:true, room_type:"STD", guest_name:"Test Guest", phone:"+256772123456"}} onClose={()=>{}} onBooked={()=>{}}/> :
     screen === "deposit" ? <DepositPanel reservation="R-1" deposit={${JSON.stringify(deposit)}} onChanged={()=>{}}/> :
     <Routes><Route path="/billing/:name" element={<FolioView/>}/></Routes>}
  </AuthProvider></MemoryRouter>
)
`
const server = await createServer({
  root: fileURLToPath(new URL("../", import.meta.url)),
  server: { host: "127.0.0.1", port: 0, proxy: {}, hmr: false },
  plugins: [{
    name: "tender-test-fixture",
    resolveId(id) { if (id === "virtual:tender-test.tsx") return "\0" + id },
    async load(id) {
      if (id === "\0virtual:tender-test.tsx") {
        return (await transformWithEsbuild(boot, "tender-test.tsx", { loader: "tsx", jsx: "transform" })).code
      }
    },
    configureServer(vite) {
      vite.middlewares.use("/__tender", async (req, res) => {
        const html = '<!doctype html><meta name="viewport" content="width=device-width, initial-scale=1"><div id="root"></div><script type="module">import "virtual:tender-test.tsx"</script>'
        res.setHeader("Content-Type", "text/html")
        res.end(await vite.transformIndexHtml(req.originalUrl, html))
      })
    },
  }],
})
const failures = []
let browser
try {
  await server.listen()
  const origin = server.resolvedUrls.local[0]
  browser = await chromium.launch(process.env.CHROMIUM ?
    { executablePath: process.env.CHROMIUM } : { channel: "chromium" })
  for (const [width, hasToday] of [[1280, true], [412, true], [412, false]]) {
    for (const screen of ["walk-in", "folio", "deposit"]) {
      const page = await browser.newPage({ viewport: { width, height: 915 } })
      page.setDefaultTimeout(5000)
      const errors = []
      page.on("pageerror", (error) => errors.push(error.message))
      const quote = { nights: 1, nightly: [], room_total: home, meal_total: 0, discount: 0,
        amount_before_tax: home, amount_after_tax: home, tax_amount: 0, tax_percent: 0 }
      const walkIn = screen === "walk-in"
      const settlementRate = walkIn ? 4032.258064516 : 3650
      if (walkIn) Object.assign(quote, { tariff: { currency:"USD", nightly:130, property_currency:"UGX",
        fx_rate:settlementRate, fx_date:businessDate }, agreed_rate:524194, room_total:524194, amount_after_tax:524194 })
      await page.route("**/api/method/*", (route) => {
        const method = new URL(route.request().url()).pathname.split("/").at(-1)
        const payload = route.request().postDataJSON() ?? {}
        const fixtures = {
          property_locale: width === 1280 ? locale : { ...locale, tender_modes: [
            { mode:"Cash", currencies:["UGX"] }, { mode:"Foreign Cash", currencies:["USD"] },
            ...locale.tender_modes.slice(1),
          ] },
          whoami: { user: "test@example.com", roles: ["Front Desk"] },
          booking_options: { room_types: [{name:"STD", room_type_name:"Standard", base_price:home,
            adults_capacity:2, children_capacity:1}], meal_plans:[], companies:[], rate_plans:[],
            travel_agents:[], experiences:[], property:{country:"Uganda"} },
          available_rooms: [{ name:"ROOM-1", room_number:"101", housekeeping_status:"Clean" }],
          get_quote: quote, rate_options: { nights: [], plans: [], plain: quote, voucher: null },
          guest_alert: {}, my_till: { required: false, open: true },
          folio_invoice: {
            folio: { name:"F-1", property:"P", status:"Open", guest_name:"Test Guest", charges:[],
              payments:[{ name:"PAY-1", posting_date:businessDate, mode:"Foreign Cash", currency:"USD",
                fx_amount:1, exchange_rate:3650, amount:3650 }],
              charges_total:home + 3650, tax_total:0, grand_total:home + 3650, payments_total:3650, balance:home },
            stay: { reservation:null, check_in:businessDate, check_out:"2031-03-12" },
            property: { name:"P", currency:"UGX" }, gst_summary:[],
          },
          list_exchange_rates: { business_date:businessDate, rates:[
            { currency:"USD", rate_date:hasToday ? businessDate : "2031-03-10", buy_rate:settlementRate * 0.98, settlement_rate:settlementRate },
            { currency:"EUR", rate_date:businessDate, buy_rate:4000 },
          ] },
          payment_rate: { currency:"USD", rate:settlementRate, rate_date:businessDate, home_currency:"UGX",
            home_amount: Math.round(Number(payload.amount) * settlementRate), quote_id:"test-quote" },
          add_folio_payment: {},
          payment_correction_options: { actions:[{ id:"reclassify_tender", label:"Correct payment mode",
            requires:[], reason_codes:[{ name:"REV-ACCOUNT", description:"Wrong tender" }] }],
            blocked:[], supervisors:[], refund_cap:0 },
          record_advance: { deposit },
          walk_in: { reservation:"R-1", room:"ROOM-1", room_number:"101", checked_in:1 },
        }
        return route.fulfill({ json: { message: fixtures[method.split(".").at(-1)] ?? [] } })
      })
      try {
        await page.goto(`${origin}__tender?screen=${screen}`)
        if (screen === "deposit") await page.getByRole("button", { name:"Record payment", exact:true }).click()
        await page.getByText("Tender currency", { exact:true }).first().waitFor()
        assert.equal(await page.locator('input[aria-label="Tender currency"]').count(), 0, "Tender currency must not be typed")
        const radio = (name) => page.getByRole("button", { name, exact:true })
        const ugx = radio("UGX"), usd = radio("USD")
        await ugx.waitFor()
        await radio("Card").click()
        assert.ok(await usd.isDisabled(), "Card may only take UGX")
        await radio("Mobile Money").click()
        assert.ok(await usd.isDisabled(), "Mobile Money may only take UGX")
        assert.equal(await radio("Bank Transfer").count(), 0, "Unmapped mode offered")
        await radio("Foreign Cash").click()
        assert.equal(await radio("EUR").count(), 0, "Published rate without a mapped account must not be offered")
        if (!hasToday) {
          assert.ok(await usd.isDisabled(), "A stale rate must not enable USD")
          assert.ok(await page.getByText(/No published rate/).first().isVisible())
        } else {
          assert.equal(await usd.getAttribute("aria-pressed"), "true")
          const amount = page.locator('input[type="number"]').filter({ visible:true })
          const rateDate = page.getByText("Effective rate date", { exact:true })
          await rateDate.waitFor()
          assert.equal(await rateDate.locator("..").locator("dd").textContent(), "11 Mar 2031")
          assert.equal(await radio("Foreign Cash").getAttribute("aria-pressed"), "true")
          assert.equal(await page.getByText("Amount tendered", { exact:true }).locator("..").locator("dd").textContent(), walkIn ? "USD 130.00" : "USD 130.01")
          assert.ok(await page.getByText(walkIn ? "1 USD = 4,032.26 UGX" : "1 USD = 3,650 UGX", { exact:true }).isVisible())
          assert.ok(await amount.count(), "The tender amount must remain editable")
          if (walkIn) {
            await page.getByText("Settled in full", {exact:true}).waitFor()
            const input = page.getByRole("spinbutton", {name:"Amount collected"})
            assert.equal(await input.inputValue(), "130")
            await input.fill("100")
            await page.getByText("Remaining balance", {exact:true}).waitFor()
            await radio("Full amount").click()
            await page.getByText("Settled in full", {exact:true}).waitFor()
          }
          const submit = walkIn ? /Collect USD 130.00 and continue/ :
            screen === "folio" ? /^Record$/ : /^Record USD 130.01$/
          const expectedMethod = { "walk-in":"walk_in", folio:"add_folio_payment", deposit:"record_advance" }[screen]
          const [request] = await Promise.all([
            page.waitForRequest(r => r.url().endsWith(`.${expectedMethod}`), { timeout:5000 }),
            page.getByRole("button", { name:submit }).click(),
          ])
          const body = request.postDataJSON()
          assert.equal(body.currency, "USD")
          assert.equal(body.quote_id, "test-quote")
          assert.ok(["Cash", "Foreign Cash"].includes(body.mode ?? body.payment_mode))
        }
        if (screen === "folio") {
          const correction = page.getByRole("listitem").filter({ hasText: "USD 1.00 @ 3,650" })
          await correction.getByRole("button", { name:"Correct…" }).click()
          const picker = correction.getByRole("group", { name:"Payment mode" })
          assert.equal(await picker.getByRole("button", { name:"Bank Transfer" }).count(), 0)
          await picker.getByRole("button", { name:"Card" }).click()
          assert.ok(await correction.getByRole("button", { name:"UGX", exact:true }).isDisabled())
          assert.equal(await correction.getByRole("button", { name:"USD", exact:true }).count(), 0)
        }
        assert.ok(await page.evaluate(() => document.scrollingElement.scrollWidth <= innerWidth), "Page overflow")
        console.log(`PASS ${width}px ${screen} (${hasToday ? "published" : "stale"} rate)`)
      } catch (error) {
        failures.push(`${width}px ${screen} (hasToday=${hasToday}): ${error.message}\n${errors.join("\n")}`)
      } finally {
        await page.close()
      }
    }
  }
} finally {
  await browser?.close()
  await server.close()
}
assert.deepEqual(failures, [])
