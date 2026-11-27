import { afterEach, expect, it, vi } from "vitest"
import { upstreamOnly } from "./brand"

afterEach(() => vi.unstubAllGlobals())

it("gates the activity feed with its RPC independently of branding", () => {
  vi.stubGlobal("window", { kamra_mcp_disabled: true })
  expect(upstreamOnly("/activity")).toBe(true)
  vi.stubGlobal("window", {})
  expect(upstreamOnly("/activity")).toBe(false)
})
