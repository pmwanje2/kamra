// @vitest-environment jsdom
// Links that leave the router (a new tab, a full-page load) carry the mount the
// SPA is served under, so a white-labelled /hotel site never links to /kamra.
import { afterEach, describe, expect, it, vi } from "vitest"

vi.mock("../src/lib/api", () => ({ call: vi.fn() }))

async function phoneApp(pathname: string) {
  window.history.replaceState(null, "", pathname)
  vi.resetModules()
  const { APPS } = await import("../src/lib/apps")
  return APPS.find((app) => app.id === "housekeeping")?.items.find((item) => item.label === "Phone App")?.href
}

async function productionRouting(pathname: string) {
  window.history.replaceState(null, "", pathname)
  vi.resetModules()
  vi.stubEnv("PROD", true)
  return await import("../src/lib/routing")
}

async function productionMount(pathname: string) {
  return (await productionRouting(pathname)).ROUTER_BASENAME
}

afterEach(() => {
  window.history.replaceState(null, "", "/")
  vi.unstubAllEnvs()
})

describe("links outside the router", () => {
  it("open the housekeeping phone app under the white-label mount", async () => {
    expect(await phoneApp("/hotel/housekeeping")).toBe("/hotel/hk")
  })

  it("open the housekeeping phone app under the development mount", async () => {
    expect(await phoneApp("/housekeeping")).toBe("/hk")
  })
})

describe("the production router's mount", () => {
  it("answers a guest's check-in link where it was sent, at the site root", async () => {
    expect(await productionMount("/checkin/tok123")).toBe("/")
  })

  it.each([
    "/book",
    "/book/",
    "/book/2026-07-10/2026-07-12/2/0",
    "/book/grand-palace",
    "/stay",
    "/stay/",
    "/stay/grand-villa",
    "/stay/grand-villa/2026-07-10/2026-07-12/2/0",
    "/hk",
    "/hk/",
  ])("mounts %s at the site root with no /kamra redirect", async (path) => {
    const routing = await productionRouting(path)
    expect(routing.ROUTER_BASENAME).toBe("/")
    expect(routing.toFullPath("/book")).toBe("/book")
    expect(routing.toFullPath("/stay/grand-villa")).toBe("/stay/grand-villa")
    expect(routing.toFullPath("/hk")).toBe("/hk")
  })

  it.each([
    "/book-appointment",
    "/staying",
    "/hks",
    "/hk/extra",
    "/",
  ])("does not over-match %s as a bare public route", async (path) => {
    expect(await productionMount(path)).toBe("/kamra")
  })

  it.each([
    "/book",
    "/book/2026-07-10/2026-07-12/2/0",
    "/stay",
    "/stay/grand-villa",
    "/hk",
    "/checkin/tok123",
  ])("keeps the Kamra and white-label mounts for %s", async (suffix) => {
    expect(await productionMount(`/kamra${suffix}`)).toBe("/kamra")
    const hotel = await productionRouting(`/hotel${suffix}`)
    expect(hotel.ROUTER_BASENAME).toBe("/hotel")
    expect(hotel.toFullPath(suffix)).toBe(`/hotel${suffix}`)
  })
})
