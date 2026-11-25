// @vitest-environment jsdom
/* RED->GREEN tests for P18 Interactive 8-Window & Routing UI, Quick Split By Qty,
   is_alcohol removal, and FolioHistory precision/mobile cards. */
import fs from "node:fs"
import path from "node:path"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import FolioView from "../src/screens/FolioView"
import FolioHistory from "../src/screens/FolioHistory"
import { setLang } from "../src/lib/dir"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => "Kuwait Bay Hotel",
  newIdempotencyKey: () => "p18-key",
}))
const auth = vi.hoisted(() => ({ roles: ["Front Desk", "Finance", "Hotel Admin"] }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed", roles: auth.roles }) }))

function kwdLocale(taxReturn = false) {
  return {
    currency: "KWD",
    currency_symbol: "KWD ",
    locale: "en",
    tax_label: "VAT",
    tax_id_label: "Tax ID",
    tax_rates: [0, 5],
    payment_modes: ["Cash", "Card", "Company Credit"],
    instant_payment_modes: [],
    outlet_payment_modes: ["Cash", "Card"],
    id_types: ["Passport"],
    id_type_hints: {},
    default_nationality: "Kuwaiti",
    adult_age: 18,
    minor_ids: "foreign",
    sells_alcohol: true,
    integrations: {
      payment_gateways: [],
      tax_return: taxReturn,
      verify_tax_id: false,
      verify_guest_id: false,
    },
  }
}

function kwdFolioPayload(overrides: Record<string, unknown> = {}) {
  return {
    folio: {
      name: "FOLIO-W1",
      property: "Kuwait Bay Hotel",
      status: "Open",
      folio_type: "Guest",
      window_no: 1,
      payee_type: "Guest",
      invoice_number: null,
      sales_invoice: null,
      guest_name: "Nasser Al-Sabah",
      charges: [
        {
          name: "ch-qty",
          posting_date: "2026-11-24",
          charge_type: "Laundry",
          description: "Express Press x4",
          qty: 4,
          rate: 10.125,
          amount: 40.5,
          gst_rate: 0,
          gst_amount: 0,
          total: 40.5,
        },
      ],
      payments: [],
      charges_total: 40.5,
      tax_total: 0,
      grand_total: 40.5,
      payments_total: 0,
      balance: 40.5,
      ...overrides,
    },
    property: {
      currency: "KWD",
      name: "Kuwait Bay Hotel",
      legal_name: null,
      logo_url: null,
      address: "Kuwait City",
      state: null,
      gstin: null,
      phone: null,
      email: null,
      sac: null,
      place_of_supply: null,
      tax_label: "VAT",
    },
    stay: {
      reservation: "RES-P18-1",
      check_in: "2026-11-24",
      check_out: "2026-11-27",
      arrival: null,
      departure: null,
      nights: 3,
      adults: 2,
      children: 0,
      pax: 2,
      meal_plan: null,
      room_type: "Kuwait Bay Hotel-DLX",
      room: "RM-101",
      room_number: "101",
      company: "CORP-1",
      group_booking: null,
      booked_by_name: null,
      booked_by_phone: null,
      contact_preference: null,
    },
    gst_summary: [],
    bill_to: null,
    statutory: null,
    document: {
      title: "Provisional Bill",
      is_final: false,
      number: "FOLIO-W1",
      date: "2026-11-24",
      amount_in_words: "",
      footer: null,
      service_code_label: "SAC",
    },
    lines: [
      {
        row: "ch-qty",
        posting_date: "2026-11-24",
        charge_type: "Laundry",
        description: "Express Press x4",
        qty: 4,
        rate: 10.125,
        amount: 40.5,
        gst_rate: 0,
        gst_amount: 0,
        total: 40.5,
        service_code: null,
      },
    ],
    guest: {
      name: "Nasser Al-Sabah",
      phone: null,
      email: null,
      nationality: "Kuwaiti",
      address: "",
      id_type: null,
    },
    summary_by_head: [],
    tax_summary: [],
  }
}

const WINDOWS_OVERVIEW = {
  folio: "FOLIO-W1",
  property: "Kuwait Bay Hotel",
  reservation: "RES-P18-1",
  group_booking: null,
  window_cap: 8,
  can_add_window: true,
  windows: [
    {
      window_no: 1,
      folio: "FOLIO-W1",
      folio_type: "Guest",
      payee_type: "Guest",
      payee_name: "Nasser Al-Sabah",
      payee_company: null,
      payee_agent: null,
      payment_method: "Cash",
      status: "Open",
      invoice_number: null,
      charges_total: 40.5,
      grand_total: 40.5,
      payments_total: 0,
      balance: 40.5,
      sales_invoice: null,
      efris_fiscalized: false,
      routed_rule_count: 0,
    },
    {
      window_no: 2,
      folio: "FOLIO-W2",
      folio_type: "Company",
      payee_type: "Company",
      payee_name: "Gulf Energy Co",
      payee_company: "CORP-1",
      payee_agent: null,
      payment_method: "Company Credit",
      status: "Open",
      invoice_number: null,
      charges_total: 45.25,
      grand_total: 45.25,
      payments_total: 0,
      balance: 45.25,
      sales_invoice: null,
      efris_fiscalized: false,
      routed_rule_count: 2,
    },
    {
      window_no: 3,
      folio: "FOLIO-W3",
      folio_type: "Extra",
      payee_type: "Travel Agent",
      payee_name: "Horizon Tours",
      payee_company: null,
      payee_agent: "TA-1",
      payment_method: "Card",
      status: "Closed",
      invoice_number: "INV-003",
      charges_total: 10,
      grand_total: 10,
      payments_total: 10,
      balance: 0,
      sales_invoice: "ACC-SINV-003",
      efris_fiscalized: false,
      routed_rule_count: 0,
    },
  ],
}

describe("P18 Interactive 8-Window & Routing UI, Qty Split & Hygiene", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    auth.roles = ["Front Desk", "Finance", "Hotel Admin"]
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    setLang("en")
    localStorage.clear()
    vi.restoreAllMocks()
    vi.unstubAllGlobals()
  })

  function setupDefaultMocks(opts: {
    folioOverrides?: Record<string, unknown>
    overview?: typeof WINDOWS_OVERVIEW
    taxReturn?: boolean
  } = {}) {
    const overview = opts.overview ?? WINDOWS_OVERVIEW
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.folio_invoice") return Promise.resolve(kwdFolioPayload(opts.folioOverrides))
      if (method === "kamra.payment_transfers.folio_windows_overview") return Promise.resolve(overview)
      if (method === "kamra.api.reservation_folios") {
        return Promise.resolve(
          overview.windows.map((w) => ({
            name: w.folio,
            folio_type: w.folio_type,
            status: w.status,
            invoice_number: w.invoice_number,
            balance: w.balance,
            grand_total: w.grand_total,
            payments_total: w.payments_total,
            window_no: w.window_no,
            payee_type: w.payee_type,
            payee_company: w.payee_company,
            payee_agent: w.payee_agent,
            company: w.payee_company,
            agent_name: w.payee_type === "Travel Agent" ? w.payee_name : null,
            payment_method: w.payment_method,
          })),
        )
      }
      if (method === "kamra.routing.list_routing_instructions") {
        return Promise.resolve([
          {
            name: "RI-00001",
            property: "Kuwait Bay Hotel",
            reservation: "RES-P18-1",
            target_type: "Window",
            target_window: 2,
            disabled: 0,
            all_codes: 0,
            codes: [{ charge_type: "Laundry", transaction_code: null }],
            from_date: "2026-11-24",
            to_date: "2026-11-27",
            monday: 1,
            tuesday: 1,
            wednesday: 1,
            thursday: 1,
            friday: 1,
            saturday: 0,
            sunday: 0,
            limit_type: "Amount",
            limit_value: 150,
            per_day: 0,
            used_amount: 75,
          },
        ])
      }
      if (method === "kamra.routing.routing_options") {
        return Promise.resolve({
          window_cap: 8,
          transaction_codes: [
            { code: "2000", description: "Food & Beverage", charge_type: "Food & Beverage", is_alcohol: 0 },
            { code: "2010", description: "Beverage & Alcohol", charge_type: "Food & Beverage", is_alcohol: 1 },
            { code: "3000", description: "Laundry", charge_type: "Laundry", is_alcohol: 0 },
          ],
          in_house_reservations: [
            { name: "RES-P18-2", room_number: "102", guest_name: "Layla Hassan" },
          ],
        })
      }
      if (method === "kamra.api.booking_options") {
        return Promise.resolve({
          companies: [{ name: "CORP-1", company_name: "Gulf Energy Co" }],
          travel_agents: [{ name: "TA-1", agent_name: "Horizon Tours" }],
        })
      }
      if (method === "kamra.api.open_folio_window") return Promise.resolve({ folio: "FOLIO-W4", window_no: 4 })
      if (method === "kamra.payment_transfers.update_folio_window") return Promise.resolve({ ok: true })
      if (method === "kamra.routing.save_routing_instruction") return Promise.resolve({ name: "RI-00002" })
      if (method === "kamra.routing.delete_routing_instruction") return Promise.resolve({ name: "RI-00001", action: "deleted" })
      if (method === "kamra.api.split_folio_charge") return Promise.resolve({ kept: 30.375, moved: 10.125 })
      if (method === "kamra.api.add_folio_charge") return Promise.resolve({})
      if (method === "kamra.api.property_locale") return Promise.resolve(kwdLocale(opts.taxReturn))
      if (method === "kamra.ledger.list_reason_codes") return Promise.resolve([])
      if (method === "kamra.api.correct_charged_nights") return Promise.resolve({ nights: [] })
      if (method === "kamra.api.linked_records") return Promise.resolve(null)
      if (method === "kamra.api.allowance_requests") return Promise.resolve({ requests: [], can_decide: false })
      return Promise.resolve({})
    })
  }

  async function renderFolio(folioId = "FOLIO-W1") {
    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={[`/billing/${folioId}`]}>
          <Routes>
            <Route path="/billing/:name" element={<FolioView />} />
          </Routes>
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Nasser Al-Sabah"), { interval: 5, timeout: 5000 })
  }

  it("renders W1..W8 tabs with 3dp KWD balances, + Window creation, and Edit Payee popover", async () => {
    setupDefaultMocks()
    await renderFolio()

    await vi.waitFor(() => expect(host.querySelector('[data-testid="folio-window-tabs"]')).not.toBeNull())
    const tabStrip = host.querySelector('[data-testid="folio-window-tabs"]')!
    expect(tabStrip.textContent).toContain("W1")
    expect(tabStrip.textContent).toContain("W2")
    expect(tabStrip.textContent).toContain("W3")
    expect(tabStrip.textContent).toContain("40.500")
    expect(tabStrip.textContent).toContain("45.250")
    expect(tabStrip.querySelector('[data-testid="routing-active-dot-2"]')).not.toBeNull()
    expect(tabStrip.querySelector('[data-testid="window-locked-3"]')).not.toBeNull()

    // + Window opens popover and invokes open_folio_window
    const addBtn = host.querySelector<HTMLButtonElement>('[data-testid="add-window-btn"]')!
    expect(addBtn.disabled).toBe(false)
    await act(async () => addBtn.click())
    const confirmAdd = host.querySelector<HTMLButtonElement>('[data-testid="confirm-open-window"]')!
    await act(async () => confirmAdd.click())
    expect(rpc).toHaveBeenCalledWith("kamra.api.open_folio_window", expect.objectContaining({
      reservation: "RES-P18-1",
    }))

    // Edit Payee / Method popover on open uninvoiced window
    const editPayeeBtn = host.querySelector<HTMLButtonElement>('[data-testid="edit-window-payee-btn"]')!
    await act(async () => editPayeeBtn.click())
    const savePayeeBtn = host.querySelector<HTMLButtonElement>('[data-testid="save-window-payee-btn"]')!
    await act(async () => savePayeeBtn.click())
    expect(rpc).toHaveBeenCalledWith("kamra.payment_transfers.update_folio_window", expect.objectContaining({
      folio: "FOLIO-W1",
    }))
  })

  it("removes deprecated is_alcohol checkbox from Post a Charge and supports Transaction Code 2010", async () => {
    setupDefaultMocks()
    await renderFolio()

    const alcoholCheckboxes = [...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].filter((box) =>
      /alcohol/i.test(box.closest("label")?.textContent ?? ""),
    )
    expect(alcoholCheckboxes).toHaveLength(0)

    const tcSelect = host.querySelector<HTMLSelectElement>('select[aria-label="Transaction Code"]')
    expect(tcSelect).not.toBeNull()
    await vi.waitFor(() => expect(tcSelect!.textContent).toContain("2010"))
  })

  it("supports Quick Split By Qty with 3dp minor-unit preview and calls split_folio_charge with qty", async () => {
    setupDefaultMocks()
    await renderFolio()

    const row = [...host.querySelectorAll("tr")].find((r) => r.textContent?.includes("Express Press x4"))!
    const checkbox = row.querySelector<HTMLInputElement>('input[type="checkbox"]')!
    await act(async () => checkbox.click())

    const byQtyBtn = host.querySelector<HTMLButtonElement>('[data-testid="split-mode-qty"]')!
    await act(async () => byQtyBtn.click())

    const qtyInput = host.querySelector<HTMLInputElement>('input[aria-label="Quantity to split"]')!
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
    await act(async () => {
      setter.call(qtyInput, "1")
      qtyInput.dispatchEvent(new Event("input", { bubbles: true }))
    })

    // Live 3dp KWD preview: kept 30.375, moved 10.125
    expect(host.textContent).toContain("30.375")
    expect(host.textContent).toContain("10.125")

    const targetW2 = [...host.querySelectorAll("button")].find(
      (b) => b.textContent?.includes("Company") && b.textContent?.includes("owes"),
    )!
    await act(async () => targetW2.click())

    expect(rpc).toHaveBeenCalledWith("kamra.api.split_folio_charge", {
      from_folio: "FOLIO-W1",
      charge_row: "ch-qty",
      to_folio: "FOLIO-W2",
      qty: 1,
    })
  })

  it("opens RoutingInstructionDrawer, shows limit progress bar, and saves/deletes routing rules", async () => {
    setupDefaultMocks()
    await renderFolio()

    const openDrawerBtn = host.querySelector<HTMLButtonElement>('[data-testid="open-routing-drawer-btn"]')!
    await act(async () => openDrawerBtn.click())

    await vi.waitFor(() => expect(host.querySelector('[data-testid="routing-drawer"]')).not.toBeNull())
    const drawer = host.querySelector('[data-testid="routing-drawer"]')!
    // Existing rule progress bar: 75.000 / 150.000
    expect(drawer.textContent).toContain("75.000")
    expect(drawer.textContent).toContain("150.000")

    // Delete existing rule
    const delBtn = drawer.querySelector<HTMLButtonElement>('[data-testid="delete-routing-RI-00001"]')!
    await act(async () => delBtn.click())
    expect(rpc).toHaveBeenCalledWith("kamra.routing.delete_routing_instruction", { name: "RI-00001" })

    // Create a new rule with Transaction Code 2010
    const code2010 = drawer.querySelector<HTMLInputElement>('input[data-code="2010"]')!
    await act(async () => code2010.click())
    const saveBtn = drawer.querySelector<HTMLButtonElement>('[data-testid="save-routing-btn"]')!
    await act(async () => saveBtn.click())
    expect(rpc).toHaveBeenCalledWith(
      "kamra.routing.save_routing_instruction",
      expect.objectContaining({
        data: expect.objectContaining({
          reservation: "RES-P18-1",
          target_type: "Window",
          codes: expect.arrayContaining([expect.objectContaining({ transaction_code: "2010" })]),
        }),
      }),
    )
  })

  it("renders FolioHistory with 3dp KWD precision, mobile cards, window badge, and tax_return-gated fiscal badge", async () => {
    rpc.mockReset().mockImplementation((method: string) => {
      if (method === "kamra.api.property_locale") return Promise.resolve(kwdLocale(false))
      if (method === "kamra.ledger.folio_history") {
        return Promise.resolve({
          folios: [
            {
              name: "FOLIO-HIST-1",
              guest_name: "Amina Khalid",
              reservation: "RES-H1",
              status: "Closed",
              folio_type: "Company",
              window_no: 2,
              payee_type: "Company",
              payee_company: "Gulf Energy Co",
              invoice_number: "INV-2026-009",
              sales_invoice: "ACC-SINV-009",
              grand_total: 125.5,
              balance: 12.375,
              reprints: 1,
            },
          ],
        })
      }
      return Promise.resolve({})
    })

    await act(async () =>
      root.render(
        <MemoryRouter>
          <FolioHistory />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(() => expect(host.textContent).toContain("Amina Khalid"))
    await vi.waitFor(() => expect(host.textContent).toContain("125.500"))
    expect(host.textContent).toContain("12.375")
    expect(host.textContent).toContain("W2")
    expect(host.querySelector('[data-testid="folio-history-cards"]')).not.toBeNull()
    expect(host.textContent).not.toMatch(/EFRIS/i)

    // Source hygiene check: no hardcoded inr() / 0dp / 2dp / en-US in FolioHistory.tsx or FolioView.tsx
    const fhSrc = fs.readFileSync(path.resolve(__dirname, "../src/screens/FolioHistory.tsx"), "utf8")
    const fvSrc = fs.readFileSync(path.resolve(__dirname, "../src/screens/FolioView.tsx"), "utf8")
    expect(fhSrc).not.toMatch(/const\s+inr\s*=/)
    expect(fhSrc).not.toMatch(/maximumFractionDigits:\s*[02]/)
    expect(fvSrc).not.toMatch(/const\s+inr\s*=/)
    expect(fvSrc).not.toMatch(/maximumFractionDigits:\s*[02]/)
    expect(fvSrc).not.toMatch(/"en-US"/)
    expect(fhSrc).not.toMatch(/"en-US"/)
  })
})
