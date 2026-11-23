// @vitest-environment jsdom
// Public and phone routes (/book, /stay, /hk) are served directly at the bare
// path without a /kamra redirect; in-app navigation must keep working under
// both the bare root mount and the white-labelled /hotel mount in production.
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { BrowserRouter, Outlet } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import App from "../src/App"

const { callMock, callGuestMock, whoamiMock, authState } = vi.hoisted(() => ({
  callMock: vi.fn(),
  callGuestMock: vi.fn(),
  whoamiMock: vi.fn(),
  authState: { status: "anon" as "anon" | "authed", roles: [] as string[] },
}))

vi.mock("../src/lib/api", () => ({
  call: (...args: unknown[]) => callMock(...args),
  callGuest: (...args: unknown[]) => callGuestMock(...args),
  getCurrentProperty: () => "Fixture Hotel",
  getDefaultProperty: () => Promise.resolve("Fixture Hotel"),
  isNetworkError: () => false,
  login: vi.fn(),
  logout: () => Promise.resolve(),
  requestPasswordReset: vi.fn(),
  serverError: (error: Error) => error.message,
  uploadTo: vi.fn(),
  whoami: () => whoamiMock(),
}))

vi.mock("../src/lib/realtime", () => ({
  subscribeRealtime: () => () => {},
}))

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({ status: authState.status, roles: authState.roles, refresh: vi.fn() }),
}))

vi.mock("../src/AppShell", () => ({
  default: () => <Outlet />,
}))

const SHOWCASE = {
  property: {
    name: "Fixture Hotel",
    property_name: "Fixture Hotel",
    description: "Direct booking fixture",
    logo_url: null,
    hero_image: null,
    brand_accent: null,
    payment_mode: "Pay at hotel",
    advance_percent: 0,
    registration_fee: 0,
    cleaning_fee: 0,
    security_deposit_amount: 0,
    minimum_nights: 1,
    free_cancel_days: 0,
    cancellation_fee: "None",
    booking_mode: "Instant",
    property_kind: "Hotel",
    star_category: null,
    address_line: null,
    city: "Kampala",
    state: "Central",
    pincode: null,
    phone: null,
    google_reviews_url: null,
    tripadvisor_url: null,
    amenities: [],
    checkin_time: "14:00",
    checkout_time: "11:00",
    driving_directions: null,
    latitude: null,
    longitude: null,
    gallery: [],
    faqs: [],
    house_rules: null,
    pets_policy: null,
    children_policy: null,
    extra_bed_policy: null,
    meta_title: null,
    meta_description: null,
    og_image: null,
    page_slug: "fixture-hotel",
  },
  room_types: [
    {
      name: "RT-VILLA",
      room_type_name: "Pool Suite",
      listing_slug: "pool-suite",
      location_slug: "ocean-villa",
      description: "Suite by the water",
      base_price: 250,
      adults_capacity: 2,
      children_capacity: 1,
      bed_type: "King",
      room_view: "Garden",
      amenities: [],
      media: [],
      location_name: "Ocean Villa",
      location_address: null,
      google_maps_url: null,
      latitude: null,
      longitude: null,
    },
  ],
  locations: [
    {
      name: "Ocean Villa",
      slug: "ocean-villa",
      address: "Lake Road",
      google_maps_url: null,
      latitude: null,
      longitude: null,
      room_types: ["RT-VILLA"],
      from_rate: 250,
    },
  ],
  meal_plans: [],
  experiences: [],
}

