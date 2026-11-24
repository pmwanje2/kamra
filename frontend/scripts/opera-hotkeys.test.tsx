// @vitest-environment jsdom
import fs from "node:fs"
import path from "node:path"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { CommandPalette } from "../src/components/CommandPalette"
import EndOfDayWizard from "../src/screens/EndOfDayWizard"
import ReservationDetail from "../src/screens/ReservationDetail"
import { matchOperaHotkey } from "../src/lib/hotkeys"
import { loadLocale } from "../src/lib/money"

const { rpc, propState } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return { rpc: vi.fn(), propState: { current: "PROP-A" } }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => propState.current,
  setCurrentProperty: (p: string) => {
    propState.current = p
  },
  reservationDetail: (name: string) => rpc("kamra.api.reservation_detail", { reservation: name }),
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", user: "desk@example.test", roles: ["Front Desk", "Hotel Admin"] }),
}))

vi.mock("../src/lib/cashierAuth", () => ({
  useCashierAuth: () => ({ withPin: async (fn: (pin: string) => Promise<unknown>) => fn("1234") }),
}))

vi.mock("../src/lib/realtime", () => ({
  useRealtime: () => {},
}))

function LocationProbe() {
  const loc = useLocation()
  return <div data-testid="loc">{`${loc.pathname}${loc.search}`}</div>
}

const KWD_LOCALE = {
  currency: "KWD",
  currency_symbol: "KWD ",
  locale: "en-GB",
  tax_label: "Tax",
  tax_id_label: "Tax ID",
  tax_rates: [],
  id_types: [],
  id_type_hints: {},
  payment_modes: ["Cash"],
  instant_payment_modes: [],
  outlet_payment_modes: ["Cash"],
  default_nationality: "",
  adult_age: 18,
  minor_ids: "foreign",
  integrations: { payment_gateways: [], tax_return: false, verify_tax_id: false, verify_guest_id: false },
}

const EOD_FIXTURE = {
  property: "PROP-A",
  property_name: "Grand Horizon",
  currency: "KWD",
  business_date: "2026-11-24",
  server_date: "2026-11-24",
  server_time: "22:00:00",
  property_now: "2026-11-24 22:00:00",
  require_cashier_pin: 0,
  blind_count: 0,
  high_balance_threshold: 500,
  auto_audit_with_blockers: "Hold",
  reports: [],
  audit: "NA-001",
  ran: false,
  status: "Draft",
  current_step: 1,
  notes: "",
  acknowledgements: [],
  steps: [],
  sessions: [],
  checks: [
    {
      key: "arrivals_pending",
      title: "Arrivals pending",
      severity: "block",
      count: 1,
      action: "Check in or mark no-show.",
      items: [
        {
          reservation: "RES-ARR-1",
          guest_name: "Alan Turing",
          room_type: "Deluxe",
          room: "101",
          check_in_date: "2026-11-24",
          nights: 2,
          adults: 2,
          children: 0,
          rate: 12.5,
          rate_plan: "BAR",
          eta: "18:00",
          policy: "No-show fee",
          outcome: "no_show",
          fee_amount: 12.5,
        },
      ],
    },
    {
      key: "departures_due",
      title: "Departures due",
      severity: "block",
      count: 2,
      action: "Check out departures.",
      items: [
        {
          reservation: "RES-DEP-ZERO",
          guest_name: "Zero Bal Guest",
          check_in_date: "2026-11-22",
          check_out_date: "2026-11-24",
          nights: 2,
          room_type: "Deluxe",
          rate_plan: "BAR",
          rate: 12.5,
          room: "102",
          balance: 0,
          adults: 1,
          children: 0,
          source: "Direct",
        },
        {
          reservation: "RES-DEP-FILS",
          guest_name: "Fils Bal Guest",
          check_in_date: "2026-11-22",
          check_out_date: "2026-11-24",
          nights: 2,
          room_type: "Deluxe",
          rate_plan: "BAR",
          rate: 12.5,
          room: "103",
          balance: 0.005,
          adults: 1,
          children: 0,
          source: "Direct",
        },
      ],
    },
    {
      key: "open_tills",
      title: "Open tills",
      severity: "block",
      count: 1,
      action: "Close open cashier sessions.",
      items: [
        {
          session: "CS-1",
          cashier_id: "Desk-1",
          business_date: "2026-11-24",
          opened_at: "2026-11-24 08:00:00",
          opening_float: 50,
          transactions: 3,
          cash_taken: 12.5,
        },
      ],
    },
  ],
}

