// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import HousekeepingBoard from "../src/screens/HousekeepingBoard"
import HkApp from "../src/screens/HkApp"
import TapeChart from "../src/screens/TapeChart"

const { callMock, setHkMock } = vi.hoisted(() => ({
  callMock: vi.fn(),
  setHkMock: vi.fn(),
}))

vi.mock("../src/lib/api", () => ({
  amendStay: vi.fn(),
  amendStayPreview: vi.fn(),
  call: (...args: unknown[]) => callMock(...args),
  getCurrentProperty: () => "PROP-1",
  isNetworkError: () => false,
  logout: vi.fn(),
  setHousekeepingStatus: (...args: unknown[]) => setHkMock(...args),
  uploadTo: vi.fn(),
  whoami: vi.fn().mockResolvedValue({ user: "hk@hotel.test", roles: ["Housekeeping"] }),
}))

vi.mock("../src/lib/realtime", () => ({
  subscribeRealtime: () => () => undefined,
  useRealtime: () => undefined,
}))

vi.mock("react-router-dom", async (importOriginal) => {
  const mod = await importOriginal<typeof import("react-router-dom")>()
  return {
    ...mod,
    useOutletContext: () => ({
      refreshKey: 0,
      canBook: true,
      openBooking: vi.fn(),
      roles: ["Front Desk", "Hotel Admin"],
      property: "PROP-1",
    }),
  }
})

describe("P35: Out of Service (OS) & Pickup in HousekeepingBoard, HkApp, and TapeChart", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    callMock.mockReset()
    setHkMock.mockReset().mockResolvedValue({ ok: true })
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 15)))

  it("HousekeepingBoard filters and renders Pickup (indigo) and Out of Service (orange) rooms distinctly from Out of Order", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.api.room_board") {
        return {
          can_supervise: true,
          floors: [{ name: "1", sort_order: 1 }],
          room_types: [{ name: "STD", room_type_name: "Standard" }],
          rooms: [
            {
              name: "PROP-1-101",
              room_number: "101",
              room_type: "STD",
              floor: "1",
              housekeeping_status: "Pickup",
              occupancy_status: "Vacant",
              guest_name: null,
              due_out: false,
              clean_tasks: [],
              task: null,
            },
            {
              name: "PROP-1-102",
              room_number: "102",
              room_type: "STD",
              floor: "1",
              housekeeping_status: "Out of Service",
              occupancy_status: "Vacant",
              guest_name: null,
              due_out: false,
              clean_tasks: [],
              task: null,
              os_oo_reason: "Touch-up paint",
              os_oo_until: "2026-11-27",
            },
            {
              name: "PROP-1-103",
              room_number: "103",
              room_type: "STD",
              floor: "1",
              housekeeping_status: "Out of Order",
              occupancy_status: "Vacant",
              guest_name: null,
              due_out: false,
              clean_tasks: [],
              task: null,
            },
          ],
        }
      }
      if (method === "kamra.api.hk_staff") return []
      return []
    })

    await act(async () => root.render(<HousekeepingBoard />))
    await settle()

    // Filter toolbar includes both Pickup and Out of Service chips
    const toolbar = host.querySelector('[role="toolbar"]')
    expect(toolbar?.textContent).toContain("Pickup")
    expect(toolbar?.textContent).toContain("Out of Service")
    expect(toolbar?.textContent).toContain("Out of Order")

    // Clicking Pickup filter shows 101 and hides 102/103
    const pickupFilter = Array.from(toolbar!.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Pickup"),
    )
    expect(pickupFilter).toBeDefined()
    await act(async () => pickupFilter!.click())
    await settle()
    expect(host.textContent).toContain("101")
    expect(host.textContent).not.toContain("102")

    // Clicking Out of Service filter shows 102 and its reason
    const osFilter = Array.from(toolbar!.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Out of Service"),
    )
    expect(osFilter).toBeDefined()
    await act(async () => osFilter!.click())
    await settle()
    expect(host.textContent).toContain("102")
    expect(host.textContent).toContain("Touch-up paint")
    expect(host.textContent).not.toContain("103")
  })

  it("HkApp renders Pickup and Out of Service room cards cleanly in the mobile Rooms tab", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.api.hk_queue") {
        return {
          date: "2026-11-24",
          tasks: [],
          rooms: [
            {
              name: "PROP-1-101",
              room_number: "101",
              housekeeping_status: "Pickup",
              occupancy_status: "Vacant",
              arrival_today: false,
            },
            {
              name: "PROP-1-102",
              room_number: "102",
              housekeeping_status: "Out of Service",
              occupancy_status: "Vacant",
              arrival_today: false,
            },
          ],
        }
      }
      return []
    })

    await act(async () => root.render(<HkApp />))
    await settle()

    // Switch to Rooms tab
    const roomsNav = Array.from(host.querySelectorAll("nav button")).find((b) =>
      b.textContent?.includes("Rooms"),
    )
    expect(roomsNav).toBeDefined()
    await act(async () => roomsNav!.click())
    await settle()

    expect(host.textContent).toContain("101")
    expect(host.textContent).toContain("Pickup")
    expect(host.textContent).toContain("102")
    expect(host.textContent).toContain("Out of Service")
    // Pickup tile uses indigo styling and OS tile uses orange styling (not undefined class)
    const pickupBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("101"))
    const osBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.includes("102"))
    expect(pickupBtn?.className).toMatch(/indigo/)
    expect(osBtn?.className).toMatch(/orange/)
  })

  it("TapeChart displays Pickup and Out of Service styling cleanly and blocks new-booking clicks on Out of Service rooms", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.api.tape_chart") {
        return {
          start: "2099-11-24",
          dates: ["2099-11-24", "2099-11-25"],
          position: [
            { date: "2099-11-24", sold: 0, capacity: 2, occupancy: 0, limit: 2, overbooked: false, premium_pct: 0, min_rate: 0 },
            { date: "2099-11-25", sold: 0, capacity: 2, occupancy: 0, limit: 2, overbooked: false, premium_pct: 0, min_rate: 0 },
          ],
          conflicts: [],
          rooms: [
            {
              name: "PROP-1-101",
              room_number: "101",
              room_type: "STD",
              room_type_name: "Standard",
              floor: "1",
              housekeeping_status: "Pickup",
              bookings: [],
              blocks: [],
            },
            {
              name: "PROP-1-102",
              room_number: "102",
              room_type: "STD",
              room_type_name: "Standard",
              floor: "1",
              housekeeping_status: "Out of Service",
              bookings: [],
              blocks: [],
            },
          ],
        }
      }
      return {}
    })

    await act(async () =>
      root.render(
        <MemoryRouter>
          <TapeChart />
        </MemoryRouter>,
      ),
    )
    await settle()

    const pickupRow = host.querySelector('[data-room="PROP-1-101"]')
    const osRow = host.querySelector('[data-room="PROP-1-102"]')
    expect(pickupRow?.textContent).toContain("Pickup")
    expect(pickupRow?.innerHTML).toMatch(/indigo/)
    expect(osRow?.textContent).toContain("Out of Service")
    expect(osRow?.innerHTML).toMatch(/orange/)
    // Out of Service row must NOT offer clickable "New booking" cells
    expect(osRow?.querySelectorAll('button[aria-label="New booking"]').length).toBe(0)
    // Pickup row CAN still be booked
    expect(pickupRow?.querySelectorAll('button[aria-label="New booking"]').length).toBeGreaterThan(0)
  })
})
