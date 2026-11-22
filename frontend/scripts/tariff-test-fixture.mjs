import { chromium } from "playwright"
import { createServer } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import { fileURLToPath } from "node:url"

export const property = "TEST K3"
export const tariff = { currency: "USD", nightly: 130, property_currency: "UGX", fx_rate: 3650, fx_date: "2026-11-21" }
const roomType = { name: "STANDARD", room_type_name: "Standard Room", room_type_code: "STD", base_price: 410619, adults_capacity: 2, children_capacity: 0, tax_percent: 0 }
const plan = { name: "CONTRACT", rate_plan_name: "Corporate tariff", is_default: 1 }

export async function withDesk(run, override = {}) {
  const server = await createServer({
    configFile: false,
    plugins: [react(), tailwindcss()],
    root: fileURLToPath(new URL("../", import.meta.url)),
    server: { host: "127.0.0.1", port: 0, proxy: {}, ws: false },
  })
  let browser
  try {
    await server.listen()
    const baseURL = server.resolvedUrls.local[0]
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM })
    const page = await browser.newPage({ baseURL })
    page.setDefaultTimeout(6000)
    const requests = []
    page.on("console", msg => {
      if (msg.type() === "error" && msg.text().includes("screen crashed")) console.error(msg.text().split("\n").slice(0, 3).join("\n"))
    })
    await page.addInitScript(p => localStorage.setItem("kamra_property", p), property)
    await page.route("**/api/**", async route => {
      const url = new URL(route.request().url())
      const method = url.pathname.split("/").at(-1)
      const args = route.request().postDataJSON() || Object.fromEntries(url.searchParams)
      requests.push({ method, args })
      const fixed = {
        "kamra.api.whoami": { user: "test@example.invalid", full_name: "Desk staff", roles: ["Hotel Admin", "Front Desk", "Finance"] },
        "kamra.api.my_properties": [{ name: property, property_name: property, business_date: "2026-11-21", calendar_date: "2026-11-21" }],
        "kamra.api.enabled_modules": [],
        "kamra.public_api.site_info": {},
        "kamra.api.property_locale": { locale: "en-UG", currency_symbol: "Sh", currency: "UGX", payment_modes: ["Cash"], rates_include_tax: true, tax_basis: { label: "VAT 18%", rate: 18, inclusive: 1 } },
        "kamra.api.booking_options": { room_types: [roomType], rate_plans: [plan], meal_plans: [], companies: [], travel_agents: [], experiences: [], property: { country: "Uganda" }, ota_channels: [], can_set_rate: true, rate_plan_required: true, default_plan: plan.name },
        "kamra.api.available_rooms": [{ name: "ROOM-101", room_number: "101" }],
        "kamra.cashier.my_till": { open: true, property, terminal_links: [] },
        "kamra.api.linked_records": { folios: [], reservations: [] },
        "kamra.api.correct_charged_nights": { nights: [] },
        "kamra.ledger.list_exchange_rates": { rates: [], business_date: "2026-11-21" },
        "frappe.client.get_list": args.doctype === "Room Type" ? [roomType] : [],
        "frappe.client.get": roomType,
        "kamra.rates.room_type_tariffs": { tariffs: [{ rate_plan: plan.name, label: plan.rate_plan_name, currency: "USD", room: 130, single: 100, extra: 20, approval_state: "Approved", valid_from: "2026-11-01", fx_rate: 3650, fx_date: "2026-11-21" }], all_active_plans_tariff: true },
        "kamra.api.folio_invoice": {
          folio: { name: "FOLIO-K3", property, status: "Open", folio_type: "Guest", balance: 949000, grand_total: 949000, charges: [], payments: [] },
          property: { currency: "UGX", name: property }, guest: { name: "Contract Guest" },
          stay: { reservation: "RES-K3", check_in: "2026-11-21", check_out: "2026-11-23", nights: 2, tariff, agreed_rate: 474500 },
          lines: [], summary_by_head: [], tax_summary: [], gst_summary: [], bill_to: null,
        },
      }
      const night = { date: args.check_in_date, bar: 410619, after_season: 410619, rate: 410619 }
      const quote = { currency: "UGX", rate_plan: args.rate_plan, room_total: 410619, amount_after_tax: 410619, tax_percent: 0, tax_amount: 0, discount: 0, meal_total: 0, nights: 1, nightly: [night], totals: { total: 410619 } }
      const amount = { room_total: 474500, amount_after_tax: 474500, tariff, agreed_rate: 474500 }
      const dynamic = {
        "kamra.api.get_quote": { ...quote, ...(args.rate_plan === plan.name ? amount : {}) },
        "kamra.rates.rate_options": {
          nights: args.rate_plan === plan.name ? [{ ...night, bar: 474500, rate: 474500 }] : [night],
          plain: quote, plans: [{ ...plan, label: plan.rate_plan_name, available: true, ...amount }],
          default_plan: plan.name, rate_plan_required: true, tax_inclusive: true,
        },
      }
      const response = override[method] ?? dynamic[method] ?? fixed[method] ?? []
      const message = typeof response === "function" ? response(args, url) : response
      if (url.pathname.startsWith("/api/resource/")) {
        const path = decodeURIComponent(url.pathname)
        return route.fulfill({ json: { data: path.endsWith("/Room Type") ? [roomType] : path.endsWith("/Room Type/STANDARD") ? roomType : [] } })
      }
      await route.fulfill({ json: { message } })
    })
    await run(page, requests)
  } finally {
    await browser?.close()
    await server.close()
  }
}