const RESERVATION_DETAIL_FIXTURE = {
  name: "RES-56",
  property: "PROP-A",
  status: "Confirmed",
  booking_type: "Individual",
  source: "Direct",
  check_in_date: "2026-11-24",
  check_out_date: "2026-11-26",
  nights: 2,
  adults: 2,
  children: 0,
  room_type: "RT-DLX",
  room_type_name: "Deluxe King",
  room: "PROP-A-101",
  room_number: "101",
  folio_name: "FOLIO-56",
  channel_review_needed: 1,
  channel_review_reason: "update_not_applied",
  channel_reviews: [{ name: "CR-1", rationale: "Channel dates changed", executed_at: "2026-11-24 10:15:00" }],
  late_arrival: 0,
  guest: { name: "G-56", full_name: "Ada Lovelace", phone: "+256700100056", stays: 1, vip: 1, blacklisted: 0 },
  money: { total: 120, paid: 20, due: 100, source: "booking", has_folio: true },
  warnings: { id_unverified: false, id_document_missing: false },
  actions: {
    can_amend: true,
    can_check_in: true,
    can_check_out: false,
    can_no_show: true,
    can_cancel: false,
    can_reinstate: false,
    can_reverse_check_in: false,
    can_set_rate: false,
  },
  night_rates: [],
  checkin_backfill: [],
}

