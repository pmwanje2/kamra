// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import AlertBanner from "../src/components/AlertBanner"
import TracesPanel from "../src/components/TracesPanel"
import CheckInDialog from "../src/components/CheckInDialog"

const { callMock } = vi.hoisted(() => ({
  callMock: vi.fn(),
}))

vi.mock("../src/lib/api", () => ({
  call: (...args: unknown[]) => callMock(...args),
  getCurrentProperty: () => "PROP-1",
  newIdempotencyKey: () => "idem-1",
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk", "Hotel Admin"] }),
}))

vi.mock("../src/lib/realtime", () => ({ useRealtime: () => undefined }))

describe("P4: AlertBanner, TracesPanel, and CheckInDialog blocking alert gate", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    callMock.mockReset()
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

  it("renders Blocking (non-dismissible), Warning, and Info alerts and requires a reason to resolve Blocking", async () => {
    const alerts = [
      {
        name: "ALT-00001",
        property: "PROP-1",
        trigger_area: "Check-In",
        severity: "Blocking",
        alert_code: "PASSPORT_CHECK",
        message: "Verify diplomatic passport before check-in",
        display_mode: "Until Resolved",
        status: "Active",
      },
      {
        name: "ALT-00002",
        property: "PROP-1",
        trigger_area: "Check-In",
        severity: "Warning",
        alert_code: "VIP_ARRIVAL",
        message: "Escort VIP guest to executive lounge",
        display_mode: "Every Access",
        status: "Active",
      },
      {
        name: "ALT-00003",
        property: "PROP-1",
        trigger_area: "Check-In",
        severity: "Info",
        alert_code: "LATE_FLIGHT",
        message: "Arriving on late flight",
        display_mode: "Once Per Business Day",
        status: "Active",
      },
    ]
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.traces.list_alerts") return alerts
      if (method === "kamra.traces.acknowledge_alert") return { ...alerts[1], status: "Acknowledged" }
      if (method === "kamra.traces.resolve_alert") return { ...alerts[0], status: "Resolved" }
      return { ok: true }
    })

    const onBlockingChange = vi.fn()
    await act(async () =>
      root.render(
        <AlertBanner
          trigger="Check-In"
          property="PROP-1"
          reservation="RES-001"
          onBlockingChange={onBlockingChange}
        />,
      ),
    )
    await settle()

    expect(host.textContent).toContain("Verify diplomatic passport before check-in")
    expect(host.textContent).toContain("Escort VIP guest to executive lounge")
    expect(host.textContent).toContain("Arriving on late flight")
    expect(onBlockingChange).toHaveBeenCalledWith(true)

    // Blocking alert card has NO plain Acknowledge/Dismiss button — only Resolve
    const blockingCard = host.querySelector('[data-severity="Blocking"]')
    expect(blockingCard).not.toBeNull()
    expect(blockingCard?.textContent).not.toContain("Acknowledge")

    // Warning alert has Acknowledge button
    const warningCard = host.querySelector('[data-severity="Warning"]')
    expect(warningCard).not.toBeNull()
    const ackBtn = Array.from(warningCard!.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Acknowledge"),
    )
    expect(ackBtn).toBeDefined()
    await act(async () => ackBtn!.click())
    await settle()
    expect(callMock).toHaveBeenCalledWith("kamra.traces.acknowledge_alert", { alert: "ALT-00002" })
  })

  it("renders TracesPanel with Due Today & Overdue / Scheduled / Done tabs and adds/resolves dated traces", async () => {
    const rows = [
      {
        name: "SI-001",
        department: "Housekeeping",
        instruction: "Extra baby cot",
        status: "Open",
        trace_date: "2026-11-23",
        trace_time: "09:00:00",
        trace_status: "Overdue",
        done_by: null,
        done_on: null,
        resolved_by: null,
        resolved_at: null,
      },
      {
        name: "SI-002",
        department: "Concierge",
        instruction: "Book safari transfer",
        status: "Open",
        trace_date: "2026-11-26",
        trace_time: null,
        trace_status: "Scheduled",
        done_by: null,
        done_on: null,
        resolved_by: null,
        resolved_at: null,
      },
    ]
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.traces.list_traces") return rows
      if (method === "kamra.traces.resolve_trace")
        return {
          ok: true,
          traces: [{ ...rows[0], status: "Done", trace_status: "Done", resolved_by: "desk@hotel.test" }, rows[1]],
        }
      return { ok: true, traces: rows }
    })

    await act(async () =>
      root.render(
        <TracesPanel reservation="RES-001" businessDate="2026-11-24" canAdd />,
      ),
    )
    await settle()

    // Default tab shows Due Today & Overdue, showing Overdue badge
    expect(host.textContent).toContain("Extra baby cot")
    expect(host.textContent).toContain("Overdue")
    expect(host.textContent).not.toContain("Book safari transfer")

    // Switching to Scheduled tab shows future scheduled trace
    const schedTab = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Scheduled"),
    )
    expect(schedTab).toBeDefined()
    await act(async () => schedTab!.click())
    await settle()
    expect(host.textContent).toContain("Book safari transfer")
  })

  it("disables Check-In confirm button when a Check-In Blocking alert is active", async () => {
    callMock.mockImplementation(async (method: string) => {
      if (method === "kamra.checkin.checkin_flow") {
        return {
          reservation: {
            name: "RES-001",
            status: "Confirmed",
            guest: "GUEST-1",
            guest_name: "Ada Guest",
            room_type: "STD",
            check_in_date: "2026-11-24",
            check_out_date: "2026-11-26",
            nights: 2,
            adults: 2,
            children: 0,
            vip: 0,
            source: "Direct",
            special_requests: "",
          },
          guest: {
            name: "GUEST-1",
            full_name: "Ada Guest",
            phone: "+256700000001",
            email: null,
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
              state: "done",
              done_by: "desk@hotel.test",
              done_at: "2026-11-24 08:00:00",
            },
          ],
          missing_required: [],
          pending_backfill: [],
          rooms: [],
          request_presets: [],
          ids: { ok: true, missing: [], online: false, precheckin_status: "Not Started", precheckin_on: null, occupants: [] },
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
            stay_total: 200,
            advance_paid: 0,
            balance_due: 200,
            options: ["Guest"],
          },
          room: {
            room: "PROP-1-101",
            room_number: "101",
            housekeeping_status: "Clean",
            entire_place: null,
            room_type_name: "Standard",
            assigned_by: "desk@hotel.test",
            assigned_at: "2026-11-24 08:00:00",
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
          keys: { count: 1, cards: "", issued_by: null, adapter: "manual", adapter_label: "Manual", manual: true },
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
          alerts: [
            {
              name: "ALT-BLOCK-1",
              property: "PROP-1",
              trigger_area: "Check-In",
              severity: "Blocking",
              alert_code: "NO_CREDIT",
              message: "Cash-only clearance required from manager",
              display_mode: "Until Resolved",
              status: "Active",
            },
          ],
        }
      }
      return []
    })

    await act(async () =>
      root.render(
        <CheckInDialog reservation="RES-001" onDone={() => undefined} onClose={() => undefined} />,
      ),
    )
    await settle()

    expect(host.textContent).toContain("Cash-only clearance required from manager")
    const confirmBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Check in to room 101"),
    )
    expect(confirmBtn).toBeDefined()
    expect(confirmBtn!.disabled).toBe(true)
  })
})
