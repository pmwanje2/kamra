// @vitest-environment jsdom
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import AlertBanner from "../src/components/AlertBanner"
import CheckInDialog from "../src/components/CheckInDialog"
import RoomDrawer, { HK_TONE } from "../src/components/RoomDrawer"
import TracesPanel from "../src/components/TracesPanel"
import HousekeepingBoard from "../src/screens/HousekeepingBoard"

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const frontendRoot = path.resolve(__dirname, "..")

const { callMock, setHkMock } = vi.hoisted(() => ({
  callMock: vi.fn(),
  setHkMock: vi.fn(),
}))

vi.mock("../src/lib/api", () => ({
  call: (...args: unknown[]) => callMock(...args),
  getCurrentProperty: () => "PROP-1",
  newIdempotencyKey: () => "idem-r2",
  setHousekeepingStatus: (...args: unknown[]) => setHkMock(...args),
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Hotel Admin"] }),
}))

vi.mock("../src/lib/realtime", () => ({
  subscribeRealtime: () => () => undefined,
  useRealtime: () => undefined,
}))

function darkModeContrastViolations(className: string): string[] {
  const tokens = className.split(/\s+/).filter(Boolean)
  const violations: string[] = []
  const hasDarkText = tokens.some((t) => /^dark:text-/.test(t))
  const hasDarkBg = tokens.some((t) => /^dark:bg-/.test(t))

  for (const tok of tokens) {
    if (
      /^text-(rose|amber|emerald|sky|orange|indigo|teal)-950$/.test(tok) &&
      !hasDarkText
    ) {
      violations.push(`${tok} stays dark in .dark without a dark:text-* override`)
    }
    if (/^bg-(orange|indigo|teal)-50(?:\/\d+)?$/.test(tok) && !hasDarkBg) {
      violations.push(`${tok} stays bright pastel in .dark without a dark:bg-* override`)
    }
  }
  return violations
}

function baseCheckinFlow(overrides: Record<string, unknown> = {}) {
  return {
    reservation: {
      name: "RES-R2-1",
      status: "Confirmed",
      guest: "GUEST-1",
      guest_name: "Claire Moreau",
      room_type: "STD",
      check_in_date: "2026-11-25",
      check_out_date: "2026-11-27",
      nights: 2,
      adults: 2,
      children: 0,
      vip: 0,
      source: "Direct",
      special_requests: "",
    },
    guest: {
      name: "GUEST-1",
      full_name: "Claire Moreau",
      phone: "+1 415 555 0144",
      email: "claire@example.com",
      nationality: null,
      id_type: null,
      id_number: null,
      id_on_file: true,
      id_status: "valid",
      id_expiry_date: null,
      id_used_count: 1,
      address: "",
      has_photo: false,
    },
    vocabulary: { id_types: ["Passport"] },
    steps: [
      {
        step_key: "assign_room",
        step_type: "action",
        title: "Room",
        instruction: "Assign room",
        prompt_to_guest: "",
        is_blocking: 1,
        relevant: 1,
        state: "todo",
        done_by: null,
        done_at: null,
      },
    ],
    missing_required: [],
    pending_backfill: [],
    rooms: [],
    request_presets: [],
    ids: {
      ok: true,
      missing: [],
      online: false,
      precheckin_status: "Not Started",
      precheckin_on: null,
      occupants: [],
    },
    billing: {
      payer: "Guest",
      source: "Direct",
      channel: null,
      ota_ref: null,
      pay_at_hotel: 0,
      company: null,
      company_name: null,
      billing_rules: [],
      group_booking: null,
      group_name: null,
      stay_total: 400,
      advance_paid: 0,
      balance_due: 400,
      options: ["Guest"],
    },
    room: {
      room: null,
      room_number: null,
      housekeeping_status: null,
      entire_place: null,
      room_type_name: "Standard King",
      assigned_by: null,
      assigned_at: null,
    },
    deposit: {
      applies: false,
      satisfied: true,
      security: { status: "None", required: 0, held: 0, due: 0 },
      advance: { can_take: false, due: 0, paid: 0 },
      security_due: 0,
      advance_due: 0,
    },
    grc: { signed: true },
    keys: {
      count: 1,
      cards: "",
      issued_by: null,
      adapter: "manual",
      adapter_label: "Manual",
      manual: true,
    },
    stay_info: {
      guest_wifi_name: "",
      guest_wifi_password: "",
      breakfast_hours: "",
      emergency_number: "",
      guest_info_note: "",
      checkout_time: "11:00",
      configured: false,
    },
    room_charges: { allowed: true, recommend_block: false },
    till: { open: true, can_open: true, who_can_open: [] },
    records: {},
    alerts: [],
    ...overrides,
  }
}