describe("P56 OPERA hotkeys, CommandPalette room search, EndOfDayWizard & ReservationDetail responsive parity", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    propState.current = "PROP-A"
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)

    rpc.mockReset().mockImplementation(async (method: string) => {
      if (method === "kamra.api.property_locale") return KWD_LOCALE
      if (method === "kamra.settings.settings_schema") return { modules: [] }
      if (method === "kamra.tape_queue.room_metadata") {
        return {
          floors: [{ name: "1 - North Wing", sort_order: 10 }],
          rooms: {
            "PROP-A-101": {
              name: "PROP-A-101", room_number: "101", room_type: "RT-DLX", room_type_name: "Deluxe King",
              floor: "1 - North Wing", wing: "North Wing", room_view: "Sea", bed_type: "King",
              housekeeping_status: "Inspected", occupancy_status: "Vacant",
              smoking_allowed: 0, air_conditioning: "AC", kitchen: 0, amenities: ["WiFi"],
            },
          },
        }
      }
      if (method === "kamra.api.guest_search") return []
      if (method === "kamra.api.find_reservations") return []
      if (method === "kamra.api.find_invoices") {
        return [{ name: "INV-1", invoice_number: "INV-001", guest_name: "Ada", grand_total: 12.5, status: "Open" }]
      }
      if (method === "kamra.night_audit.eod_session") return EOD_FIXTURE
      if (method === "kamra.api.reservation_detail") return RESERVATION_DETAIL_FIXTURE
      if (method === "kamra.api.linked_records") return { folios: [], reservations: [] }
      if (method === "kamra.api.stay_instructions") return []
      if (method === "kamra.api.reservation_changes") return []
      if (method === "kamra.api.booking_options") {
        return { room_types: [], rate_plans: [], meal_plans: [], sources: [], countries: [], today: "2026-11-24" }
      }
      if (method === "kamra.checkin.checkin_flow") {
        return {
          reservation: {
            name: "RES-56", status: "Confirmed", guest: "G-56", guest_name: "Ada Lovelace",
            room_type: "RT-DLX", check_in_date: "2026-11-24", check_out_date: "2026-11-26",
            nights: 2, adults: 2, children: 0, vip: 1, source: "Direct", special_requests: "",
          },
          guest: {
            name: "G-56", full_name: "Ada Lovelace", phone: "+256700100056", email: null,
            nationality: null, id_type: null, id_number: null, id_on_file: true,
            id_status: "valid", id_expiry_date: null, id_used_count: 1, address: "", has_photo: false,
          },
          vocabulary: { id_types: [] },
          steps: [],
          missing_required: [],
          pending_backfill: [],
          rooms: [],
          request_presets: [],
          ids: { ok: true, missing: [], online: false, precheckin_status: "Not Started", precheckin_on: null, occupants: [] },
          billing: {
            payer: "Guest", source: "Direct", channel: null, ota_ref: null, pay_at_hotel: 1,
            company: null, company_name: null, billing_rules: [], group_booking: null,
            group_name: null, stay_total: 120, advance_paid: 20, balance_due: 100, options: ["Guest"],
          },
          room: {
            room: "PROP-A-101", room_number: "101", housekeeping_status: "Inspected",
            entire_place: null, room_type_name: "Deluxe King", assigned_by: null, assigned_at: null,
          },
          deposit: {
            applies: false, satisfied: true,
            security: { required: 0, held: 0, due: 0, blocks_checkout: false, status: "None" },
            advance: { required: 0, paid: 0, due: 0, can_take: false },
            security_due: 0, advance_due: 0,
          },
          grc: { signed: false },
          keys: { count: 1, cards: "", issued_by: null, adapter: "", adapter_label: "", manual: true },
          stay_info: {
            guest_wifi_name: "", guest_wifi_password: "", breakfast_hours: "",
            emergency_number: "", guest_info_note: "", checkout_time: "11:00", configured: false,
          },
          room_charges: { allowed: true, recommend_block: false },
          till: { required: false, open: true, has_terminal: false },
          records: {},
        }
      }
      return []
    })

    await loadLocale()
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    localStorage.clear()
    vi.unstubAllGlobals()
  })

  it("handles Alt+N, Alt+C, F4 (on /tape vs skipped on /pos), Esc, and Enter fast-cashier safely", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/tape"]}>
          <LocationProbe />
          <CommandPalette />
          <input data-testid="text-box" />
          <button type="button" data-fast-cashier-submit="true" onClick={() => rpc("fast.cashier.submit", {})}>
            Settle
          </button>
        </MemoryRouter>,
      )
    })

    // Alt+N dispatches cancelable kamra:open-booking
    const openBookingEvents: Event[] = []
    const intercept = (e: Event) => {
      openBookingEvents.push(e)
      e.preventDefault()
    }
    window.addEventListener("kamra:open-booking", intercept)
    try {
      await act(async () => {
        window.dispatchEvent(new KeyboardEvent("keydown", { key: "n", code: "KeyN", altKey: true, bubbles: true }))
      })
      expect(openBookingEvents.length).toBe(1)
      expect(openBookingEvents[0].cancelable).toBe(true)

      // Typing inside an <input> must NOT fire Alt+N
      const input = host.querySelector<HTMLInputElement>('[data-testid="text-box"]')!
      input.focus()
      await act(async () => {
        input.dispatchEvent(new KeyboardEvent("keydown", { key: "n", code: "KeyN", altKey: true, bubbles: true }))
      })
      expect(openBookingEvents.length).toBe(1)
      input.blur()
    } finally {
      window.removeEventListener("kamra:open-booking", intercept)
    }

    // F4 on /tape opens CommandPalette in room search mode & shows OPERA shortcut legend
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "F4", bubbles: true }))
    })
    await vi.waitFor(() => {
      const dlg = host.querySelector<HTMLElement>('[role="dialog"]')
      expect(dlg).not.toBeNull()
      expect(dlg!.dataset.paletteMode).toBe("room")
      expect(dlg!.textContent).toContain("101")
      expect(dlg!.textContent).toContain("Deluxe King")
    })
    const legend = host.querySelector<HTMLElement>('[data-testid="opera-hotkey-legend"]')
    expect(legend).not.toBeNull()
    expect(legend!.textContent).toContain("Alt+N")
    expect(legend!.textContent).toContain("Alt+C")
    expect(legend!.textContent).toContain("F4")
    expect(legend!.textContent).toContain("Esc")

    // Invoice formatting inside CommandPalette uses KWD 3dp (fmtMoney)
    const searchInput = host.querySelector<HTMLInputElement>('[role="dialog"] input')!
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!
      setter.call(searchInput, "INV")
      searchInput.dispatchEvent(new Event("input", { bubbles: true }))
    })
    await vi.waitFor(() => {
      expect(host.querySelector('[role="dialog"]')?.textContent).toContain("KWD 12.500")
    }, { timeout: 3000 })

    // Escape closes CommandPalette
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }))
    })
    expect(host.querySelector('[role="dialog"]')).toBeNull()

    // Enter triggers [data-fast-cashier-submit] when focus is not in input/button
    await act(async () => {
      document.body.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }))
    })
    expect(rpc).toHaveBeenCalledWith("fast.cashier.submit", {})

    // Alt+C with no open ReservationDetail navigates to /in-house?view=arrivals
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "c", code: "KeyC", altKey: true, bubbles: true }))
    })
    await vi.waitFor(() => {
      expect(host.querySelector('[data-testid="loc"]')?.textContent).toBe("/in-house?view=arrivals")
    })
  })

  it("skips F4 room search on /pos so POS proceedToPay is never interfered with", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/pos"]}>
          <CommandPalette />
        </MemoryRouter>,
      )
    })
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "F4", bubbles: true }))
    })
    expect(host.querySelector('[role="dialog"]')).toBeNull()
  })

  it("renders EndOfDayWizard mobile stacked cards, 44px touch targets, rtl:rotate-180, and KWD 3dp precision + epsilon", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/PROP-A"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Alan Turing")
    })

    // Stacked mobile cards (md:hidden) alongside desktop table (hidden md:block)
    const mobileLists = host.querySelectorAll<HTMLElement>('[data-testid="eod-mobile-cards"]')
    const desktopTables = host.querySelectorAll<HTMLElement>('[data-testid="eod-desktop-table"]')
    expect(mobileLists.length).toBeGreaterThan(0)
    expect(desktopTables.length).toBeGreaterThan(0)
    expect(mobileLists[0].className).toContain("md:hidden")
    expect(desktopTables[0].className).toContain("hidden")
    expect(desktopTables[0].className).toContain("md:block")

    // Search input uses w-full sm:w-56 + min-h-11
    const search = host.querySelector<HTMLInputElement>('input[placeholder="Search"]')!
    expect(search.className).toContain("w-full")
    expect(search.className).toContain("sm:w-56")
    expect(search.className).toContain("min-h-11")

    // Touch buttons & counted-cash input carry min-h-11
    const checkInBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "Check in")!
    expect(checkInBtn.className).toContain("min-h-11")
    const countedInput = host.querySelector<HTMLInputElement>('input[aria-label="Counted cash"]')!
    expect(countedInput.className).toContain("min-h-11")

    // ArrowLeft back link carries rtl:rotate-180
    const backSvg = host.querySelector('a[href="/end-of-day"] svg')!
    expect(backSvg.getAttribute("class") ?? "").toContain("rtl:rotate-180")

    // KWD 3dp formatting (12.500) AND 0.005 KWD is NOT zero-balance (only 1 zero-balance departure, not 2!)
    expect(host.textContent).toContain("12.500")
    expect(host.textContent).toContain("Quick check out 1 zero-balance departures")
    expect(host.textContent).not.toContain("Quick check out 2 zero-balance departures")
  })

  it("renders ReservationDetail with 44px touch targets, KWD 3dp, ms-2 RTL, translated Finish check-in, and Alt+C check-in", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/reservations"]}>
          <CommandPalette />
          <ReservationDetail row={{ name: "RES-56" }} reload={vi.fn()} onClose={vi.fn()} />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Ada Lovelace")
    })

    // Exact 3dp KWD totals (never stripping .000)
    expect(host.textContent).toContain("KWD 120.000")
    expect(host.textContent).toContain("KWD 20.000")
    expect(host.textContent).toContain("KWD 100.000")

    // RTL logical ms-2 on channel review timestamp (zero ml-2)
    expect(host.innerHTML).not.toMatch(/\bml-2\b/)
    expect(host.innerHTML).toContain("ms-2")

    // 44px touch targets on primary action buttons, links, and late-arrival checkbox label
    const regLink = host.querySelector<HTMLAnchorElement>('a[href="/grc/RES-56"]')!
    expect(regLink.className).toContain("min-h-11")
    const quoteLink = host.querySelector<HTMLAnchorElement>('a[href="/booking-quote/RES-56"]')!
    expect(quoteLink.className).toContain("min-h-11")
    const checkInBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Check in"),
    )!
    expect(checkInBtn.className).toContain("min-h-11")

    // Source must wrap Finish check-in in translate(...)
    const resDetailSrc = fs.readFileSync(
      path.resolve(process.cwd(), "src/screens/ReservationDetail.tsx"),
      "utf8",
    )
    expect(resDetailSrc).toContain('translate("Finish check-in ({n})"')

    // Alt+C triggers kamra:shortcut-checkin -> ReservationDetail opens CheckInDialog
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "c", code: "KeyC", altKey: true, bubbles: true }))
    })
    await vi.waitFor(() => {
      expect(rpc).toHaveBeenCalledWith("kamra.checkin.checkin_flow", { reservation: "RES-56" })
    })
  })

  it("skips F4 room search inside input, textarea, select, and contenteditable targets", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/tape"]}>
          <CommandPalette />
          <input data-testid="f4-input" />
          <textarea data-testid="f4-textarea" />
          <select data-testid="f4-select">
            <option value="1">1</option>
          </select>
          <div data-testid="f4-ce" contentEditable="true">
            <span data-testid="f4-ce-child">Editable</span>
          </div>
        </MemoryRouter>,
      )
    })

    const targets = [
      host.querySelector<HTMLElement>('[data-testid="f4-input"]')!,
      host.querySelector<HTMLElement>('[data-testid="f4-textarea"]')!,
      host.querySelector<HTMLElement>('[data-testid="f4-select"]')!,
      host.querySelector<HTMLElement>('[data-testid="f4-ce"]')!,
      host.querySelector<HTMLElement>('[data-testid="f4-ce-child"]')!,
    ]

    for (const el of targets) {
      const synthetic = new KeyboardEvent("keydown", { key: "F4", bubbles: true })
      Object.defineProperty(synthetic, "target", { value: el })
      expect(matchOperaHotkey(synthetic, "/tape")).toBeNull()

      await act(async () => {
        el.dispatchEvent(new KeyboardEvent("keydown", { key: "F4", bubbles: true }))
      })
      expect(host.querySelector('[role="dialog"]')).toBeNull()
    }

    // Outside text inputs, F4 still opens room search mode
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "F4", bubbles: true }))
    })
    expect(host.querySelector<HTMLElement>('[role="dialog"]')?.dataset.paletteMode).toBe("room")
  })

  it("subscribes EndOfDayWizard and ReservationDetail to reactive useLocale() when property_locale resolves late (UGX 0dp -> KWD 3dp)", async () => {
    const UGX_LOCALE = {
      ...KWD_LOCALE,
      currency: "UGX",
      currency_symbol: "UGX ",
      locale: "en-UG",
    }

    // 1. Prime cache with UGX (0dp, eps=0.5) on PROP-UGX
    propState.current = "PROP-UGX"
    const baseImpl = rpc.getMockImplementation()!
    rpc.mockImplementation(async (method: string, params: unknown) => {
      if (method === "kamra.api.property_locale") return UGX_LOCALE
      return baseImpl(method, params)
    })
    await loadLocale()

    // 2. Defer property_locale so eod_session wins the race while cache is still UGX 0dp
    const localeResolvers: Array<(val: typeof KWD_LOCALE) => void> = []
    const requestedLocaleProps: string[] = []
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.property_locale") {
        requestedLocaleProps.push(String(params?.property ?? ""))
        return new Promise((resolve) => {
          localeResolvers.push(resolve)
        })
      }
      if (method === "kamra.night_audit.eod_session") {
        return Promise.resolve({ ...EOD_FIXTURE, property: "PROP-KWD", currency: undefined })
      }
      return baseImpl(method, params)
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/end-of-day/PROP-KWD"]}>
          <Routes>
            <Route path="/end-of-day/:property" element={<EndOfDayWizard />} />
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Alan Turing")
    })
    expect(propState.current).toBe("PROP-KWD")
    expect(requestedLocaleProps).toContain("PROP-KWD")
    // Still at UGX 0dp before KWD locale resolves: 0.005 < 0.5 falsely counts as zero-balance
    expect(host.textContent).toContain("Quick check out 2 zero-balance departures")
    expect(host.textContent).not.toContain("KWD 12.500")

    // Resolve KWD 3dp locale asynchronously — EndOfDayWizard must re-render immediately
    await act(async () => {
      while (localeResolvers.length > 0) localeResolvers.shift()!(KWD_LOCALE)
      await Promise.resolve()
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("KWD 12.500")
      expect(host.textContent).toContain("0.005")
      expect(host.textContent).toContain("Quick check out 1 zero-balance departures")
      expect(host.textContent).not.toContain("Quick check out 2 zero-balance departures")
    })

    // 3. Re-prime UGX 0dp and verify ReservationDetail re-renders reactively on late KWD locale resolution
    await act(async () => root.unmount())
    root = createRoot(host)
    rpc.mockImplementation(async (method: string, params: unknown) => {
      if (method === "kamra.api.property_locale") return UGX_LOCALE
      return baseImpl(method, params)
    })
    await loadLocale()

    const detailResolvers: Array<(val: typeof KWD_LOCALE) => void> = []
    rpc.mockImplementation((method: string, params: unknown) => {
      if (method === "kamra.api.property_locale") {
        return new Promise((resolve) => {
          detailResolvers.push(resolve)
        })
      }
      return baseImpl(method, params)
    })

    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/reservations"]}>
          <ReservationDetail row={{ name: "RES-56" }} reload={vi.fn()} onClose={vi.fn()} />
        </MemoryRouter>,
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("Ada Lovelace")
    })
    expect(host.textContent).not.toContain("KWD 120.000")

    await act(async () => {
      while (detailResolvers.length > 0) detailResolvers.shift()!(KWD_LOCALE)
      await Promise.resolve()
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("KWD 120.000")
      expect(host.textContent).toContain("KWD 20.000")
      expect(host.textContent).toContain("KWD 100.000")
    })
  })
})
