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

async function productionMount(pathname: string) {
  window.history.replaceState(null, "", pathname)
  vi.resetModules()
  vi.stubEnv("PROD", true)
  return (await import("../src/lib/routing")).ROUTER_BASENAME
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

  it("keeps the Kamra and white-label mounts", async () => {
    expect(await productionMount("/kamra/checkin/tok123")).toBe("/kamra")
    expect(await productionMount("/hotel/checkin/tok123")).toBe("/hotel")
  })
})
