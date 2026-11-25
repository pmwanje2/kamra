// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { expect, it, vi } from "vitest"
import { CashierAuthProvider, useCashierAuth } from "../src/lib/cashierAuth"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({ call: rpc, getCurrentProperty: () => "Test Hotel" }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ status: "authed" }) }))

function Till() {
  const { ensureUnlocked } = useCashierAuth()
  return <button onClick={() => { void ensureUnlocked().catch(() => {}) }}>Open till</button>
}

it("collects an account password and both PIN entries before enrolling on a till", async () => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  const dialog = HTMLDialogElement.prototype
  if (!dialog.showModal) dialog.showModal = function () { this.open = true }
  if (!dialog.close) dialog.close = function () { this.open = false }
  rpc.mockImplementation(async (method) => method === "kamra.api.cashier_pin_status"
    ? { required: true, has_pin: false, unlocked: false }
    : { ok: true })
  const host = document.createElement("div")
  document.body.append(host)
  const root = createRoot(host)
  const button = (label: string) => [...host.querySelectorAll("button")].find((item) => item.textContent === label)!
  const click = (label: string) => act(async () => button(label).click())

  try {
    await act(async () => root.render(<CashierAuthProvider><Till /></CashierAuthProvider>))
    await click("Open till")
    for (const digit of "4067") await click(digit)
    await click("Set PIN")
    for (const digit of "4067") await click(digit)
    const password = host.querySelector<HTMLInputElement>('input[autocomplete="current-password"]')
    expect(password?.type).toBe("password")
    expect(button("Confirm").disabled).toBe(true)
    expect(rpc.mock.calls.some(([method]) => method === "kamra.api.set_cashier_pin")).toBe(false)

    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(password, "synthetic-password")
      password!.dispatchEvent(new Event("input", { bubbles: true }))
    })
    await click("Confirm")

    expect(rpc).toHaveBeenCalledWith("kamra.api.set_cashier_pin", {
      pin: "4067", current_password: "synthetic-password",
    })
    expect(rpc).toHaveBeenCalledWith("kamra.api.verify_cashier_pin", { property: "Test Hotel", pin: "4067" })
    expect(host.querySelector("dialog")).toBeNull()
  } finally {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  }
})
