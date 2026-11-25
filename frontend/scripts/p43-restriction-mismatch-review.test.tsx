// @vitest-environment jsdom
// R2 RED test for PR #57 P1 #2: ReservationDetail restriction_mismatch banner copy & localization
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import ReservationDetail from "../src/screens/ReservationDetail"
import ar from "../src/i18n/locales/ar.json"
import de from "../src/i18n/locales/de.json"
import es from "../src/i18n/locales/es.json"
import fil from "../src/i18n/locales/fil.json"
import fr from "../src/i18n/locales/fr.json"
import id from "../src/i18n/locales/id.json"
import pt from "../src/i18n/locales/pt.json"
import zhCN from "../src/i18n/locales/zh-CN.json"

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
  getCurrentProperty: () => "Grand Kuwait",
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

const MISMATCH_TITLE = "OTA booking honored despite active stay restrictions — review required"
const MISMATCH_BODY =
  "The channel booking was accepted to avoid a walk even though active stay restrictions were in place. Review the restriction details below and adjust the stay or channel availability if needed."

function restrictionMismatchDetail() {
  return {
    name: "RES-2026-00099",
    status: "Confirmed",
    source: "OTA",
    channel: "Booking.com",
    booking_type: "Individual",
    property: "Grand Kuwait",
    check_in_date: "2026-12-01",
    check_out_date: "2026-12-02",
    nights: 1,
    adults: 2,
    children: 0,
    room: "RM-101",
    room_number: "101",
    room_type: "STD",
    room_type_name: "Standard",
    meal_plan: null,
    meal_plan_name: null,
    rate_plan: "BAR",
    special_requests: "OTA restriction mismatch — resolve: Closed to arrival on 2026-12-01.",
    eta: null,
    late_arrival: 0,
    occupants: [],
    booked_check_out_date: null,
    early_departure_reason: null,
    night_rates: [{ date: "2026-12-01", rate: 48.375, demand_premium_pct: 0 }],
    precheckin_status: "Not Started",
    precheckin_token: null,
    id_document: null,
    id_document_source: null,
    id_document_on: null,
    id_document_discarded: 0,
    precheckin_verified_by: null,
    precheckin_verified_on: null,
    amount_after_tax: 48.375,
    advance_paid: 0,
    company: null,
    travel_agent: null,
    folio_name: "FOLIO-2026-00099",
    money: { total: 48.375, paid: 0, due: 48.375, source: "folio", has_folio: true },
    deposit: null,
    security_deposit: null,
    guest: {
      name: "GUEST-99",
      full_name: "Nadia Guest",
      phone: "+96550000099",
      email: "nadia@example.com",
      vip: 0,
      blacklisted: 0,
      stays: 1,
      last_stay: null,
    },
    booker: null,
    cancellation: null,
    channel_review_needed: 1,
    channel_review_reason: "restriction_mismatch",
    channel_reviews: [
      {
        name: "AAL-2026-000099",
        action_type: "channel.restriction_mismatch",
        reason: "restriction_mismatch",
        rationale:
          "Booking.com book HTTP-PROBE-01 via Channex accepted for review despite restriction: Closed to arrival on 2026-12-01.",
        executed_at: "2026-12-01 09:30:00",
      },
    ],
    actions: {
      can_check_in: true,
      can_check_out: false,
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
  }
}

describe("ReservationDetail: OTA restriction_mismatch review banner [P1 #2]", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    callMock.mockReset().mockResolvedValue({ ok: true })
    detailMock.mockReset().mockResolvedValue(restrictionMismatchDetail())
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

  it("shows accurate restriction-mismatch alert copy and structured review record instead of 'not applied'", async () => {
    await act(async () =>
      root.render(
        <MemoryRouter>
          <ReservationDetail row={{ name: "RES-2026-00099" }} reload={vi.fn()} onClose={() => undefined} />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Nadia Guest")
    })

    const text = host.textContent ?? ""
    expect(text).toContain("Channel review needed")
    expect(text).toContain(MISMATCH_TITLE)
    expect(text).toContain(MISMATCH_BODY)
    expect(text).toContain("Closed to arrival on 2026-12-01.")
    expect(text).not.toContain("Channel update not applied — review required")
    expect(text).not.toContain("dates, room, and charges were left unchanged")
    expect(text).not.toMatch(/Kamra/i)
  })

  it.each([
    ["ar", ar],
    ["de", de],
    ["es", es],
    ["fil", fil],
    ["fr", fr],
    ["id", id],
    ["pt", pt],
    ["zh-CN", zhCN],
  ])("localizes restriction_mismatch banner strings in %s", (_language, dictionary: Record<string, string>) => {
    expect(dictionary[MISMATCH_TITLE], MISMATCH_TITLE).toBeTruthy()
    expect(dictionary[MISMATCH_BODY], MISMATCH_BODY).toBeTruthy()
  })
})
