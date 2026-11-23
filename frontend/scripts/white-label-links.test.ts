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

afterEach(() => window.history.replaceState(null, "", "/"))

describe("links outside the router", () => {
  it("open the housekeeping phone app under the white-label mount", async () => {
    expect(await phoneApp("/hotel/housekeeping")).toBe("/hotel/hk")
  })

  it("open the housekeeping phone app under the development mount", async () => {
    expect(await phoneApp("/housekeeping")).toBe("/hk")
  })
})
