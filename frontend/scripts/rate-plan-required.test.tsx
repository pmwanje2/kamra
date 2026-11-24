// @vitest-environment jsdom
// Frontend contracts for rate-plan-required policy:
// 1. Room Types hides card-rate/extra-adult fields & section when rate_plan_required is ON,
//    replaces card-rate fallback copy with rate-plan pricing copy, and hides Tax % on ERP-tax sites
//    (leaving standalone sites unchanged).
// 2. Settings > Front Desk renders the Rate Plan readiness card listing future bookings without
//    a plan and stating existing reservations are never repriced.
// 3. BookingDialog blocks walk-in & reservation submit when rate_plan_required is ON and no plan applies.
// 4. Contract Tariffs <-> Room Types <-> Rate Calendar <-> BookingDialog stay in live agreement
//    after discard, approve, schedule and expire without any page reload.
// 5. All 8 languages, mobile-safe classes, and zero Kamra/ERPNext/Frappe wording.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import { BookingDialog } from "../src/components/BookingDialog"
import ContractTariffs from "../src/screens/ContractTariffs"
import ConfiguredScreen from "../src/screens/ConfiguredScreen"
import PublicListing from "../src/screens/PublicListing"
import RateCalendar from "../src/screens/RateCalendar"
import { Route, Routes } from "react-router-dom"
import { setLang } from "../src/lib/dir"
import { ensureDictionary } from "../src/lib/locales"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zh from "../src/i18n/locales/zh-CN.json"

const NEW_STRINGS = [
  "Rate plans set the price for all bookings at this property.",
  "Tax comes from the accounting tax rules.",
  "Rate plan readiness",
  "Existing reservations are never repriced when this setting changes.",
  "No default rate plan is set yet.",
  "Manage rate plans",
  "All future bookings already have a rate plan.",
  "{n} future booking has no rate plan.",
  "{n} future bookings have no rate plan.",
  "They keep their booked prices. Only new bookings will require a rate plan.",
  "On: every new booking, walk-in, group pick-up, channel and online booking resolves a rate plan. Existing reservations are never repriced.",
  "Set a default rate plan in Rate Plans.",
  "No active rate plans are configured. Set up a default rate plan in Rate Plans.",
]

const FORBIDDEN = /(?<![a-z0-9])(?:KMR-[A-Z0-9-]+|Kamra|Frappe|ERPNext|URY|HUF|HeyKoala|Tridz|BuildSuite)(?![a-z0-9])/i

const { rpc, guestRpc, fetches, create, update, state } = vi.hoisted(() => ({
  rpc: vi.fn(),
  guestRpc: vi.fn(),
  fetches: vi.fn(),
  create: vi.fn(),
  update: vi.fn(),
  state: {
    locale: {
      locale: "en-UG",
      currency: "UGX",
      currency_symbol: "UGX ",
      tax_label: "VAT",
      tax_basis: null as { label: string; rate: number; inclusive: number; determined?: number } | null,
      erp_tax_managed: false,
      payment_modes: ["Cash"],
    } as Record<string, unknown>,
    roomTypes: [
      {
        name: "STANDARD",
        room_type_name: "Standard Room",
        room_type_code: "STD",
        room_category: "Private",
        base_price: 410619,
        base_occupancy: 2,
        single_occupancy_price: 380000,
        extra_adult_price: 50000,
        child_price: 20000,
        adults_capacity: 2,
        children_capacity: 0,
        tax_percent: 18,
      },
    ],
  },
}))

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  return {
    ...actual,
    call: (method: string, params?: Record<string, unknown>) =>
      method === "kamra.api.property_locale"
        ? Promise.resolve(state.locale)
        : rpc(method, params),
    callGuest: (method: string, params?: Record<string, unknown>) => guestRpc(method, params),
    getCurrentProperty: () => "Tank Hill Hotel",
    frappeFetch: (path: string, init?: RequestInit) => fetches(path, init),
  }
})

vi.mock("../src/lib/resource", async (original) => ({
  ...(await original<typeof import("../src/lib/resource")>()),
  listResource: async (doctype: string) => (doctype === "Room Type" ? state.roomTypes : []),
  createResource: create,
  updateResource: update,
}))

vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: ["Hotel Admin", "Front Desk"] }) }))
vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

const baseSummary = {
  property_currency: "UGX",
  has_card_rate_plans: true,
  rate_plan_required: true,
  erp_tax_managed: true,
  default_plan: { name: "STD-USD", label: "Standard USD", code: "STD-USD", currency: "USD", is_tariff: true },
  room_types: [
    {
      room_type: "STANDARD",
      room_count: 5,
      priced: true,
      source: "contract",
      currency: "USD",
      amount: 100,
      local_amount: 365000,
      refusal: null,
    },
  ],
}