describe("R2 fixes: dark-mode contrast, trace time formatting, and clean i18n scope", () => {
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

  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 20)))

  it("registers complete French (fr) locale in languages.json and translates PR #58 keys in locales/fr.json", () => {
    const languages = JSON.parse(
      fs.readFileSync(path.join(frontendRoot, "src/i18n/languages.json"), "utf8"),
    ) as { code: string }[]
    expect(languages.some((l) => l.code === "fr")).toBe(true)

    const frPath = path.join(frontendRoot, "src/i18n/locales/fr.json")
    expect(fs.existsSync(frPath)).toBe(true)
    const fr = JSON.parse(fs.readFileSync(frPath, "utf8")) as Record<string, string>

    const p4P35Keys = [
      "Traces & Instructions",
      "Add trace",
      "Due Today & Overdue",
      "Add alert",
      "Blocking",
      "Clear / Resolve",
      "A resolution reason is required to clear a blocking alert.",
      "Resolve blocking alerts before check-in.",
      "Resolve blocking alerts before check-out.",
      "Out of Service",
      "Pickup",
      "Expected return: {date}",
      "This room is Out of Service and cannot be assigned. Pick another room first.",
      "This room is flagged for quick touch-up (Pickup). Confirm with housekeeping before handing over keys.",
    ]
    for (const key of p4P35Keys) {
      expect(typeof fr[key], `fr.json missing key: ${key}`).toBe("string")
      expect(fr[key].trim().length, `fr.json empty translation: ${key}`).toBeGreaterThan(0)
      expect(fr[key], `fr.json untranslated English fallback: ${key}`).not.toBe(key)
    }
  })

  it("RoomDrawer HK_TONE uses theme-aware text colors", () => {
    for (const [status, cls] of Object.entries(HK_TONE)) {
      expect(
        darkModeContrastViolations(cls),
        `HK_TONE[${status}] has dark-mode contrast violations`,
      ).toEqual([])
    }
  })

  it("RoomDrawer OS and OO status callouts provide dark-mode surfaces and high-contrast text", async () => {

    for (const variant of [
      {
        name: "PROP-1-101",
        number: "101",
        status: "Out of Service" as const,
        reason: "Balcony railing touch-up",
        until: "2026-11-27",
      },
      {
        name: "PROP-1-102",
        number: "102",
        status: "Out of Order" as const,
        reason: "Main pipe leak",
        until: "2026-11-30",
      },
    ]) {
      callMock.mockImplementation(async (method: string) => {
        if (method === "kamra.rooms.room_detail") {
          return {
            date: "2026-11-25",
            room: {
              name: variant.name,
              property: "PROP-1",
              room_number: variant.number,
              room_type: "STD",
              room_type_name: "Standard King",
              floor: "1",
              housekeeping_status: variant.status,
              occupancy_status: "Vacant",
              os_oo_reason: variant.reason,
              os_oo_until: variant.until,
              os_oo_set_by: "sup@hotel.test",
              notes: null,
            },
            room_type: { name: "STD", room_type_name: "Standard King" },
            photos: [],
            stay: null,
            next_arrival: null,
            blocks: [],
            tasks: [],
            can: { supervise: true, block: true, desk: true, money: true },
          }
        }
        return {}
      })

      await act(async () =>
        root.render(
          <MemoryRouter>
            <RoomDrawer
              room={variant.name}
              onClose={() => undefined}
              onChanged={() => undefined}
              onCheckOut={() => undefined}
              onCheckIn={() => undefined}
              onMove={() => undefined}
            />
          </MemoryRouter>,
        ),
      )
      await settle()

      const reasonNode = Array.from(host.querySelectorAll("div")).find(
        (el) => el.textContent === variant.reason,
      )
      expect(reasonNode, `Expected ${variant.status} reason box`).toBeDefined()
      const callout = reasonNode!.parentElement!
      expect(
        darkModeContrastViolations(callout.className),
        `RoomDrawer ${variant.status} callout dark-mode contrast violation`,
      ).toEqual([])
      expect(callout.className).toMatch(/bg-(?:notice|danger)-subtle/)
    }
  })

  it("HousekeepingBoard Inspected and all status tiles use theme-aware text colors", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.api.room_board") {
        return {
          can_supervise: true,
          floors: [{ name: "1", sort_order: 1 }],
          room_types: [{ name: "STD", room_type_name: "Standard" }],
          rooms: [
            { name: "PROP-1-101", room_number: "101", room_type: "STD", floor: "1", housekeeping_status: "Out of Service", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
            { name: "PROP-1-102", room_number: "102", room_type: "STD", floor: "1", housekeeping_status: "Out of Order", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
            { name: "PROP-1-103", room_number: "103", room_type: "STD", floor: "1", housekeeping_status: "Clean", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
            { name: "PROP-1-104", room_number: "104", room_type: "STD", floor: "1", housekeeping_status: "Pickup", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
            { name: "PROP-1-105", room_number: "105", room_type: "STD", floor: "1", housekeeping_status: "Inspected", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
            { name: "PROP-1-106", room_number: "106", room_type: "STD", floor: "1", housekeeping_status: "Dirty", occupancy_status: "Vacant", guest_name: null, due_out: false, clean_tasks: [], task: null },
          ],
        }
      }
      if (method === "kamra.api.hk_staff") return []
      return []
    })

    await act(async () => root.render(<HousekeepingBoard />))
    await settle()

    for (const num of ["101", "102", "103", "104", "105", "106"]) {
      const card = Array.from(host.querySelectorAll("button")).find((b) =>
        b.textContent?.includes(num),
      )
      expect(card, `Room ${num} card should render`).toBeDefined()
      expect(
        darkModeContrastViolations(card!.className),
        `HousekeepingBoard card ${num} dark-mode contrast violation`,
      ).toEqual([])
    }
  })

  it("sends only the edited phone after reviewing a stay-specific registration email", async () => {
    const flow = baseCheckinFlow()
    flow.steps[0].step_key = "guest_ids"
    flow.ids.online = true
    flow.guest.email = "arrival@example.test"
    callMock.mockImplementation(async (method: string) => method.startsWith("kamra.checkin.") ? flow : [])
    await act(async () => root.render(
      <CheckInDialog reservation={flow.reservation.name} onDone={() => undefined} onClose={() => undefined} />,
    ))
    await settle()
    expect(host.querySelector<HTMLInputElement>('input[type="email"]')?.value).toBe("arrival@example.test")
    const phone = host.querySelector<HTMLInputElement>('input[type="tel"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(phone, "+256700000917")
      phone.dispatchEvent(new Event("input", { bubbles: true }))
    })
    const next = Array.from(host.querySelectorAll("button")).find(b => b.textContent === "Done, continue")!
    await act(async () => next.click())
    expect(callMock).toHaveBeenCalledWith("kamra.checkin.save_guest_details", {
      reservation: flow.reservation.name, phone: "+256700000917",
    })
  })

  it("CheckInDialog room status badges and Pickup/OS notices maintain crisp dark-mode contrast", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.checkin.checkin_flow") {
        return baseCheckinFlow({
          rooms: [
            { name: "PROP-1-101", room_number: "101", housekeeping_status: "Inspected" },
            { name: "PROP-1-102", room_number: "102", housekeeping_status: "Clean" },
            { name: "PROP-1-103", room_number: "103", housekeeping_status: "Pickup" },
            { name: "PROP-1-104", room_number: "104", housekeeping_status: "Dirty" },
            { name: "PROP-1-105", room_number: "105", housekeeping_status: "Out of Service" },
            { name: "PROP-1-106", room_number: "106", housekeeping_status: "Out of Order" },
          ],
        })
      }
      return []
    })

    await act(async () =>
      root.render(
        <CheckInDialog
          reservation="RES-R2-1"
          onDone={() => undefined}
          onClose={() => undefined}
        />,
      ),
    )
    await settle()

    const pills = Array.from(host.querySelectorAll("span.rounded-full"))
    expect(pills.length).toBeGreaterThanOrEqual(6)
    for (const pill of pills) {
      expect(
        darkModeContrastViolations(pill.className),
        `CheckInDialog badge (${pill.textContent}) dark-mode contrast violation`,
      ).toEqual([])
    }

    // The assigned-room notices use the corresponding status text tokens.
    for (const [status, snippet] of [
      ["Pickup", "quick touch-up (Pickup)"],
      ["Out of Service", "Out of Service and cannot be assigned"],
    ] as const) {
      callMock.mockImplementation(async (method: string) => {
        if (method === "kamra.checkin.checkin_flow") {
          return baseCheckinFlow({
            room: {
              room: "PROP-1-104",
              room_number: "104",
              housekeeping_status: status,
              entire_place: null,
              room_type_name: "Standard King",
              assigned_by: "desk@hotel.test",
              assigned_at: "2026-11-25 08:00:00",
            },
          })
        }
        return []
      })

      await act(async () => root.unmount())
      root = createRoot(host)
      await act(async () =>
        root.render(
          <CheckInDialog
            reservation="RES-R2-1"
            onDone={() => undefined}
            onClose={() => undefined}
          />,
        ),
      )
      await settle()

      const notice = Array.from(host.querySelectorAll("p")).find((p) =>
        p.textContent?.includes(snippet),
      )
      expect(notice, `Expected ${status} notice`).toBeDefined()
      expect(notice!.classList).toContain(status === "Pickup" ? "text-indigo-ink" : "text-notice-ink")
    }
  })

  it("AlertBanner surfaces use theme-aware colors", async () => {
    await act(async () =>
      root.render(
        <AlertBanner
          trigger="Reservation"
          property="PROP-1"
          reservation="RES-R2-1"
          initialAlerts={[
            {
              name: "ALT-INFO-1",
              property: "PROP-1",
              trigger_area: "Reservation",
              severity: "Info",
              message: "Guest prefers top-floor quiet room",
              status: "Active",
            },
            {
              name: "ALT-WARN-1",
              property: "PROP-1",
              trigger_area: "Reservation",
              severity: "Warning",
              message: "Allergic to down feathers",
              status: "Active",
            },
            {
              name: "ALT-BLOCK-1",
              property: "PROP-1",
              trigger_area: "Reservation",
              severity: "Blocking",
              message: "Verify passport on arrival",
              status: "Active",
            },
          ]}
        />,
      ),
    )
    await settle()

    for (const card of Array.from(host.querySelectorAll("[data-severity]"))) {
      expect(
        darkModeContrastViolations(card.className),
        `AlertBanner ${card.getAttribute("data-severity")} dark-mode contrast violation`,
      ).toEqual([])
    }
  })

  it("TracesPanel status badges avoid inverted dark tokens and pad single-digit trace hours", async () => {
    await act(async () =>
      root.render(
        <TracesPanel
          reservation="RES-R2-1"
          businessDate="2026-11-25"
          initialTraces={[
            {
              name: "SI-OD-1",
              department: "Front Desk",
              instruction: "Reconfirm airport limousine pickup",
              status: "Open",
              trace_date: "2026-11-24",
              trace_time: "9:00:00",
              trace_status: "Overdue",
            },
            {
              name: "SI-OP-1",
              department: "Concierge",
              instruction: "Deliver chilled sparkling water",
              status: "Open",
              trace_date: "2026-11-25",
              trace_time: "17:00:00",
              trace_status: "Open",
            },
          ]}
        />,
      ),
    )
    await settle()

    const traceBadges = Array.from(host.querySelectorAll("li span.rounded.border"))
    expect(traceBadges.length).toBeGreaterThanOrEqual(2)
    for (const badge of traceBadges) {
      expect(
        darkModeContrastViolations(badge.className),
        `TracesPanel badge (${badge.textContent}) dark-mode contrast violation`,
      ).toEqual([])
    }

    // Frappe Time delta "9:00:00" must format cleanly as "09:00", never truncated to "9:00:"
    expect(host.textContent).toContain("09:00")
    expect(host.textContent).not.toContain("9:00:")
  })
})
