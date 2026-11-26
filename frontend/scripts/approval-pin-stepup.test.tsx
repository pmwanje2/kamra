// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { MyApprovalPinModal } from "../src/components/MyApprovalPinModal"

const { rpc, status } = vi.hoisted(() => ({ rpc: vi.fn(), status: vi.fn() }))
vi.mock("../src/lib/api", () => ({ call: rpc, frappeFetch: status }))
vi.mock("../src/lib/cashierAuth", () => ({ useCashierAuth: () => ({ refresh: vi.fn() }) }))
vi.mock("../src/lib/i18n", () => ({ useT: () => ({ t: (text: string) => text }) }))
vi.mock("../src/lib/resource", () => ({ serverError: (error: Error) => error.message }))

const descriptor = { mode: "identity_provider", label: "Company login", purpose: "floor_pin", start: "company.auth.start" }
const popup = { location: "", closed: false, close: vi.fn() }
let root: ReturnType<typeof createRoot>
let host: HTMLDivElement
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  HTMLDialogElement.prototype.showModal = vi.fn()
  HTMLDialogElement.prototype.close = vi.fn()
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
  vi.spyOn(window, "open").mockReturnValue(popup as unknown as Window)
  rpc.mockReset().mockResolvedValue({ authorize_url: "https://login.example.test/authorize" })
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function render(reauth: object) {
  status.mockResolvedValue({ message: { reauth } })
  await act(async () => root.render(<MyApprovalPinModal onClose={() => {}} />))
  for (const input of host.querySelectorAll("input")) {
    const value = input.autocomplete === "current-password" ? "local-password" : "4067"
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, value)
      input.dispatchEvent(new Event("input", { bubbles: true }))
    })
  }
}
const save = () => host.querySelector<HTMLButtonElement>('button[type="submit"]')!.click()
const data = { type: "siteops-reauth", purpose: "floor_pin", ok: true }
async function message(overrides: Record<string, unknown> = {}) {
  await act(async () => window.dispatchEvent(Object.assign(new Event("message"), {
    data, origin: location.origin, source: popup, ...overrides,
  })))
}
it("opens synchronously, accepts only the bound callback, and sends only the PIN", async () => {
  await render(descriptor)
  expect(host.querySelector('[autocomplete="current-password"]')).toBeNull()
  expect(host.textContent).toContain("Confirm it's you (Company login)")
  await act(async () => {
    save()
    expect(vi.mocked(window.open).mock.calls[0]?.[0]).toBe("about:blank")
  })
  expect(rpc.mock.calls).toEqual([[descriptor.start, { purpose: "floor_pin" }]])
  expect(String(popup.location)).toBe("https://login.example.test/authorize")
  for (const wrong of [{ source: window }, { origin: "https://other.example" },
    { data: { ...data, type: "other" } }, { data: { ...data, purpose: "other" } }]) await message(wrong)
  expect(rpc).toHaveBeenCalledTimes(1)
  await message()
  await message()
  expect(rpc.mock.calls.slice(1)).toEqual([["kamra.api.set_cashier_pin", { pin: "4067" }]])
  expect(JSON.stringify([localStorage, sessionStorage, location.href, String(popup.location)])).not.toContain("4067")
})
it.each(["/api/method/x", "no_dot", "bad.A", "safe.start?x", "safe.start\n"])("rejects invalid method %j", async start => {
  await render({ ...descriptor, start })
  await act(async () => save())
  expect(rpc).not.toHaveBeenCalled()
  expect(host.querySelector('[role="alert"]')).toBeTruthy()
})
it("lets the user retry refusal with a fresh popup", async () => {
  await render(descriptor)
  await act(async () => save())
  rpc.mockRejectedValueOnce(Object.assign(new Error("Refused"), { status: 417, body: "[SITEOPS-DOA-REAUTH]" }))
  await message()
  expect(host.querySelector('[role="alert"]')?.textContent).toMatch(/try again|retry/i)
  await act(async () => save())
  expect(window.open).toHaveBeenCalledTimes(2)
  await message({ data: { ...data, ok: "true" } })
  expect(rpc.mock.calls.filter(([method]) => method === "kamra.api.set_cashier_pin")).toHaveLength(1)
})
it("keeps local password enrollment", async () => {
  await render({ mode: "password" })
  await act(async () => save())
  expect(window.open).not.toHaveBeenCalled()
  expect(rpc).toHaveBeenCalledWith("kamra.api.set_cashier_pin", { pin: "4067", current_password: "local-password" })
})
