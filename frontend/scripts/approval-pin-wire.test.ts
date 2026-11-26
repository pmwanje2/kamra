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

it.each([
  "kamra.api.check_in", "kamra.api.move_reservation", "kamra.api.move_room",
])("masks the supervisor PIN on the wire before hooks for %s", async (method) => {
  const fetch = vi.fn(async (_path: string, _init?: RequestInit) => Response.json({ message: { ok: true } }))
  vi.stubGlobal("fetch", fetch)
  await call(method, { reservation: "RES-1", supervisor_user: "manager@example.test", supervisor_pin: "4067" })
  const [, request] = fetch.mock.calls[0]
  expect(JSON.parse(String(request?.body))).toEqual({
    reservation: "RES-1", supervisor_user: "manager@example.test", pin_secret: "4067",
  })
})
