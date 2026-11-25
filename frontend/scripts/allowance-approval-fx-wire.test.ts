// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest"
import { call } from "../src/lib/api"

afterEach(() => vi.unstubAllGlobals())

it.each([
  ["kamra.api.post_discount", { folio: "F-1", kind: "Amount", value: 20, discount_currency: "USD" },
    { pin: "4067" }, { pin_secret: "4067" }],
  ["kamra.api.decide_allowance", { allowance_request: "ALW-1", approve: 1 },
    { pin: "4067" }, { pin_secret: "4067" }],
  ["kamra.api.decide_allowance", { allowance_request: "ALW-1", approve: 1, supervisor: "manager@example.test" },
    { supervisor_pin: "4067" }, { confirm_pin_secret: "4067" }],
])("preserves discount and approval parameters when masking the PIN for %s", async (method, fields, credential, wire) => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => Response.json({ message: { ok: true } }))
  vi.stubGlobal("fetch", fetch)
  await call(method, { ...fields, ...credential })
  const [path, request] = fetch.mock.calls[0]
  expect(path).toBe(`/api/method/${method}`)
  expect(request?.method).toBe("POST")
  expect(JSON.parse(String(request?.body))).toEqual({ ...fields, ...wire })
})
