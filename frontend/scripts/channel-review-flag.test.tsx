// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import ReservationDetail from "../src/screens/ReservationDetail"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

/** An OTA modify or cancel arriving after check-in or close leaves the stay
 *  unchanged and lights the review flag on the reservation drawer (OPERA
 *  Cloud OXI UPDATE INHOUSE RES = N + WARNINGS AS TRACES + Mark As Reviewed). */

const { callMock, detailMock } = vi.hoisted(() => ({
  callMock: vi.fn(),
  detailMock: vi.fn(),
}))

vi.mock("../src/lib/api", () => ({
  acceptBookingRequest: vi.fn(),
  amendStay: vi.fn(),
  amendStayPreview: vi.fn(),
  call: (...args: unknown[]) => callMock(...args),
  clearChannelReview: (reservation: string) =>
    callMock("kamra.channel_manager.clear_channel_review", { reservation }),
  declineBookingRequest: vi.fn(),
  getCurrentProperty: () => "Kampala Grand Exclusive",
  idDocumentImage: vi.fn().mockResolvedValue({ data: "" }),
  promoteWaitlist: vi.fn(),
  reservationDetail: (name: string) => detailMock(name),
  setLateArrival: vi.fn(),
  verifyPrecheckin: vi.fn(),
}))
vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: ["Front Desk"] }),
}))
vi.mock("../src/lib/realtime", () => ({ useRealtime: () => undefined }))
vi.mock("../src/components/LinkedRecords", () => ({ default: () => null }))
vi.mock("../src/components/StayInstructions", () => ({ default: () => null }))
vi.mock("../src/components/CancelPanel", () => ({ default: () => null }))

function sampleDetail(overrides: Record<string, unknown> = {}) {
  return {
    name: "RES-2026-00010",
    status: "Checked In",
    source: "OTA",
    channel: "Booking.com",
    booking_type: "Individual",
    property: "Kampala Grand Exclusive",
    check_in_date: "2026-11-23",
    check_out_date: "2026-11-24",
    nights: 1,
    adults: 2,
    children: 0,
    room: "RM-101",
    room_number: "101",
    room_type: "STD",
    room_type_name: "Standard",
    meal_plan: null,
    meal_plan_name: null,
    rate_plan: null,
    special_requests:
      "OTA modify — resolve: 2026-11-23 to 2026-11-25, Deluxe, 590000 UGX from Booking.com (not applied: the stay is Checked In)",
    eta: null,
    late_arrival: 0,
    occupants: [],
    booked_check_out_date: null,
    early_departure_reason: null,
    night_rates: [{ date: "2026-11-23", rate: 150000, demand_premium_pct: 0 }],
    precheckin_status: "Not Started",
    precheckin_token: null,
    id_document: null,
    id_document_source: null,
    id_document_on: null,
    id_document_discarded: 0,
    precheckin_verified_by: null,
    precheckin_verified_on: null,
    amount_after_tax: 177000,
    advance_paid: 0,
    company: null,
    travel_agent: null,
    folio_name: "FOLIO-2026-00010",
    money: { total: 177000, paid: 0, due: 177000, source: "folio", has_folio: true },
    deposit: null,
    security_deposit: null,
    guest: {
      name: "GUEST-1",
      full_name: "Ada Guest",
      phone: "+256700000001",
      email: "ada@example.com",
      vip: 0,
      blacklisted: 0,
      stays: 1,
      last_stay: null,
    },
    booker: null,
    cancellation: null,
    channel_review_needed: 1,
    channel_reviews: [
      {
        name: "AAL-2026-000001",
        action_type: "channel.modify_needs_review",
        rationale:
          "Booking.com modify of BDC-10 via Channex not applied: OTA modify — resolve: 2026-11-23 to 2026-11-25, Deluxe, 590000 UGX from Booking.com (not applied: the stay is Checked In)",
        executed_at: "2026-11-23 12:00:00",
      },
    ],
    actions: {
      can_check_in: false,
      can_check_out: true,
      can_cancel: true,
      can_no_show: false,
      can_amend: true,
      can_reinstate: false,
      can_reverse_check_in: false,
    },
    warnings: {
      id_document_missing: false,
      id_unverified: false,
      channel_review_needed: true,
    },
    checkin_backfill: [],
    ...overrides,
  }
}

describe("ReservationDetail: channel review flag", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    callMock.mockReset().mockResolvedValue({ ok: true })
    detailMock.mockReset().mockResolvedValue(sampleDetail())
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  const settle = () => act(() => new Promise((resolve) => setTimeout(resolve, 10)))

  async function renderDrawer(reload = vi.fn()) {
    await act(async () =>
      root.render(
        <MemoryRouter>
          <ReservationDetail row={{ name: "RES-2026-00010" }} reload={reload} onClose={() => undefined} />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Ada Guest")
    })
  }

  it("shows the review badge and banner with the unapplied channel update and marks it reviewed", async () => {
    const reload = vi.fn()
    await renderDrawer(reload)
    expect(host.textContent).toContain("Channel review needed")
    expect(host.textContent).toContain("Channel update not applied — review required")
    expect(host.textContent).toContain("590000 UGX")
    expect(host.textContent).not.toMatch(/Kamra/i)

    const button = Array.from(host.querySelectorAll("button")).find(
      (b) => b.textContent?.trim() === "Mark as reviewed",
    )
    expect(button, "Missing Mark as reviewed button").toBeDefined()
    await act(async () => button!.click())
    await settle()
    expect(callMock).toHaveBeenCalledWith("kamra.channel_manager.clear_channel_review", {
      reservation: "RES-2026-00010",
    })
    expect(reload).toHaveBeenCalled()
  })

  it("hides the review flag when channel_review_needed is 0", async () => {
    detailMock.mockResolvedValue(
      sampleDetail({
        channel_review_needed: 0,
        channel_reviews: [],
        warnings: { id_document_missing: false, id_unverified: false, channel_review_needed: false },
      }),
    )
    await renderDrawer()
    expect(host.textContent).not.toContain("Channel review needed")
    expect(host.textContent).not.toContain("Channel update not applied — review required")
  })

  it.each([
    ["ar", ar],
    ["de", de],
    ["es", es],
    ["fil", fil],
    ["id", id],
    ["pt", pt],
    ["zh-CN", zhCN],
  ])("translates the channel review UI strings in %s", (_language, dictionary: Record<string, string>) => {
    for (const key of [
      "Channel review needed",
      "Channel update not applied — review required",
      "The stay's dates, room, and charges were left unchanged. Review what the channel sent below and update the stay manually if needed.",
      "Mark as reviewed",
    ]) {
      expect(dictionary[key], key).toBeTruthy()
    }
  })
})
