// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest"
import { call } from "../src/lib/api"

afterEach(() => vi.unstubAllGlobals())

it.each([
  ["kamra.api.set_cashier_pin", { current_password: "example-account-password" }],
  ["kamra.api.verify_cashier_pin", { property: "Test Hotel" }],
])("posts only Frappe-redacted PIN credentials to %s", async (method, fields) => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => Response.json({ message: { ok: true } }))
  vi.stubGlobal("fetch", fetch)
  await call(method, { ...fields, pin: "4067" })
  const [path, request] = fetch.mock.calls[0]
  expect(path).toBe(`/api/method/${method}`)
  expect(request?.method).toBe("POST")
  expect(JSON.parse(String(request?.body))).toEqual({ ...fields, pin_secret: "4067" })
})
