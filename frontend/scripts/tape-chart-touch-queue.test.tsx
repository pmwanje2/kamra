// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import TapeChart from "../src/screens/TapeChart"
import { loadLocale } from "../src/lib/money"
import type { ShellContext } from "../src/AppShell"

const { rpc, openBookingSpy, propState, authState } = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return {
    rpc: vi.fn(),
    openBookingSpy: vi.fn(),
    propState: { current: "PROP-A" },
    authState: { roles: ["Front Desk", "Housekeeping Supervisor", "Hotel Admin"] },
  }
})

vi.mock("../src/lib/api", async (original) => ({
  ...await original<typeof import("../src/lib/api")>(),
  call: (method: string, params: unknown) => rpc(method, params),
  getCurrentProperty: () => propState.current,
  setHousekeepingStatus: (room: string, status: string, reason?: string) =>
    rpc("kamra.api.set_housekeeping_status", { room, status, reason }),
  amendStayPreview: (reservation: string, check_in_date: string, check_out_date: string) =>
    rpc("kamra.api.amend_stay_preview", { reservation, check_in_date, check_out_date }),
  amendStay: (reservation: string, check_in_date: string, check_out_date: string, confirm_price?: number) =>
    rpc("kamra.api.amend_stay", { reservation, check_in_date, check_out_date, confirm_price }),
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: authState.roles }),
}))

