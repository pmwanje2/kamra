// @vitest-environment jsdom
// P43: Channel Mapping & Rate Parity Health UI (/channel-health) + ChannelConnectionActions link
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"
import ChannelConnectionActions from "../src/components/ChannelConnectionActions"

const FORBIDDEN = /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY)(?![a-z0-9])|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala/i

const { rpc, authState } = vi.hoisted(() => ({
  rpc: vi.fn(),
  authState: { roles: ["Revenue Manager"] as string[] },
}))

vi.mock("../src/lib/api", () => ({
  call: (method: string, params: unknown) => {
    if (method === "kamra.api.property_locale") {
      return Promise.resolve({ locale: "en-US", currency: "KWD", currency_symbol: "KWD " })
    }
    return rpc(method, params)
  },
  getCurrentProperty: () => "Grand Kuwait",
  frappeFetch: async () => ({ data: [] }),
  serverError: (error: Error) => error.message,
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: "authed", roles: authState.roles }),
}))

vi.mock("../src/AppShell", () => ({ default: () => <Outlet /> }))

function makeSnapshot(canManage = true) {
  return {
    property: "Grand Kuwait",
    property_currency: "KWD",
    checked_at: "2026-12-01 09:00:00",
    can_manage: canManage,
    connections: [
      {
        name: "CMC-00001",
        provider: "Channex",
        active: 1,
        external_property_id: "cx-kuwait-01",
        uses_outbox: true,
        sync_days: 90,
        ari_calls_per_minute: 10,
        ari_batch_seconds: 30,
        last_push: "2026-12-01 08:55:00",
        last_push_status: "OK: 1 availability, 1 restrictions",
        last_full_sync: "2026-11-30 06:00:00",
        health_status: "Error",
        paused: {},
        outbox_counts: { pending: 2, sending: 0, sent: 14, failed: 1 },
        mappings_count: 2,
      },
    ],
    outbox: [
      {
        name: "OUT-001",
        connection: "CMC-00001",
        provider: "Channex",
        date_from: "2026-12-01",
        date_to: "2026-12-05",
        kinds: "restrictions",
        full: 0,
        status: "Failed",
        attempts: 3,
        queued_at: "2026-12-01 08:50:00",
        next_attempt_at: null,
        sent_at: null,
        reason: "restrictions.bulk_set",
        detail: "Channex 422: min_stay_arrival invalid on rp-std-kwd",
      },
    ],
    parity: [
      {
        mapping_name: "CRM-00001",
        connection: "CMC-00001",
        provider: "Channex",
        room_type: "RT-STD",
        room_type_name: "Standard Suite",
        rate_plan: null,
        rate_plan_label: "BAR",
        external_room_id: "rt-std",
        external_rate_id: "rp-std-kwd",
        external_currency: "KWD",
        currency: "KWD",
        direct_rate: 45.125,
        ota_rate: 40.0,
        parity_delta: -5.125,
        parity_status: "Undercut",
        last_synced_at: "2026-12-01 08:55:00",
        last_error: "",
        restrictions_synced: {
          stop_sell: true,
          min_stay_arrival: 2,
          max_stay: 7,
          closed_to_arrival: true,
          closed_to_departure: false,
          active_days: 3,
        },
        days: [
          {
            date: "2026-12-01",
            direct_rate: 45.125,
            ota_rate: 40.0,
            delta: -5.125,
            status: "Undercut",
            stop_sell: true,
            min_los: 2,
            max_los: 7,
            cta: true,
            ctd: false,
          },
        ],
      },
      {
        mapping_name: "CRM-00002",
        connection: "CMC-00001",
        provider: "Channex",
        room_type: "RT-DLX",
        room_type_name: "Deluxe King",
        rate_plan: "RP-USD",
        rate_plan_label: "USD Direct",
        external_room_id: "rt-dlx",
        external_rate_id: "rp-dlx-usd",
        external_currency: "USD",
        currency: "USD",
        direct_rate: 129.5,
        ota_rate: 129.5,
        parity_delta: 0,
        parity_status: "OK",
        last_synced_at: "2026-12-01 08:55:00",
        last_error: "",
        restrictions_synced: {
          stop_sell: false,
          min_stay_arrival: null,
          max_stay: null,
          closed_to_arrival: false,
          closed_to_departure: false,
          active_days: 0,
        },
        days: [],
      },
    ],
    summary: {
      connections_total: 1,
      connections_active: 1,
      outbox_pending: 2,
      outbox_failed: 1,
      mappings_total: 2,
      parity_ok: 1,
      parity_issues: 1,
      unmapped_room_types: [{ name: "RT-VLA", room_type_name: "Royal Villa", room_count: 1 }],
    },
  }
}