describe("in-app navigation under production bare and /hotel mounts", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    authState.status = "anon"
    authState.roles = []
    callMock.mockReset().mockResolvedValue({})
    callGuestMock.mockReset().mockResolvedValue({})
    whoamiMock.mockReset()
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    window.history.replaceState(null, "", "/")
    localStorage.clear()
    vi.unstubAllEnvs()
    vi.unstubAllGlobals()
  })

  async function mountAt(startPath: string) {
    window.history.replaceState(null, "", startPath)
    vi.resetModules()
    vi.stubEnv("PROD", true)
    const { ROUTER_BASENAME } = await import("../src/lib/routing")
    await act(async () => {
      root.render(
        <BrowserRouter basename={ROUTER_BASENAME}>
          <App />
        </BrowserRouter>,
      )
    })
    return ROUTER_BASENAME
  }

  async function waitForSettled() {
    for (let i = 0; i < 60; i++) {
      const text = host.textContent?.trim() ?? ""
      if (text && !/^Loading[….]+$/.test(text)) return text
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 15))
      })
    }
    return host.textContent?.trim() ?? ""
  }

  async function waitForText(expected: string) {
    for (let i = 0; i < 60; i++) {
      if ((host.textContent ?? "").includes(expected)) return
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 15))
      })
    }
    expect(host.textContent).toContain(expected)
  }

  it.each([
    ["bare root", "", "/"],
    ["white-label /hotel", "/hotel", "/hotel"],
  ])("navigates between /book and /stay under the %s mount", async (_label, prefix, expectedBase) => {
    callGuestMock.mockImplementation((method: string, params?: Record<string, string>) => {
      if (method === "kamra.public_api.catalog_index") {
        return Promise.resolve({
          mode: "sites",
          property: "Fixture Hotel",
          sites: [{
            name: "Ocean Villa",
            slug: "ocean-villa",
            address: "Lake Road",
            from_rate: 250,
            room_types: ["RT-VILLA"],
            listing_count: 1,
          }],
        })
      }
      if (method === "kamra.public_api.resolve_slug") {
        if (params?.slug === "ocean-villa") {
          return Promise.resolve({
            kind: "site",
            property: "Fixture Hotel",
            location_slug: "ocean-villa",
            location_name: "Ocean Villa",
          })
        }
        return Promise.resolve({
          kind: "listing",
          property: "Fixture Hotel",
          listing_slug: "pool-suite",
          room_type: "RT-VILLA",
          room_type_name: "Pool Suite",
        })
      }
      if (method === "kamra.public_api.showcase") return Promise.resolve(SHOWCASE)
      if (method === "kamra.public_api.search_stay") return Promise.resolve([])
      throw new Error(`Unexpected guest RPC: ${method}`)
    })

    const basename = await mountAt(`${prefix}/book`)
    expect(basename).toBe(expectedBase)
    await waitForText("Ocean Villa")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/book/\\d{4}-\\d{2}-\\d{2}/\\d{4}-\\d{2}-\\d{2}/2/0$`))
    expect(window.location.pathname).not.toContain("/kamra")

    // Follow a site card link from /book into /stay/<slug>
    const siteLink = host.querySelector<HTMLAnchorElement>("a[href*='/stay/ocean-villa']")
    expect(siteLink, "Missing site card link").not.toBeNull()
    expect(siteLink!.getAttribute("href")).toMatch(new RegExp(`^${prefix}/stay/ocean-villa/`))
    await act(async () => siteLink!.click())
    await waitForText("Choose a listing")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/stay/ocean-villa/`))

    // Follow "View details" inside the site screen to /stay/<listing_slug>
    const detailsBtn = Array.from(host.querySelectorAll("button")).find((b) => b.textContent?.trim() === "View details")
    expect(detailsBtn, "Missing View details button").toBeDefined()
    await act(async () => detailsBtn!.click())
    await waitForText("Pool Suite")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/stay/pool-suite/`))
    expect(window.location.pathname).not.toContain("/kamra")

    // Follow header back link from /stay/<listing_slug> to /stay/ocean-villa, then back to /book
    const backToSite = host.querySelector<HTMLAnchorElement>("header a")
    expect(backToSite?.getAttribute("href")).toMatch(new RegExp(`^${prefix}/stay/ocean-villa/`))
    await act(async () => backToSite!.click())
    await waitForText("All properties")
    const backToBook = host.querySelector<HTMLAnchorElement>("header a")
    expect(backToBook?.getAttribute("href")).toMatch(new RegExp(`^${prefix}/book/`))
    await act(async () => backToBook!.click())
    await waitForText("Places to stay")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/book/`))
    expect(window.location.pathname).not.toContain("/kamra")
  })

  it.each([
    ["bare root", "", "/"],
    ["white-label /hotel", "/hotel", "/hotel"],
  ])("redirects a property /stay/<slug> to /book?property=<slug> under the %s mount", async (_label, prefix, expectedBase) => {
    callGuestMock.mockImplementation((method: string) => {
      if (method === "kamra.public_api.resolve_slug") {
        return Promise.resolve({
          kind: "property",
          property: "Fixture Hotel",
          property_slug: "fixture-hotel",
        })
      }
      if (method === "kamra.public_api.catalog_index") {
        return Promise.resolve({ mode: "hotel", property: "Fixture Hotel" })
      }
      if (method === "kamra.public_api.showcase") return Promise.resolve(SHOWCASE)
      if (method === "kamra.public_api.search_stay") return Promise.resolve([])
      throw new Error(`Unexpected guest RPC: ${method}`)
    })

    const basename = await mountAt(`${prefix}/stay/fixture-hotel`)
    expect(basename).toBe(expectedBase)
    await waitForText("Choose your room")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/book/\\d{4}-\\d{2}-\\d{2}/\\d{4}-\\d{2}-\\d{2}/2/0$`))
    expect(new URLSearchParams(window.location.search).get("property")).toBe("fixture-hotel")
    expect(window.location.pathname).not.toContain("/kamra")
  })

  it.each([
    ["bare root", "", "/"],
    ["white-label /hotel", "/hotel", "/hotel"],
  ])("renders /hk and switches tabs in place under the %s mount", async (_label, prefix, expectedBase) => {
    whoamiMock.mockResolvedValue({ user: "hk@example.test", roles: ["Housekeeping"] })
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.api.hk_queue") {
        return Promise.resolve({ tasks: [], rooms: [] })
      }
      throw new Error(`Unexpected staff RPC: ${method}`)
    })

    const basename = await mountAt(`${prefix}/hk`)
    expect(basename).toBe(expectedBase)
    await waitForText("Housekeeping")
    expect(window.location.pathname).toBe(`${prefix}/hk`)

    const availableTab = Array.from(host.querySelectorAll("nav button")).find((b) =>
      b.textContent?.includes("Available"),
    )
    expect(availableTab, "Missing Available tab").toBeDefined()
    await act(async () => availableTab!.click())
    await waitForText("No unassigned rooms right now.")
    expect(window.location.pathname).toBe(`${prefix}/hk`)
    expect(window.location.pathname).not.toContain("/kamra")
  })

  it.each([
    ["bare root (guest)", "", "/", "anon", []],
    ["white-label /hotel (guest)", "/hotel", "/hotel", "anon", []],
    ["bare root (staff)", "", "/", "authed", ["Hotel Admin"]],
    ["white-label /hotel (staff)", "/hotel", "/hotel", "authed", ["Hotel Admin"]],
  ] as const)("routes bare /stay to the public booking engine under %s", async (_label, prefix, expectedBase, status, roles) => {
    authState.status = status
    authState.roles = [...roles]
    callGuestMock.mockImplementation((method: string) => {
      if (method === "kamra.public_api.catalog_index") {
        return Promise.resolve({
          mode: "properties",
          properties: [{
            name: "Fixture Hotel",
            property_name: "Fixture Hotel",
            property_slug: "fixture-hotel",
            city: "Kampala",
            state: "Central",
            hero_image: null,
            logo_url: null,
            property_kind: "Hotel",
            description: "Direct booking fixture",
            from_rate: 250,
          }],
        })
      }
      throw new Error(`Unexpected guest RPC: ${method}`)
    })

    const basename = await mountAt(`${prefix}/stay?utm_source=ig`)
    expect(basename).toBe(expectedBase)
    await waitForSettled()
    expect(host.textContent).toContain("Places to stay")
    expect(host.textContent).not.toContain("Sign in")
    expect(host.textContent).not.toContain("Page not found.")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/book/\\d{4}-\\d{2}-\\d{2}/\\d{4}-\\d{2}-\\d{2}/2/0$`))
    expect(new URLSearchParams(window.location.search).get("utm_source")).toBe("ig")
    expect(window.location.pathname).not.toContain("/login")
    expect(window.location.pathname).not.toContain("/kamra")
  })

  it.each([
    ["bare root /book overflow", "", "/", "/book/2026-07-10/2026-07-12/2/0/extra"],
    ["white-label /hotel/book overflow", "/hotel", "/hotel", "/hotel/book/2026-07-10/2026-07-12/2/0/extra"],
    ["bare root /stay overflow", "", "/", "/stay/ocean-villa/2026-07-10/2026-07-12/2/0/extra"],
    ["white-label /hotel/stay overflow", "/hotel", "/hotel", "/hotel/stay/ocean-villa/2026-07-10/2026-07-12/2/0/extra"],
  ])("keeps %s on the public booking engine instead of staff login", async (_label, prefix, expectedBase, startPath) => {
    callGuestMock.mockImplementation((method: string) => {
      if (method === "kamra.public_api.catalog_index") {
        return Promise.resolve({ mode: "hotel", property: "Fixture Hotel" })
      }
      if (method === "kamra.public_api.showcase") return Promise.resolve(SHOWCASE)
      if (method === "kamra.public_api.search_stay") return Promise.resolve([])
      throw new Error(`Unexpected guest RPC: ${method}`)
    })

    const basename = await mountAt(startPath)
    expect(basename).toBe(expectedBase)
    await waitForSettled()
    expect(host.textContent).toContain("Choose your room")
    expect(host.textContent).not.toContain("Sign in")
    expect(window.location.pathname).toMatch(new RegExp(`^${prefix}/book/`))
    expect(window.location.pathname).not.toContain("/login")
    expect(window.location.pathname).not.toContain("/kamra")
  })

  it.each([
    ["bare root /checkin/<token>/extra", "/", "/checkin/tok123/extra"],
    ["white-label /hotel/checkin/<token>/extra", "/hotel", "/hotel/checkin/tok123/extra"],
    ["white-label bare /hotel/checkin", "/hotel", "/hotel/checkin"],
  ])("shows an invalid check-in link message for %s instead of staff login", async (_label, expectedBase, startPath) => {
    const basename = await mountAt(startPath)
    expect(basename).toBe(expectedBase)
    await waitForSettled()
    expect(host.textContent).toContain("This check-in link isn't valid. Please contact the hotel.")
    expect(host.textContent).not.toContain("Sign in")
    expect(host.textContent).not.toContain("Page not found.")
    expect(callMock).not.toHaveBeenCalled()
    expect(window.location.pathname).toBe(startPath)
  })

  it.each([
    ["bare root", "", "/"],
    ["white-label /hotel", "/hotel", "/hotel"],
  ])("still serves a valid /checkin/<token> link under the %s mount", async (_label, prefix, expectedBase) => {
    callMock.mockImplementation((method: string) => {
      if (method === "kamra.public_api.precheckin_info") {
        return Promise.resolve({
          id_types: ["Passport"],
          property: {
            property_name: "Fixture Hotel",
            logo_url: null,
            city: "Kampala",
            checkin_time: "14:00:00",
            phone: null,
            house_rules: null,
            pets_policy: null,
            children_policy: null,
            extra_bed_policy: null,
            id_retention: "Verify & Discard",
          },
          stay: {
            reservation: "RES-1",
            room_type: "Pool Suite",
            check_in_date: "2026-07-10",
            check_out_date: "2026-07-12",
            nights: 2,
            adults: 2,
            children: 0,
            status: "Pending",
          },
          guest: {
            full_name: "Ada Guest",
            phone: "+256700000001",
            email: null,
            id_type: null,
            nationality: null,
            has_id_document: false,
            id_document_on: "",
          },
        })
      }
      if (method === "kamra.laundry.guest_laundry") return Promise.resolve({ active: false })
      return Promise.resolve(null)
    })

    const basename = await mountAt(`${prefix}/checkin/tok123`)
    expect(basename).toBe(expectedBase)
    await waitForText("Online check-in")
    expect(host.textContent).toContain("Ada Guest")
    expect(window.location.pathname).toBe(`${prefix}/checkin/tok123`)
  })
})