const TODAY = (() => {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`
})()

function shift(iso: string, days: number): string {
  const [y, m, d] = iso.split("-").map(Number)
  const dt = new Date(Date.UTC(y, m - 1, d + days))
  return dt.toISOString().slice(0, 10)
}

function setSelectValue(el: HTMLSelectElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value")?.set
  if (setter) setter.call(el, value)
  else el.value = value
  el.dispatchEvent(new Event("change", { bubbles: true }))
}

function pointerEvt(
  type: string,
  opts: { pointerType: string; pointerId?: number; clientX?: number; clientY?: number; button?: number },
): Event {
  const evt = new MouseEvent(type, {
    bubbles: true,
    cancelable: true,
    clientX: opts.clientX ?? 0,
    clientY: opts.clientY ?? 0,
    button: opts.button ?? 0,
  })
  Object.defineProperty(evt, "pointerType", { value: opts.pointerType })
  Object.defineProperty(evt, "pointerId", { value: opts.pointerId ?? 1 })
  return evt
}

const TAPE_DATES = Array.from({ length: 14 }, (_, i) => shift(TODAY, i))

function makeTapeFixture() {
  return {
    start: TODAY,
    dates: TAPE_DATES,
    rooms: [
      {
        name: "PROP-A-101",
        room_number: "101",
        room_type: "RT-DLX",
        room_type_name: "Deluxe King",
        floor: "1 - North Wing",
        housekeeping_status: "Inspected",
        bookings: [
          {
            name: "RES-1",
            room: "PROP-A-101",
            guest_name: "Ada Lovelace",
            status: "Confirmed" as const,
            check_in_date: TODAY,
            check_out_date: shift(TODAY, 2),
            is_day_use: 0 as const,
            vip: 1 as const,
            source: "Direct",
          },
        ],
        blocks: [],
      },
      {
        name: "PROP-A-102",
        room_number: "102",
        room_type: "RT-DLX",
        room_type_name: "Deluxe King",
        floor: "1 - North Wing",
        housekeeping_status: "Dirty",
        bookings: [],
        blocks: [],
      },
      {
        name: "PROP-A-201",
        room_number: "201",
        room_type: "RT-STD",
        room_type_name: "Standard Twin",
        floor: "2 - South Wing",
        housekeeping_status: "Ready",
        bookings: [],
        blocks: [],
      },
      {
        name: "PROP-A-202",
        room_number: "202",
        room_type: "RT-DLX",
        room_type_name: "Deluxe King",
        floor: "2 - South Wing",
        housekeeping_status: "Out of Service",
        bookings: [],
        blocks: [],
      },
    ],
    position: TAPE_DATES.map((date, idx) => ({
      date,
      sold: idx === 0 ? 1 : 0,
      capacity: 4,
      occupancy: idx === 0 ? 25 : 0,
      limit: 4,
      overbooked: false,
      premium_pct: 0,
      min_rate: idx === 0 ? 12.5 : 0,
    })),
    conflicts: [],
  }
}

const ROOM_METADATA = {
  floors: [
    { name: "1 - North Wing", sort_order: 10 },
    { name: "2 - South Wing", sort_order: 20 },
  ],
  rooms: {
    "PROP-A-101": {
      name: "PROP-A-101", room_number: "101", room_type: "RT-DLX", room_type_name: "Deluxe King",
      floor: "1 - North Wing", wing: "North Wing", room_view: "Sea", bed_type: "King",
      smoking_allowed: 0, air_conditioning: "AC", kitchen: 0, amenities: ["WiFi", "Balcony"],
      housekeeping_status: "Inspected", occupancy_status: "Vacant",
    },
    "PROP-A-102": {
      name: "PROP-A-102", room_number: "102", room_type: "RT-DLX", room_type_name: "Deluxe King",
      floor: "1 - North Wing", wing: "North Wing", room_view: "Sea", bed_type: "King",
      smoking_allowed: 0, air_conditioning: "AC", kitchen: 0, amenities: ["WiFi"],
      housekeeping_status: "Dirty", occupancy_status: "Vacant",
    },
    "PROP-A-201": {
      name: "PROP-A-201", room_number: "201", room_type: "RT-STD", room_type_name: "Standard Twin",
      floor: "2 - South Wing", wing: "South Wing", room_view: "Garden", bed_type: "Twin",
      smoking_allowed: 1, air_conditioning: "AC", kitchen: 1, amenities: ["WiFi"],
      housekeeping_status: "Ready", occupancy_status: "Vacant",
    },
    "PROP-A-202": {
      name: "PROP-A-202", room_number: "202", room_type: "RT-DLX", room_type_name: "Deluxe King",
      floor: "2 - South Wing", wing: "South Wing", room_view: "City", bed_type: "King",
      smoking_allowed: 0, air_conditioning: "AC", kitchen: 0, amenities: ["WiFi"],
      housekeeping_status: "Out of Service", occupancy_status: "Vacant",
    },
  },
}

const UNASSIGNED_QUEUE = {
  start: TODAY,
  days: 14,
  property: "PROP-A",
  items: [
    {
      name: "RES-Q1",
      guest: "G-Q1",
      guest_name: "Grace Hopper",
      vip: 1 as const,
      room_type: "RT-DLX",
      room_type_name: "Deluxe King",
      check_in_date: TODAY,
      check_out_date: shift(TODAY, 2),
      nights: 2,
      is_day_use: 0 as const,
      booking_type: "Individual",
      source: "Direct",
      special_requests: "High floor sea view",
    },
  ],
}

describe("P22 TapeChart touch drag, unassigned dock, filters, HK toggle, 390px cards, KWD 3dp & RTL", () => {
  let host: HTMLDivElement
  let root: Root
  let origElementFromPoint: typeof document.elementFromPoint | undefined

  beforeEach(async () => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    vi.stubGlobal("localStorage", jsdom.window.localStorage)
    propState.current = "PROP-A"
    authState.roles = ["Front Desk", "Housekeeping Supervisor", "Hotel Admin"]
    openBookingSpy.mockReset()
    origElementFromPoint = document.elementFromPoint
    host = document.createElement("div")
    document.body.appendChild(host)
    root = createRoot(host)

    rpc.mockReset().mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.property_locale") {
        return {
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
      }
      if (method === "kamra.api.tape_chart") return makeTapeFixture()
      if (method === "kamra.tape_queue.room_metadata") return ROOM_METADATA
      if (method === "kamra.tape_queue.unassigned_queue") return UNASSIGNED_QUEUE
      if (method === "kamra.tape_queue.suggest_queue_allocation") {
        return {
          date: TODAY,
          proposals: [
            {
              reservation: "RES-Q1",
              guest_name: "Grace Hopper",
              vip: 1,
              room_type: "RT-DLX",
              room_type_name: "Deluxe King",
              suggested_room: "PROP-A-101",
              room_number: "101",
              housekeeping_status: "Inspected",
              why: "Inspected, Sea view",
              alternatives: 1,
              needs_review: 1,
            },
          ],
          unfittable: [],
        }
      }
      if (method === "kamra.tape_queue.quick_assign_room") {
        return { ok: true, reservation: params?.reservation, room: params?.room, executed: [] }
      }
      if (method === "kamra.allocation.apply_allocation") return { executed: [], skipped: [] }
      if (method === "kamra.api.set_housekeeping_status") return { ok: true, room: params?.room, status: params?.status }
      if (method === "kamra.api.movable_rooms") {
        return [
          {
            name: "PROP-A-102", room_number: "102", room_type: "RT-DLX", room_type_name: "Deluxe King",
            floor: "1 - North Wing", housekeeping_status: "Dirty", free: true, same_type: true, current: false,
          },
        ]
      }
      if (method === "kamra.api.room_change_preview") {
        return {
          same_type: true, new_room_type_name: "Deluxe King", current_amount: 25, new_amount: 25,
          difference: 0, auto_price: true, in_house: false, can_set_rate: false,
        }
      }
      if (method === "kamra.api.amend_stay_preview") {
        return {
          current_amount: 12.5,
          new_amount: 25.375,
          difference: 12.875,
          needs_confirmation: true,
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
    if (origElementFromPoint) {
      document.elementFromPoint = origElementFromPoint
    } else {
      delete (document as unknown as Record<string, unknown>).elementFromPoint
    }
    vi.unstubAllGlobals()
  })

  async function mountTape(refreshKey = 0) {
    const shellCtx: ShellContext = {
      refreshKey,
      canBook: true,
      openBooking: openBookingSpy,
    }
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/tape"]}>
          <Routes>
            <Route element={<Outlet context={shellCtx} />}>
              <Route path="/tape" element={<TapeChart />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      )
    })
    await vi.waitFor(async () => {
      await act(() => new Promise((r) => setTimeout(r, 15)))
      expect(host.querySelector('[data-stay="RES-1"]')).not.toBeNull()
    }, { interval: 10, timeout: 5000 })
  }

  it("supports 300ms touch long-press drag-to-room and cancels on quick swipe (<300ms pan)", async () => {
    await mountTape()
    const bar = host.querySelector<HTMLButtonElement>('[data-stay="RES-1"]')!
    const room102 = host.querySelector<HTMLElement>('[data-room="PROP-A-102"]')!
    document.elementFromPoint = () => room102

    // Quick touch pan (< 300ms, dx = 35 > 8px): must cancel drag timer and NOT lift bar or open move dialog
    await act(async () => {
      bar.dispatchEvent(pointerEvt("pointerdown", { pointerType: "touch", pointerId: 11, clientX: 200, clientY: 100 }))
      window.dispatchEvent(pointerEvt("pointermove", { pointerType: "touch", pointerId: 11, clientX: 235, clientY: 100 }))
      await new Promise((r) => setTimeout(r, 340))
      window.dispatchEvent(pointerEvt("pointerup", { pointerType: "touch", pointerId: 11, clientX: 235, clientY: 100 }))
    })
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.movable_rooms", expect.anything())

    // Genuine >= 300ms touch long-press without exceeding 8px hysteresis -> lifts bar -> drop onto Room 102 opens RoomChangeDialog
    await act(async () => {
      bar.dispatchEvent(pointerEvt("pointerdown", { pointerType: "touch", pointerId: 12, clientX: 200, clientY: 100 }))
      await new Promise((r) => setTimeout(r, 340))
    })
    expect(bar.dataset.dragging).toBe("true")
    expect(bar.className).toContain("touch-none")

    await act(async () => {
      window.dispatchEvent(pointerEvt("pointermove", { pointerType: "touch", pointerId: 12, clientX: 202, clientY: 150 }))
      window.dispatchEvent(pointerEvt("pointerup", { pointerType: "touch", pointerId: 12, clientX: 202, clientY: 150 }))
    })
    await vi.waitFor(() => {
      expect(rpc).toHaveBeenCalledWith("kamra.api.movable_rooms", { reservation: "RES-1" })
    })
  })

  it("supports coarse-pointer enlarged end-handle touch extend and formats KWD 3dp in preview & hurdle", async () => {
    await mountTape()
    // Hurdle min_rate (12.5) must format as KWD 12.500 (3dp), not 12.5 or 13
    const posWithTitle = Array.from(host.querySelectorAll<HTMLElement>("[title]")).find((el) =>
      el.title.includes("hurdle"),
    )
    expect(posWithTitle?.title).toContain("KWD 12.500")

    const endHandle = host.querySelector<HTMLElement>('[data-stay="RES-1"] [data-handle="end"]')!
    expect(endHandle.className).toContain("[@media(pointer:coarse)]:w-6")

    await act(async () => {
      endHandle.dispatchEvent(pointerEvt("pointerdown", { pointerType: "touch", pointerId: 21, clientX: 300, clientY: 100 }))
      await new Promise((r) => setTimeout(r, 340))
      window.dispatchEvent(pointerEvt("pointermove", { pointerType: "touch", pointerId: 21, clientX: 364, clientY: 100 }))
      window.dispatchEvent(pointerEvt("pointerup", { pointerType: "touch", pointerId: 21, clientX: 364, clientY: 100 }))
    })
    await vi.waitFor(() => {
      expect(rpc).toHaveBeenCalledWith(
        "kamra.api.amend_stay_preview",
        expect.objectContaining({ reservation: "RES-1", check_out_date: shift(TODAY, 3) }),
      )
    })
    await vi.waitFor(() => {
      expect(host.textContent).toContain("KWD 12.500")
      expect(host.textContent).toContain("KWD 25.375")
      expect(host.textContent).toContain("KWD 12.875")
    })
  })

  it("renders sticky Unassigned Queue Dock with 1-click Inspected-first Auto-Assign & inline quick assign", async () => {
    await mountTape()
    const dock = host.querySelector<HTMLElement>('[data-testid="tape-unassigned-dock"]')
    expect(dock, "Unassigned queue dock must be rendered").not.toBeNull()
    expect(dock!.textContent).toContain("Grace Hopper")
    expect(dock!.textContent).toContain("High floor sea view")

    // Inline dropdown excludes Out of Service (202) and lists Inspected/Dirty assignable rooms
    const select = dock!.querySelector<HTMLSelectElement>('select[aria-label*="Grace Hopper"]')
    expect(select).not.toBeNull()
    const optionValues = Array.from(select!.options).map((o) => o.value).filter(Boolean)
    expect(optionValues).not.toContain("PROP-A-202")
    expect(optionValues).toContain("PROP-A-102")

    // Clicking quick assign invokes kamra.tape_queue.quick_assign_room
    await act(async () => {
      setSelectValue(select!, "PROP-A-102")
    })
    const assignBtn = dock!.querySelector<HTMLButtonElement>('[data-testid="quick-assign-RES-Q1"]')!
    await act(async () => assignBtn.click())
    expect(rpc).toHaveBeenCalledWith(
      "kamra.tape_queue.quick_assign_room",
      expect.objectContaining({ property: "PROP-A", reservation: "RES-Q1", room: "PROP-A-102" }),
    )

    // Drag-to-room from Unassigned Queue Dock onto a room row invokes quick_assign_room
    // and suppresses accidental text selection while dragging
    rpc.mockClear()
    const card = dock!.querySelector<HTMLElement>('[data-unassigned-reservation="RES-Q1"]')!
    expect(card.className.split(/\s+/)).toContain("select-none")
    const room102 = host.querySelector<HTMLElement>('[data-room="PROP-A-102"]')!
    document.elementFromPoint = () => room102
    await act(async () => {
      card.dispatchEvent(pointerEvt("pointerdown", { pointerType: "mouse", pointerId: 31, clientX: 500, clientY: 120 }))
      window.dispatchEvent(pointerEvt("pointermove", { pointerType: "mouse", pointerId: 31, clientX: 300, clientY: 160 }))
      window.dispatchEvent(pointerEvt("pointerup", { pointerType: "mouse", pointerId: 31, clientX: 300, clientY: 160 }))
    })
    expect(rpc).toHaveBeenCalledWith(
      "kamra.tape_queue.quick_assign_room",
      expect.objectContaining({ property: "PROP-A", reservation: "RES-Q1", room: "PROP-A-102" }),
    )

    // 1-click Auto-Assign uses kamra.tape_queue.suggest_queue_allocation (Inspected first)
    const autoBtn = dock!.querySelector<HTMLButtonElement>('[data-testid="dock-auto-assign"]')!
    await act(async () => autoBtn.click())
    await vi.waitFor(() => {
      expect(rpc).toHaveBeenCalledWith("kamra.tape_queue.suggest_queue_allocation", {
        property: "PROP-A",
        date: TODAY,
      })
    })
    expect(host.textContent).toContain("Inspected")
  })

  it("filters rooms by Floor, Wing, View, Bed, Smoking, and HK readiness with 1-click Reset", async () => {
    await mountTape()
    expect(host.querySelectorAll('[data-testid="tape-desktop-grid"] [data-room]').length).toBe(4)

    const floorSel = host.querySelector<HTMLSelectElement>('select[aria-label="Filter by floor"]')
    expect(floorSel).not.toBeNull()
    await act(async () => {
      setSelectValue(floorSel!, "2 - South Wing")
    })
    expect(host.querySelectorAll('[data-testid="tape-desktop-grid"] [data-room]').length).toBe(2)

    const hkSel = host.querySelector<HTMLSelectElement>('select[aria-label="Filter by housekeeping status"]')
    expect(hkSel).not.toBeNull()
    await act(async () => {
      setSelectValue(hkSel!, "Ready")
    })
    const remaining = Array.from(host.querySelectorAll<HTMLElement>('[data-testid="tape-desktop-grid"] [data-room]')).map(
      (n) => n.dataset.room,
    )
    expect(remaining).toEqual(["PROP-A-201"])

    // Open collapsible filters (Wing / View / Bed / Smoking) and click Reset
    const moreBtn = host.querySelector<HTMLButtonElement>('[data-testid="tape-filters-toggle"]')
    expect(moreBtn).not.toBeNull()
    await act(async () => moreBtn!.click())
    expect(host.querySelector('select[aria-label="Filter by wing"]')).not.toBeNull()
    expect(host.querySelector('select[aria-label="Filter by view"]')).not.toBeNull()
    expect(host.querySelector('select[aria-label="Filter by bed type"]')).not.toBeNull()

    const resetBtn = host.querySelector<HTMLButtonElement>('[data-testid="tape-filter-reset"]')
    expect(resetBtn).not.toBeNull()
    await act(async () => resetBtn!.click())
    expect(host.querySelectorAll('[data-testid="tape-desktop-grid"] [data-room]').length).toBe(4)
  })

  it("toggles inline HK status and invokes set_housekeeping_status", async () => {
    await mountTape()
    const toggle = host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-102"]')
    expect(toggle).not.toBeNull()
    await act(async () => toggle!.click())
    const cleanOpt = host.querySelector<HTMLButtonElement>(
      '[data-testid="hk-popover-PROP-A-102"] [data-hk-option="Clean"]',
    )
    expect(cleanOpt).not.toBeNull()
    await act(async () => cleanOpt!.click())
    expect(rpc).toHaveBeenCalledWith("kamra.api.set_housekeeping_status", {
      room: "PROP-A-102",
      status: "Clean",
      reason: undefined,
    })
  })

  it("renders stacked 390px mobile room cards (md:hidden) with 44px touch targets and RTL-logical sticky grid", async () => {
    await mountTape()
    const desktopGrid = host.querySelector<HTMLElement>('[data-testid="tape-desktop-grid"]')
    const mobileCards = host.querySelector<HTMLElement>('[data-testid="tape-mobile-cards"]')
    expect(desktopGrid).not.toBeNull()
    expect(mobileCards).not.toBeNull()
    expect(desktopGrid!.className).toContain("hidden")
    expect(desktopGrid!.className).toContain("md:block")
    expect(mobileCards!.className).toContain("md:hidden")

    const mobileHk = mobileCards!.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-mobile-PROP-A-102"]')
    expect(mobileHk).not.toBeNull()
    expect(mobileHk!.className).toContain("min-h-11")
    expect(mobileHk!.className).toContain("min-w-11")

    // Sticky start-0 room column + zero legacy border-l / text-right in grid
    const roomHeader = host.querySelector<HTMLElement>('[data-testid="tape-room-header"]')
    expect(roomHeader?.className).toContain("sticky")
    expect(roomHeader?.className).toContain("start-0")
    expect(desktopGrid!.innerHTML).not.toMatch(/\bborder-l\b/)
    expect(desktopGrid!.innerHTML).toContain("border-s")
  })

  it("guards empty property on cold boot, elevates active HK popover stacking context, and avoids w-full on inline filter selects", async () => {
    // 1. Cold boot with empty current property must not fire empty-property RPCs
    propState.current = ""
    rpc.mockClear()
    const shellCtx: ShellContext = { refreshKey: 0, canBook: true, openBooking: openBookingSpy }
    await act(async () => {
      root.render(
        <MemoryRouter initialEntries={["/tape"]}>
          <Routes>
            <Route element={<Outlet context={shellCtx} />}>
              <Route path="/tape" element={<TapeChart />} />
            </Route>
          </Routes>
        </MemoryRouter>,
      )
    })
    await act(() => new Promise((r) => setTimeout(r, 30)))
    expect(rpc).not.toHaveBeenCalledWith("kamra.api.tape_chart", expect.anything())
    expect(rpc).not.toHaveBeenCalledWith("kamra.tape_queue.unassigned_queue", expect.anything())
    expect(rpc).not.toHaveBeenCalledWith("kamra.tape_queue.room_metadata", expect.anything())

    // 2. Once property resolves (AppShell bumps refreshKey), toolbar filter selects must be inline (w-auto without conflicting w-full)
    propState.current = "PROP-A"
    await mountTape(1)
    const floorSel = host.querySelector<HTMLSelectElement>('select[aria-label="Filter by floor"]')!
    expect(floorSel.className).toContain("w-auto")
    expect(floorSel.className.split(/\s+/)).not.toContain("w-full")

    // 3. Opening HK popover elevates its room row and sticky cell above sibling z-10 sticky cells
    const row102 = host.querySelector<HTMLElement>('[data-room="PROP-A-102"]')!
    const cell102 = host.querySelector<HTMLElement>('[data-testid="tape-room-cell-PROP-A-102"]')!
    const toggle102 = host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-102"]')!
    await act(async () => toggle102.click())
    expect(row102.className.split(/\s+/)).toContain("z-30")
    expect(cell102.className.split(/\s+/)).toContain("z-30")
  })

  it("opens HK popover upward on a 1-room last Room Type group and downward on the top row", async () => {
    await mountTape()

    // Last Room Type group ("Standard Twin") has only 1 room (PROP-A-201, rIdx === 0)
    const toggle201 = host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-201"]')!
    await act(async () => toggle201.click())
    const pop201 = host.querySelector<HTMLElement>('[data-testid="hk-popover-PROP-A-201"]')!
    expect(pop201).not.toBeNull()
    expect(pop201.className.split(/\s+/)).toContain("bottom-full")
    expect(pop201.className.split(/\s+/)).not.toContain("top-full")

    // Top row (PROP-A-101) opens downward cleanly
    const toggle101 = host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-101"]')!
    await act(async () => toggle101.click())
    const pop101 = host.querySelector<HTMLElement>('[data-testid="hk-popover-PROP-A-101"]')!
    expect(pop101).not.toBeNull()
    expect(pop101.className.split(/\s+/)).toContain("top-full")
    expect(pop101.className.split(/\s+/)).not.toContain("bottom-full")
  })

  it("gates HK_TOGGLE_OPTIONS.supervisorOnly options for non-supervisors on desktop and mobile", async () => {
    authState.roles = ["Housekeeping"]
    await mountTape()

    // Desktop HK popover hides supervisor-only options (Inspected, Ready, Out of Order)
    const desktopToggle = host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-102"]')!
    await act(async () => desktopToggle.click())
    const desktopPop = host.querySelector<HTMLElement>('[data-testid="hk-popover-PROP-A-102"]')!
    expect(desktopPop.querySelector('[data-hk-option="Clean"]')).not.toBeNull()
    expect(desktopPop.querySelector('[data-hk-option="Dirty"]')).not.toBeNull()
    expect(desktopPop.querySelector('[data-hk-option="Inspected"]')).toBeNull()
    expect(desktopPop.querySelector('[data-hk-option="Ready"]')).toBeNull()
    expect(desktopPop.querySelector('[data-hk-option="Out of Order"]')).toBeNull()
    expect(desktopPop.querySelector('input[placeholder="Out of Order reason"]')).toBeNull()

    // Mobile cards HK menu hides supervisor-only options too
    const mobileCard = host.querySelector<HTMLElement>('[data-mobile-room="PROP-A-102"]')!
    const mobileToggle = mobileCard.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-mobile-PROP-A-102"]')!
    await act(async () => mobileToggle.click())
    const mobileBtnTexts = Array.from(mobileCard.querySelectorAll("button")).map((b) => b.textContent?.trim())
    expect(mobileBtnTexts).toContain("Clean")
    expect(mobileBtnTexts).toContain("Dirty")
    expect(mobileBtnTexts).not.toContain("Inspected")
    expect(mobileBtnTexts).not.toContain("Ready")
    expect(mobileBtnTexts).not.toContain("Out of Order")
    expect(mobileCard.querySelector('input[placeholder="Out of Order reason"]')).toBeNull()

    // Supervisor role sees all 5 statuses on both desktop and mobile
    await act(async () => root.unmount())
    root = createRoot(host)
    authState.roles = ["Front Desk"]
    await mountTape()
    await act(async () => {
      host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-PROP-A-102"]')!.click()
      host.querySelector<HTMLButtonElement>('[data-testid="hk-toggle-mobile-PROP-A-102"]')!.click()
    })
    const supPop = host.querySelector<HTMLElement>('[data-testid="hk-popover-PROP-A-102"]')!
    expect(supPop.querySelector('[data-hk-option="Inspected"]')).not.toBeNull()
    expect(supPop.querySelector('[data-hk-option="Ready"]')).not.toBeNull()
    expect(supPop.querySelector('[data-hk-option="Out of Order"]')).not.toBeNull()
    const supMobileCard = host.querySelector<HTMLElement>('[data-mobile-room="PROP-A-102"]')!
    const supMobileTexts = Array.from(supMobileCard.querySelectorAll("button")).map((b) => b.textContent?.trim())
    expect(supMobileTexts).toContain("Inspected")
    expect(supMobileTexts).toContain("Ready")
    expect(supMobileTexts).toContain("Out of Order")
  })

  it("re-renders TapeChart hurdle currency precision reactively via useLocale() when property_locale resolves late", async () => {
    const UGX_LOCALE = {
      currency: "UGX",
      currency_symbol: "UGX ",
      locale: "en-UG",
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
    const KWD_LOCALE = {
      ...UGX_LOCALE,
      currency: "KWD",
      currency_symbol: "KWD ",
      locale: "en-GB",
    }

    const baseImpl = rpc.getMockImplementation()!
    rpc.mockImplementation(async (method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.property_locale") return UGX_LOCALE
      return baseImpl(method, params)
    })
    await loadLocale()

    const resolvers: Array<(val: typeof KWD_LOCALE) => void> = []
    rpc.mockImplementation((method: string, params: Record<string, unknown>) => {
      if (method === "kamra.api.property_locale") {
        return new Promise((resolve) => {
          resolvers.push(resolve)
        })
      }
      return baseImpl(method, params)
    })

    await mountTape(0)
    const getHurdleTitle = () =>
      Array.from(host.querySelectorAll<HTMLElement>("[title]")).find((el) => el.title.includes("hurdle"))?.title ?? ""
    expect(getHurdleTitle()).toContain("UGX")
    expect(getHurdleTitle()).not.toContain("KWD 12.500")

    // Resolve KWD locale asynchronously without bumping ShellContext refreshKey
    await act(async () => {
      while (resolvers.length > 0) resolvers.shift()!(KWD_LOCALE)
      await Promise.resolve()
    })
    await vi.waitFor(() => {
      expect(getHurdleTitle()).toContain("KWD 12.500")
    })
  })
})