describe("P43 Channel Mapping & Rate Parity Health UI", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    authState.roles = ["Revenue Manager"]
    rpc.mockReset()
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

  it("renders connection health, parity monitor in exact 3dp/2dp, restrictions badges, outbox failures, and retry/full-sync actions", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.restrictions.channel_health_snapshot") return Promise.resolve(makeSnapshot(true))
      if (method === "kamra.restrictions.retry_outbox_failures") return Promise.resolve({ ok: true, retried: 1 })
      if (method === "kamra.channel_ari.request_full_sync") return Promise.resolve({ queued: true })
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/channel-health"]}>
          <App />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Standard Suite")
    }, { interval: 1, timeout: 3000 })

    const body = host.textContent ?? ""
    expect(body).toContain("Channel mapping & parity health")
    expect(body).toContain("45.125")
    expect(body).toContain("40.000")
    expect(body).toContain("129.50")
    expect(body).toContain("Undercut")
    expect(body).toContain("Channex 422: min_stay_arrival invalid on rp-std-kwd")
    expect(body).toContain("Royal Villa")
    expect(FORBIDDEN.test(body)).toBe(false)

    const retryBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Retry failed outbox"),
    )!
    expect(retryBtn).toBeDefined()
    await act(async () => retryBtn.click())
    await settle()
    expect(rpc).toHaveBeenCalledWith("kamra.restrictions.retry_outbox_failures", { connection: "CMC-00001" })

    const syncBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Push full ARI"),
    )!
    expect(syncBtn).toBeDefined()
    await act(async () => syncBtn.click())
    await settle()
    expect(rpc).toHaveBeenCalledWith("kamra.channel_ari.request_full_sync", { connection: "CMC-00001", confirm: 0 })
  })

  it("hides mutating channel actions for Front Desk read-only viewers", async () => {
    authState.roles = ["Front Desk"]
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.restrictions.channel_health_snapshot") return Promise.resolve(makeSnapshot(false))
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })

    await act(async () =>
      root.render(
        <MemoryRouter initialEntries={["/channel-health"]}>
          <App />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.textContent).toContain("Standard Suite")
    }, { interval: 1, timeout: 3000 })

    const retryBtn = Array.from(host.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Retry failed outbox"),
    )
    expect(retryBtn).toBeUndefined()
  })

  it("links to /channel-health from ChannelConnectionActions without vendor copy", async () => {
    rpc.mockImplementation((method: string) => {
      if (method === "kamra.channel_ari.outbox_status") {
        return Promise.resolve({
          outbox: true,
          pending: 1,
          failed: 2,
          paused: {},
          last_push: "2026-12-01 08:55:00",
          last_push_status: "OK",
          last_full_sync: "",
        })
      }
      return Promise.reject(new Error(`Unexpected: ${method}`))
    })
    await act(async () =>
      root.render(
        <MemoryRouter>
          <ChannelConnectionActions row={{ name: "CMC-00001" }} reload={() => {}} />
        </MemoryRouter>,
      ),
    )
    await vi.waitFor(async () => {
      await settle()
      expect(host.querySelector('a[href="/channel-health"]')).not.toBeNull()
    }, { interval: 1, timeout: 3000 })
    expect(FORBIDDEN.test(host.textContent ?? "")).toBe(false)
  })
})
