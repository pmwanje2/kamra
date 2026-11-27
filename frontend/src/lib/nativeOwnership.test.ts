import { describe, expect, it, vi } from "vitest"
import { upstreamOnly } from "./brand"

describe("site-owned assistant and point of sale", () => {
  it("hides disabled surfaces even without white-label branding", () => {
    vi.stubGlobal("window", { kamra_mcp_disabled: true, kamra_erp_pos_url: "/pos" })
    try {
      for (const route of ["/whatsapp", "/assistant", "/agents", "/settings/ai", "/settings/mcp-access", "/kitchen", "/inventory"]) {
        expect(upstreamOnly(route), route).toBe(true)
      }
      expect(upstreamOnly("/reservations")).toBe(false)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it("retains the standalone routes", () => {
    vi.stubGlobal("window", {})
    try {
      expect(upstreamOnly("/assistant")).toBe(false)
      expect(upstreamOnly("/kitchen")).toBe(false)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