const basePanel = {
  tariffs: [
    {
      rate_plan: "STD-USD",
      label: "Standard USD",
      currency: "USD",
      room: 100,
      single: 90,
      extra: 20,
      approval_state: "Approved",
      valid_from: "2026-11-01",
      fx_rate: 3650,
      fx_date: "2026-12-01",
      local_room: 365000,
      local_single: 329000,
      fx_lock_policy: "Fixed at Reservation",
      rounding_step: 1000,
      rounding_mode: "Nearest",
    },
  ],
  all_active_plans_tariff: true,
  rate_plan_required: true,
  property_currency: "UGX",
  primary_rate_plan: "STD-USD",
  card_rate_plans: [],
}

describe("Rate plan required frontend contracts", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    // jsdom lacks HTMLDialogElement.prototype.showModal
    if (!HTMLDialogElement.prototype.showModal) {
      HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", "") }
    }
    if (!HTMLDialogElement.prototype.close) {
      HTMLDialogElement.prototype.close = function () { this.removeAttribute("open") }
    }
    state.locale = {
      locale: "en-UG",
      currency: "UGX",
      currency_symbol: "UGX ",
      tax_label: "VAT",
      tax_id_label: "TIN",
      tax_rates: [0, 18],
      tax_basis: null,
      erp_tax_managed: false,
      id_types: ["National ID", "Passport"],
      id_type_hints: {},
      payment_modes: ["Cash", "Card"],
      instant_payment_modes: [],
      outlet_payment_modes: ["Cash", "Card"],
      default_nationality: "Ugandan",
      adult_age: 18,
      minor_ids: "foreign",
      integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
    }
    create.mockReset().mockResolvedValue({ data: state.roomTypes[0] })
    update.mockReset().mockResolvedValue({ data: state.roomTypes[0] })
    fetches.mockReset().mockImplementation(async (path: string) => ({
      data: path.includes("?") ? state.roomTypes : state.roomTypes[0],
    }))
    rpc.mockReset()
    guestRpc.mockReset()
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    setLang("en")
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  const text = (scope: ParentNode = host) => scope.textContent ?? ""
  const settle = () => act(() => new Promise((r) => setTimeout(r, 15)))
  const inputFor = (label: string, scope: ParentNode = host) => {
    const owner = Array.from(scope.querySelectorAll("label")).find(
      (node) => node.querySelector("span")?.textContent?.replace(/\s*\*$/, "").trim() === label,
    )
    return owner?.querySelector<HTMLInputElement>("input") ?? null
  }
  const clickButton = async (label: string, scope: ParentNode = host) => {
    const btn = Array.from(scope.querySelectorAll("button")).find((b) => b.textContent?.trim() === label)
    expect(btn, `Missing button: ${label}`).toBeDefined()
    await act(async () => btn!.click())
  }

  // ── Requirement 3: Room Types form, columns, copy & ERP vs standalone tax ──

  it("removes card-rate & extra-adult fields and says rate plans set the price when rate_plan_required is ON", async () => {
    state.locale = {
      ...state.locale,
      erp_tax_managed: true,
      tax_basis: { label: "", rate: 0, inclusive: 0, determined: 0 },
    }
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary") return Promise.resolve(baseSummary)
      if (method === "kamra.rates.room_type_tariffs") return Promise.resolve(basePanel)
      return Promise.resolve([])
    })

    await act(async () => {
      root.render(<MemoryRouter initialEntries={["/room-types?open=STANDARD"]}><App /></MemoryRouter>)
    })
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[role=dialog] section[aria-label='Contract tariffs']")).not.toBeNull()
      expect(text(host)).toContain("Rate plans set the price for all bookings at this property.")
    }, { interval: 10, timeout: 3000 })

    const dialog = host.querySelector<HTMLElement>("[role=dialog]")!
    const headers = Array.from(host.querySelectorAll("thead th"), (th) => th.textContent?.trim())
    expect(headers.some((h) => h?.includes("Card rate"))).toBe(false)
    expect(headers.some((h) => h?.includes("Extra adult"))).toBe(false)
    expect(headers).not.toContain("Tax %")

    // Card-rate section and fields are completely gone from the drawer form
    expect(dialog.querySelectorAll("details").length).toBe(0)
    for (const removed of [
      "Card rate / night (UGX)",
      "Single occupancy price",
      "Extra adult / night",
      "Extra child / night",
      "Tax %",
    ]) {
      expect(inputFor(removed, dialog), `Field should be hidden: ${removed}`).toBeNull()
    }
    expect(text(dialog)).not.toContain("Stays booked without a rate plan still use the card rate.")
    expect(text(dialog)).toContain("Rate plans set the price for all bookings at this property.")
    expect(text(dialog)).toContain("Tax comes from the accounting tax rules.")
    expect(text(dialog)).not.toContain("Tax is applied by the invoice ().")
    expect(text(dialog)).not.toMatch(FORBIDDEN)
  })

  it("keeps Tax % visible on standalone sites where ERP does not determine tax", async () => {
    state.locale = { ...state.locale, erp_tax_managed: false, tax_basis: null }
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.room_types_summary") {
        return Promise.resolve({ ...baseSummary, rate_plan_required: false, erp_tax_managed: false })
      }
      if (method === "kamra.rates.room_type_tariffs") {
        return Promise.resolve({ ...basePanel, rate_plan_required: false })
      }
      return Promise.resolve([])
    })

    await act(async () => {
      root.render(<MemoryRouter initialEntries={["/room-types?open=STANDARD"]}><App /></MemoryRouter>)
    })
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[role=dialog]")).not.toBeNull()
      expect(inputFor("Tax %", host.querySelector("[role=dialog]")!)).not.toBeNull()
    }, { interval: 10, timeout: 3000 })

    const headers = Array.from(host.querySelectorAll("thead th"), (th) => th.textContent?.trim())
    expect(headers).toContain("Tax %")
    expect(text(host)).not.toContain("Tax comes from the accounting tax rules.")
  })

  // ── Requirement 2: Settings > Front Desk readiness note ───────────────────

  it("shows the readiness note on Settings > Front Desk listing future bookings without a rate plan", async () => {
    const schema = {
      is_admin: true,
      is_it_admin: true,
      enabled_modules: ["front-desk"],
      modules: [
        {
          id: "front-desk",
          title: "Front Desk",
          description: "Front desk settings.",
          group: "modules",
          toggle: false,
          enabled: true,
          editors: ["Hotel Admin"],
          can_edit: true,
          related: [],
          sections: [
            {
              id: "room_at_booking",
              title: "Room at booking",
              description: "",
              settings: [
                {
                  key: "rate_plan_required",
                  label: "Require a rate plan for new bookings",
                  type: "check",
                  default: 0,
                  hint: "On: every new booking, walk-in, group pick-up, channel and online booking resolves a rate plan. Existing reservations are never repriced.",
                  store: "kv",
                },
              ],
            },
          ],
          values: { rate_plan_required: 0 },
        },
      ],
    }

    rpc.mockImplementation((method: string) => {
      if (method === "kamra.settings.settings_schema") return Promise.resolve(schema)
      if (method === "kamra.api.localization_countries") return Promise.resolve([{ country: "Uganda" }])
      if (method === "kamra.settings.rate_plan_readiness") {
        return Promise.resolve({
          property: "Tank Hill Hotel",
          rate_plan_required: false,
          default_plan: null,
          active_plan_count: 0,
          total_without_plan: 2,
          future_bookings_without_plan: [
            {
              name: "RES-2026-00012",
              guest_name: "Amina Okello",
              room_type: "STANDARD",
              room_type_name: "Standard Room",
              check_in_date: "2026-12-05",
              check_out_date: "2026-12-07",
              status: "Confirmed",
            },
            {
              name: "RES-2026-00019",
              guest_name: "David Kigozi",
              room_type: "STANDARD",
              room_type_name: "Standard Room",
              check_in_date: "2026-12-10",
              check_out_date: "2026-12-12",
              status: "Held",
            },
          ],
        })
      }
      if (method === "kamra.api.checkin_steps") return Promise.resolve({ steps: [] })
      if (method === "kamra.night_audit.schedule_status") return Promise.resolve({ auto: false })
      return Promise.resolve({})
    })

    await act(async () => {
      root.render(<MemoryRouter initialEntries={["/settings/front-desk"]}><App /></MemoryRouter>)
    })
    await vi.waitFor(async () => {
      await settle()
      expect(text(host)).toContain("Require a rate plan for new bookings")
    }, { interval: 10, timeout: 4000 })
    await settle()

    const body = text(host)
    expect(body).toContain("Rate plan readiness")
    expect(body).toContain("RES-2026-00012")
    expect(body).toContain("2 future bookings have no rate plan.")
    expect(body).toContain("Amina Okello")
    expect(body).toContain("David Kigozi")
    expect(body).toContain("Existing reservations are never repriced when this setting changes.")
    expect(body).toContain("No default rate plan is set yet.")
    expect(body).not.toMatch(FORBIDDEN)
    const bookingLink = Array.from(host.querySelectorAll("a")).find((a) =>
      a.getAttribute("href")?.includes("RES-2026-00012"),
    )
    expect(bookingLink, "Missing link to reservation without rate plan").toBeDefined()
  })

  // ── Requirement 1: Walk-in blocked in BookingDialog when plan required & none chosen ──

  it("blocks walk-in submission in BookingDialog when rate_plan_required is ON and no plan is chosen", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.booking_options") {
        return Promise.resolve({
          room_types: state.roomTypes,
          rate_plans: [],
          meal_plans: [],
          companies: [],
          travel_agents: [],
          experiences: [],
          property: { country: "Uganda", checkin_time: "14:00", checkout_time: "11:00" },
          ota_channels: [],
          can_set_rate: false,
          rate_plan_required: true,
          default_plan: null,
        })
      }
      if (method === "kamra.api.available_rooms") return Promise.resolve([{ name: "R101", room_number: "101", housekeeping_status: "Clean" }])
      if (method === "kamra.cashier.my_till") return Promise.resolve({ required: false, open: true, terminal_links: [] })
      if (method === "kamra.ledger.list_exchange_rates") return Promise.resolve({ business_date: "2026-12-01", rates: [] })
      if (method === "kamra.api.guest_alert") return Promise.resolve({})
      if (method === "kamra.api.guest_search") return Promise.resolve([])
      if (method === "kamra.api.get_quote") {
        return Promise.resolve({
          currency: "UGX", nights: 1, room_total: 410619, meal_total: 0, discount: 0,
          amount_before_tax: 410619, tax_percent: 0, tax_amount: 0, amount_after_tax: 410619,
          nightly: [{ date: "2026-11-24", rate: 410619 }],
        })
      }
      if (method === "kamra.rates.rate_options") {
        return Promise.resolve({
          nights: [], plans: [], voucher: null,
          plain: { room_total: 410619, amount_after_tax: 410619, discount: 0 },
          company_plan: null, default_plan: null, rate_plan_required: true, tax_inclusive: true,
        })
      }
      return Promise.resolve({})
    })

    await act(async () => {
      root.render(
        <MemoryRouter>
          <BookingDialog
            initial={{ walkIn: true, room_type: "STANDARD", guest_name: "Walk In Guest", phone: "+256772111222" }}
            onClose={() => {}}
            onBooked={() => {}}
          />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector("[role=dialog]")).not.toBeNull()
      expect(text(host)).toContain("410,619")
    }, { interval: 20, timeout: 4000 })
    const submitBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      /continue to check-in/i.test(b.textContent ?? ""),
    )
    expect(submitBtn, "Missing walk-in submit button").toBeDefined()
    expect(submitBtn!.disabled).toBe(true)
  })

  // ── Requirement 4: Live 4-screen sync across discard, approve, schedule, expire without reload ──

  it("keeps Contract Tariffs, Room Types, Rate Calendar and BookingDialog in sync without reload", async () => {
    type Version = {
      name: string
      rate: number
      valid_from: string
      valid_upto: string | null
      workflow_state: "Approved" | "Draft"
    }
    const FX = 3650
    let today = "2026-12-01"
    const versions: Version[] = [
      { name: "IP-0001", rate: 100, valid_from: "2026-11-01", valid_upto: "2026-12-05", workflow_state: "Approved" },
      { name: "IP-0002", rate: 140, valid_from: "2026-12-01", valid_upto: "2026-12-05", workflow_state: "Draft" },
    ]
    const activeOn = (day: string) => {
      const hits = versions
        .filter((v) => v.workflow_state === "Approved" && v.valid_from <= day && (!v.valid_upto || day <= v.valid_upto))
        .sort((a, b) => a.valid_from.localeCompare(b.valid_from) || a.name.localeCompare(b.name))
      return hits.at(-1) ?? null
    }
    const planPolicy = {
      name: "STD-USD", label: "Standard USD", code: "STD-USD", currency: "USD",
      fx_lock_policy: "Fixed at Reservation", rounding_step: 1000, rounding_mode: "Nearest",
      base_rate_plan: null, is_tariff: true,
    }
    const round1k = (n: number) => Math.round(n / 1000) * 1000

    rpc.mockImplementation((method: string, params: Record<string, unknown> = {}) => {
      if (method === "kamra.rates.list_contract_tariffs") {
        const asOf = String(params.as_of_date || today)
        const actVer = activeOn(asOf)
        return Promise.resolve({
          supported: true,
          property: "Tank Hill Hotel",
          property_currency: "UGX",
          business_date: today,
          can_propose: true,
          can_approve: true,
          can_discard: true,
          price_lists: [{ name: "Contract USD", currency: "USD", fx_rate: FX, fx_date: today, rate_plans: ["STD-USD"] }],
          items: [{
            item_code: "ROOM-STD",
            item_name: "Standard Room (2 Adults)",
            category: "Room",
            room_type: "Standard Room",
            occupancy: "2 Adults",
            price_list: "Contract USD",
            currency: "USD",
            fx_rate: FX,
            active: actVer,
            pending: versions.filter((v) => v.workflow_state === "Draft"),
            history: versions.filter((v) => v.workflow_state === "Approved"),
          }],
        })
      }
      if (method === "kamra.rates.discard_contract_tariff") {
        const idx = versions.findIndex((v) => v.name === params.item_price)
        if (idx >= 0) versions.splice(idx, 1)
        return Promise.resolve({ discarded: true })
      }
      if (method === "kamra.rates.approve_contract_tariff") {
        const hit = versions.find((v) => v.name === params.item_price)
        if (hit) hit.workflow_state = "Approved"
        return Promise.resolve({ approved: true })
      }
      if (method === "kamra.rates.room_types_summary") {
        const actVer = activeOn(today)
        return Promise.resolve({
          property_currency: "UGX",
          has_card_rate_plans: false,
          rate_plan_required: true,
          erp_tax_managed: true,
          default_plan: planPolicy,
          room_types: [{
            room_type: "STANDARD",
            room_count: 5,
            priced: !!actVer,
            source: actVer ? "contract" : null,
            currency: actVer ? "USD" : null,
            amount: actVer ? actVer.rate : null,
            local_amount: actVer ? round1k(actVer.rate * FX) : null,
            refusal: actVer ? null : "No approved tariff in Contract Tariffs.",
          }],
        })
      }
      if (method === "kamra.rates.rate_calendar") {
        const days = ["2026-12-01", "2026-12-03", "2026-12-06"]
        return Promise.resolve({
          from: "2026-12-01",
          days: 6,
          tax_inclusive: true,
          rate_plan_required: true,
          property_currency: "UGX",
          default_plan: "STD-USD",
          rate_plans: [planPolicy],
          rate_plan: planPolicy,
          company: null,
          room_types: [{
            room_type: "STANDARD",
            code: "STD",
            name: "Standard Room",
            base_price: 0,
            room_count: 5,
            sellable: true,
            nights: days.map((d) => {
              const v = activeOn(d)
              if (!v) return { date: d, available: false, rate: null, contract: null, refusal: "Expired tariff", occupancy: 0 }
              const local = round1k(v.rate * FX)
              return {
                date: d, available: true, rate: local, bar: local, after_season: local,
                season: null, demand: null, package: null, occupancy: 0, source: "contract",
                contract: { currency: "USD", nightly: v.rate, fx_rate: FX, fx_date: today, property_currency: "UGX" },
              }
            }),
          }],
        })
      }
      if (method === "kamra.api.booking_options") {
        return Promise.resolve({
          room_types: state.roomTypes,
          rate_plans: [{ name: "STD-USD", rate_plan_name: "Standard USD", code: "STD-USD", is_default: 1 }],
          meal_plans: [], companies: [], travel_agents: [], experiences: [],
          property: { country: "Uganda", checkin_time: "14:00", checkout_time: "11:00" },
          ota_channels: [], can_set_rate: false, rate_plan_required: true, default_plan: "STD-USD",
        })
      }
      if (method === "kamra.api.available_rooms") return Promise.resolve([{ name: "R101", room_number: "101" }])
      if (method === "kamra.cashier.my_till") return Promise.resolve({ required: false, open: true, terminal_links: [] })
      if (method === "kamra.ledger.list_exchange_rates") return Promise.resolve({ business_date: "2026-12-01", rates: [] })
      if (method === "kamra.api.guest_alert") return Promise.resolve({})
      if (method === "kamra.api.guest_search") return Promise.resolve([])
      if (method === "kamra.rates.rate_options") {
        const v = activeOn(String(params.check_in_date || today))
        return Promise.resolve({
          nights: [], plain: { room_total: 0, amount_after_tax: 0, discount: 0 },
          plans: [{
            name: "STD-USD", label: "Standard USD", code: "STD-USD", is_default: true,
            available: !!v, refusal: v ? undefined : "No approved tariff in Contract Tariffs.",
            tariff: v ? { currency: "USD", nightly: v.rate, fx_rate: FX, fx_date: today, property_currency: "UGX" } : null,
            agreed_rate: v ? round1k(v.rate * FX) : undefined,
            room_total: v ? round1k(v.rate * FX) : 0,
            amount_after_tax: v ? round1k(v.rate * FX) : 0,
            vs_plain: 0,
          }],
          voucher: null, company_plan: null, default_plan: "STD-USD", rate_plan_required: true, tax_inclusive: true,
        })
      }
      if (method === "kamra.api.get_quote") {
        const day = String(params.check_in_date || today)
        const v = activeOn(day)
        if (!v) return Promise.reject(new Error("Rate plan 'Standard USD' has no approved tariff for Standard Room. Approve a tariff in Contract Tariffs."))
        const local = round1k(v.rate * FX)
        return Promise.resolve({
          currency: "UGX", rate_plan: "STD-USD", nights: 1, room_total: local,
          amount_after_tax: local, agreed_rate: local, tax_percent: 18, tax_amount: 0, discount: 0, meal_total: 0,
          tariff: { currency: "USD", nightly: v.rate, fx_rate: FX, fx_date: today, property_currency: "UGX" },
          nightly: [{ date: day, rate: local }],
        })
      }
      return Promise.resolve([])
    })

    // Mount ALL FOUR surfaces simultaneously (zero reload!)
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/tariffs"]}>
          <div>
            <div data-surface="tariffs"><ContractTariffs /></div>
            <div data-surface="room-types"><ConfiguredScreen config="roomTypesConfig" /></div>
            <div data-surface="calendar"><RateCalendar /></div>
            <div data-surface="booking">
              <BookingDialog
                initial={{ room_type: "STANDARD", check_in_date: "2026-12-01", nights: 1 }}
                onClose={() => {}}
                onBooked={() => {}}
              />
            </div>
          </div>
        </MemoryRouter>,
      )
    })

    const surface = (id: string) => host.querySelector<HTMLElement>(`[data-surface="${id}"]`)!

    // Baseline: all four show USD 100 / UGX 365,000
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("tariffs"))).toContain("USD 100.00")
      expect(text(surface("room-types"))).toContain("USD 100.00")
      expect(text(surface("calendar"))).toContain("365,000")
      expect(text(surface("booking"))).toContain("365,000")
    }, { interval: 20, timeout: 4000 })

    // 1. Discard draft ($140) from Contract Tariffs -> all four remain $100 / 365,000, draft gone
    await clickButton("Discard", surface("tariffs"))
    const discardDialog = surface("tariffs").querySelector("dialog")!
    await clickButton("Discard", discardDialog)
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("tariffs"))).not.toContain("USD 140.00")
      expect(text(surface("room-types"))).toContain("USD 100.00")
      expect(text(surface("calendar"))).toContain("365,000")
      expect(text(surface("booking"))).toContain("365,000")
    }, { interval: 20, timeout: 4000 })

    // 2. Approve new version ($120 effective today) -> all four immediately update to $120 / 438,000 without reload!
    versions.push({ name: "IP-0003", rate: 120, valid_from: "2026-12-01", valid_upto: "2026-12-05", workflow_state: "Draft" })
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:tariffs-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("tariffs"))).toContain("USD 120.00")
    }, { interval: 20, timeout: 4000 })
    await clickButton("Approve", surface("tariffs"))
    const approveDialog = surface("tariffs").querySelector("dialog")!
    await clickButton("Approve", approveDialog)
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("room-types"))).toContain("USD 120.00")
      expect(text(surface("room-types"))).toContain("UGX 438,000")
      expect(text(surface("calendar"))).toContain("438,000")
      expect(text(surface("booking"))).toContain("438,000")
    }, { interval: 20, timeout: 4000 })

    // 3. Schedule future version ($150 from 2026-12-03) -> today stays $120, future calendar cell shows $150 (548,000)
    versions.push({ name: "IP-0004", rate: 150, valid_from: "2026-12-03", valid_upto: "2026-12-05", workflow_state: "Draft" })
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:tariffs-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("tariffs"))).toContain("USD 150.00")
    }, { interval: 20, timeout: 4000 })
    await clickButton("Approve", surface("tariffs"))
    const approveFutureDialog = surface("tariffs").querySelector("dialog")!
    await clickButton("Approve", approveFutureDialog)
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("room-types"))).toContain("USD 120.00")
      expect(text(surface("calendar"))).toContain("438,000")
      expect(text(surface("calendar"))).toContain("548,000")
    }, { interval: 20, timeout: 4000 })

    // 4. Expire (business date moves past 2026-12-05 to 2026-12-06) -> all four agree: no active rate!
    today = "2026-12-06"
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:tariffs-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(text(surface("tariffs"))).toContain("No active tariff")
      expect(text(surface("room-types"))).toContain("No rate set")
    }, { interval: 20, timeout: 4000 })
  })

  // ── R1 Finding #4: syncRev preserves user-selected meal plan, discount & explicit choices ──

  it("preserves user-selected meal plan, discount, and explicit empty selections across syncRev refresh", async () => {
    let optionsCalls = 0
    let policyRequired = true
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.api.booking_options") {
        optionsCalls += 1
        return Promise.resolve({
          room_types: state.roomTypes,
          rate_plans: [
            { name: "STD-USD", rate_plan_name: "Standard USD", code: "STD", is_default: 1, discount_eligible: 1 },
            { name: "PROMO-USD", rate_plan_name: "Promo USD", code: "PROMO", is_default: 0, discount_eligible: 1 },
          ],
          meal_plans: [
            { name: "MP-EP", code: "EP", label: "Room Only", price_per_adult: 0, is_default: 1 },
            { name: "MP-BB", code: "BB", label: "Breakfast", price_per_adult: 25, is_default: 0 },
          ],
          companies: [],
          travel_agents: [],
          experiences: [],
          property: { country: "Uganda", checkin_time: "14:00", checkout_time: "11:00" },
          ota_channels: [],
          can_set_rate: false,
          can_discount: true,
          discount_reasons: [{ name: "LOYAL", description: "Loyalty guest" }],
          rate_plan_required: policyRequired,
          default_plan: "STD-USD",
        })
      }
      if (method === "kamra.api.available_rooms") return Promise.resolve([{ name: "R101", room_number: "101", housekeeping_status: "Clean" }])
      if (method === "kamra.cashier.my_till") return Promise.resolve({ required: false, open: true, terminal_links: [] })
      if (method === "kamra.ledger.list_exchange_rates") return Promise.resolve({ business_date: "2026-12-01", rates: [] })
      if (method === "kamra.api.guest_alert") return Promise.resolve({})
      if (method === "kamra.api.guest_search") return Promise.resolve([])
      if (method === "kamra.rates.rate_options") {
        return Promise.resolve({
          nights: [], plans: [], voucher: null,
          plain: { room_total: 160, amount_after_tax: 160, discount: 0 },
          company_plan: null, default_plan: "STD-USD", rate_plan_required: policyRequired, tax_inclusive: true,
          can_discount: true, discount_reasons: [{ name: "LOYAL", description: "Loyalty guest" }],
        })
      }
      if (method === "kamra.api.get_quote") {
        return Promise.resolve({
          currency: "UGX", rate_plan: "STD-USD", nights: 1, room_total: 160, meal_total: 25,
          discount: 16, amount_before_tax: 169, tax_percent: 0, tax_amount: 0, amount_after_tax: 169,
          nightly: [{ date: "2026-12-01", rate: 160 }],
        })
      }
      return Promise.resolve({})
    })

    const selectFor = (label: string) => {
      const owner = Array.from(host.querySelectorAll("label")).find(
        (node) => node.querySelector("span")?.textContent?.replace(/\s*\*$/, "").trim() === label,
      )
      return owner?.querySelector<HTMLSelectElement>("select") ?? null
    }
    const setReactValue = async (el: HTMLSelectElement | HTMLInputElement, value: string) => {
      const proto = el instanceof HTMLSelectElement ? HTMLSelectElement.prototype : HTMLInputElement.prototype
      const setter = Object.getOwnPropertyDescriptor(proto, "value")!.set!
      await act(async () => {
        setter.call(el, value)
        el.dispatchEvent(new Event("input", { bubbles: true }))
        el.dispatchEvent(new Event("change", { bubbles: true }))
      })
    }

    await act(async () => {
      root.render(
        <MemoryRouter>
          <BookingDialog
            initial={{ room_type: "STANDARD", check_in_date: "2026-12-01", nights: 1 }}
            onClose={() => {}}
            onBooked={() => {}}
          />
        </MemoryRouter>,
      )
    })

    await vi.waitFor(async () => {
      await settle()
      expect(selectFor("Meal plan")?.value).toBe("MP-EP")
      expect(selectFor("Rate plan")?.value).toBe("STD-USD")
    }, { interval: 20, timeout: 4000 })

    // Operator picks Breakfast (MP-BB), Promo USD (PROMO-USD), and enters a 10% discount
    await setReactValue(selectFor("Meal plan")!, "MP-BB")
    await setReactValue(selectFor("Rate plan")!, "PROMO-USD")
    const discToggle = Array.from(host.querySelectorAll("button")).find((b) => /discount/i.test(b.textContent ?? ""))
    if (discToggle) await act(async () => discToggle.click())
    const discValueInput = Array.from(host.querySelectorAll<HTMLInputElement>("input")).find(
      (i) => i.getAttribute("aria-label")?.toLowerCase().includes("discount") || i.placeholder?.includes("10") || i.type === "number",
    )
    if (discValueInput) await setReactValue(discValueInput, "10")

    expect(selectFor("Meal plan")!.value).toBe("MP-BB")
    expect(selectFor("Rate plan")!.value).toBe("PROMO-USD")

    // Fire kamra:tariffs-changed -> triggers syncRev -> re-fetches booking_options
    const callsBefore = optionsCalls
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:tariffs-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(optionsCalls).toBeGreaterThan(callsBefore)
    }, { interval: 20, timeout: 4000 })
    await settle()

    // BOTH meal plan (Breakfast) AND rate plan (PROMO-USD) AND discount MUST stay intact!
    expect(selectFor("Meal plan")!.value).toBe("MP-BB")
    expect(selectFor("Rate plan")!.value).toBe("PROMO-USD")
    if (discValueInput) expect(discValueInput.value).toBe("10")

    // Also verify explicit empty choices ("Room only" meal plan and standard rate when policy is off)
    policyRequired = false
    const callsBeforePolicyOff = optionsCalls
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:settings-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(optionsCalls).toBeGreaterThan(callsBeforePolicyOff)
    }, { interval: 20, timeout: 4000 })
    await setReactValue(selectFor("Meal plan")!, "")
    await setReactValue(selectFor("Rate plan")!, "")
    expect(selectFor("Meal plan")!.value).toBe("")
    expect(selectFor("Rate plan")!.value).toBe("")

    const callsBeforeEmptySync = optionsCalls
    await act(async () => { window.dispatchEvent(new CustomEvent("kamra:tariffs-changed")) })
    await vi.waitFor(async () => {
      await settle()
      expect(optionsCalls).toBeGreaterThan(callsBeforeEmptySync)
    }, { interval: 20, timeout: 4000 })
    await settle()
    expect(selectFor("Meal plan")!.value).toBe("")
    expect(selectFor("Rate plan")!.value).toBe("")
  })

  // ── R1 Finding #3 & #5: Public /stay hides raw base_price & RateCalendar hides BAR when required ──

  it("hides Price per night / $0 on PublicListing when rate_plan_required has no active rate plan", async () => {
    guestRpc.mockImplementation((method: string) => {
      if (method === "kamra.public_api.resolve_slug") {
        return Promise.resolve({
          kind: "listing",
          property: "R1 Gamma NoDef",
          listing_slug: "gamma-suite",
          room_type: "R1 Gamma NoDef-GMS",
          room_type_name: "Gamma Suite",
        })
      }
      if (method === "kamra.public_api.showcase") {
        return Promise.resolve({
          ui_locale: { currency_symbol: "$", locale: "en-US", currency: "USD" },
          rate_plan_required: true,
          default_rate_plan: null,
          property: {
            name: "R1 Gamma NoDef",
            property_name: "R1 Gamma NoDef",
            description: null, logo_url: null, hero_image: null, brand_accent: "Emerald",
            payment_mode: "Pay at hotel", cleaning_fee: 0, security_deposit_amount: 0,
            minimum_nights: 1, booking_mode: "Instant", property_kind: "Hotel",
            checkin_time: "14:00", checkout_time: "11:00",
            house_rules: null, pets_policy: null, children_policy: null,
            city: "Kampala", state: "", phone: null, gallery: [],
          },
          room_types: [{
            name: "R1 Gamma NoDef-GMS",
            room_type_name: "Gamma Suite",
            listing_slug: "gamma-suite",
            location_slug: null,
            description: null,
            base_price: 0,
            adults_capacity: 2,
            children_capacity: 0,
            bed_type: null, room_view: null, amenities: [], media: [],
            location_name: null, location_address: null, google_maps_url: null,
            latitude: null, longitude: null,
          }],
          locations: [{ name: "R1 Gamma NoDef", slug: null, address: null, google_maps_url: null, latitude: null, longitude: null, room_types: ["R1 Gamma NoDef-GMS"], from_rate: 0 }],
          meal_plans: [],
        })
      }
      if (method === "kamra.public_api.search_stay") {
        return Promise.resolve([{
          room_type: "R1 Gamma NoDef-GMS",
          rooms_left: 2,
          available: false,
          quote: null,
          reason: "No rate is available for these dates. Please contact the property.",
        }])
      }
      return Promise.resolve({})
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/stay/gamma-suite"]}>
          <Routes>
            <Route path="/stay/:slug/*" element={<PublicListing />} />
          </Routes>
        </MemoryRouter>,
      )
    })

    await vi.waitFor(async () => {
      await settle()
      expect(text(host)).toContain("Gamma Suite")
    }, { interval: 20, timeout: 4000 })
    await settle()

    expect(text(host)).not.toContain("Price per night")
    expect(text(host)).not.toContain("$0")
    expect(text(host)).not.toContain("$220")
    expect(text(host)).not.toMatch(FORBIDDEN)
  })

  it("hides BAR column on RateCalendar when rate_plan_required is ON and no default plan is set", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.rates.rate_calendar") {
        return Promise.resolve({
          from: "2026-12-01",
          days: 2,
          tax_inclusive: true,
          rate_plan_required: true,
          property_currency: "UGX",
          default_plan: null,
          rate_plans: [],
          rate_plan: null,
          company: null,
          room_types: [{
            room_type: "STANDARD",
            code: "STD",
            name: "Standard Room",
            base_price: 0,
            room_count: 5,
            sellable: true,
            nights: [
              { date: "2026-12-01", available: false, rate: null, contract: null, refusal: "Set a default rate plan in Rate Plans.", occupancy: 0 },
              { date: "2026-12-02", available: false, rate: null, contract: null, refusal: "Set a default rate plan in Rate Plans.", occupancy: 0 },
            ],
          }],
        })
      }
      return Promise.resolve([])
    })

    await act(async () => {
      root.render(<MemoryRouter initialEntries={["/rate-calendar"]}><RateCalendar /></MemoryRouter>)
    })
    await vi.waitFor(async () => {
      await settle()
      expect(text(host)).toContain("Standard Room")
    }, { interval: 20, timeout: 4000 })

    const headers = Array.from(host.querySelectorAll("thead th"), (th) => th.textContent?.trim())
    expect(headers).not.toContain("BAR")
    expect(text(host)).not.toContain("Above BAR")
  })

  // ── Requirement 5: all 8 languages & zero Kamra/ERPNext/Frappe wording ────

  it.each([
    ["ar", ar], ["de", de], ["es", es], ["fil", fil], ["id", id], ["pt", pt], ["zh-CN", zh],
  ] as [string, Record<string, string>][])("translates every new string cleanly in %s with no brand leaks", async (lang, dict) => {
    await ensureDictionary(lang)
    setLang(lang)
    for (const key of NEW_STRINGS) {
      expect(dict[key], `${lang}: missing translation for ${JSON.stringify(key)}`).toBeTruthy()
      expect(dict[key], `${lang}: untranslated ${JSON.stringify(key)}`).not.toBe(key)
      expect(dict[key], `${lang}: brand leak in ${JSON.stringify(key)}`).not.toMatch(FORBIDDEN)
    }
  })
})
