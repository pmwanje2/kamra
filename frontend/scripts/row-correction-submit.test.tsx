// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import RowCorrection from "../src/components/RowCorrection"
import correctionSource from "../src/components/RowCorrection.tsx?raw"

const { rpc } = vi.hoisted(() => ({ rpc: vi.fn() }))
vi.mock("../src/lib/api", () => ({
  call: rpc,
  getCurrentProperty: () => "Test Hotel",
  newIdempotencyKey: () => "correction-key",
}))

const correctPayment = "kamra.api.correct_folio_payment"
const supervisor = "supervisor@example.test"

describe("Confirm correction", () => {
  let host: HTMLDivElement
  let root: Root

  beforeEach(() => {
    vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
    const dialog = HTMLDialogElement.prototype
    // jsdom may omit the native dialog methods.
    if (!dialog.showModal) dialog.showModal = function () { this.open = true }
    if (!dialog.close) dialog.close = function () { this.open = false }
    rpc.mockReset()
    host = document.createElement("div")
    document.body.append(host)
    root = createRoot(host)
  })

  afterEach(async () => {
    await act(async () => root.unmount())
    host.remove()
    vi.unstubAllGlobals()
  })

  async function click(label: string, container: ParentNode = host) {
    const button = [...container.querySelectorAll("button")].find(
      (candidate) => candidate.textContent === label,
    )
    expect(button, label).toBeDefined()
    await act(async () => button!.click())
  }

  async function openCorrection(requiresApproval: boolean) {
    rpc.mockImplementation(async (method: string) => {
      if (method === "kamra.api.payment_correction_options") {
        return {
          actions: [{
            id: "reverse_payment",
            label: "Reverse payment",
            requires: [],
            reason_codes: [{
              name: "DUPLICATE",
              description: "Duplicate payment",
              requires_supervisor: Number(requiresApproval),
            }],
          }],
          blocked: [],
          supervisors: [{ user: supervisor, full_name: "Shift Supervisor", has_pin: true }],
          refund_cap: 0,
        }
      }
      return {}
    })
    await act(async () => root.render(
      <RowCorrection folio="FOLIO-1" row="PAY-1" home="USD" modes={[]}
        act={async (submit) => { await submit() }} />,
    ))
    await click("Correct…")
    const note = host.querySelector<HTMLInputElement>('input[aria-label="Reason note"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(note, "Duplicate receipt")
      note.dispatchEvent(new Event("input", { bubbles: true }))
      const select = host.querySelector<HTMLSelectElement>('select[aria-label="Supervisor user"]')
      if (select) {
        select.value = supervisor
        select.dispatchEvent(new Event("change", { bubbles: true }))
      }
    })
  }

  it("binds the button directly so React passes its click event to submit", () => {
    const directBinding = /onClick=\{submit\}\s*>\s*\{t\("Confirm correction"\)\}/
    expect(directBinding.test(correctionSource), "Confirm correction must pass the click event to submit").toBe(true)
  })

  it("opens the PIN pad on click and submits only the PIN entered there", async () => {
    await openCorrection(true)
    await click("Confirm correction")

    expect(rpc).not.toHaveBeenCalledWith(correctPayment, expect.anything())
    const pad = host.querySelector("dialog[open]")
    expect(pad?.textContent).toContain("Supervisor PIN")
    for (const digit of "0456") await click(digit, pad!)
    await click("Approve correction", pad!)

    expect(rpc).toHaveBeenCalledWith(correctPayment, expect.objectContaining({
      supervisor_user: supervisor,
      supervisor_pin: "0456",
    }))
  })

  it("submits on click without a PIN when approval is unnecessary", async () => {
    await openCorrection(false)
    await click("Confirm correction")

    expect(host.querySelector("dialog")).toBeNull()
    expect(rpc).toHaveBeenCalledWith(correctPayment, expect.objectContaining({
      supervisor_user: undefined,
      supervisor_pin: undefined,
    }))
  })
})
