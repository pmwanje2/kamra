// @vitest-environment jsdom
/** Follow-up mobile overflow regression test:
 * Reservations list page date filter ("Check-in from" / "Check-in to") must
 * wrap cleanly inside narrow 390px viewports instead of overflowing horizontally
 * by 18px on the right edge. */
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { MemoryRouter } from "react-router-dom"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("../src/lib/resource", () => ({
  listResource: vi.fn().mockResolvedValue([]),
  createResource: vi.fn(),
  updateResource: vi.fn(),
  deleteResource: vi.fn(),
  serverError: (e: unknown) => String(e),
}))

vi.mock("../src/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/api")>()
  return {
    ...actual,
    call: vi.fn().mockResolvedValue({}),
    getCurrentProperty: () => "Tank Hill Lodge",
    frappeFetch: vi.fn().mockResolvedValue({}),
  }
})

vi.mock("../src/lib/auth", () => ({
  useAuth: () => ({
    status: "authed",
    user: "desk@test",
    roles: ["Front Desk", "Hotel Admin"],
    refresh: async () => {},
    signOut: async () => {},
  }),
}))

vi.mock("../src/lib/money", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/lib/money")>()
  return {
    ...actual,
    cur: () => "UGX ",
    fmtAmount: (n: number) => `UGX ${n}`,
  }
})

import { ResourceScreen } from "../src/components/ResourceScreen"
import { reservationsConfig } from "../src/screens/configs"

;(globalThis as unknown as { IS_REACT_ACT_ENVIRONMENT: boolean }).IS_REACT_ACT_ENVIRONMENT = true

let host: HTMLDivElement
let root: Root

beforeEach(() => {
  host = document.createElement("div")
  document.body.appendChild(host)
  root = createRoot(host)
})

afterEach(async () => {
  await act(async () => {
    root.unmount()
  })
  host.remove()
})

describe("Reservations list mobile 390px date filter overflow", () => {
  it("wraps Check-in from/to date inputs inside a flex-wrap min-w-0 max-w-full container", async () => {
    await act(async () => {
      root.render(
        <MemoryRouter>
          <ResourceScreen config={reservationsConfig} />
        </MemoryRouter>,
      )
    })

    const fromInput = host.querySelector<HTMLInputElement>('input[aria-label="Check-in from"]')
    const toInput = host.querySelector<HTMLInputElement>('input[aria-label="Check-in to"]')
    expect(fromInput).not.toBeNull()
    expect(toInput).not.toBeNull()

    // Both date inputs must allow shrinking within mobile viewport widths
    for (const input of [fromInput!, toInput!]) {
      expect(input.className).toContain("min-w-0")
      expect(input.className).toContain("max-w-full")
    }

    // "to" label and "Check-in to" input stay paired in their own flex item so
    // the second input wraps cleanly below the first without stranding "to"
    const toPair = toInput!.parentElement!
    expect(toPair.textContent?.trim()).toBe("to")
    expect(toPair.className).toContain("min-w-0")

    // The date-filter group itself must wrap and stay bounded to 100% width
    const dateFilterGroup = toPair.parentElement!
    expect(dateFilterGroup.contains(fromInput!)).toBe(true)
    expect(dateFilterGroup.contains(toInput!)).toBe(true)
    expect(dateFilterGroup.className).toContain("flex-wrap")
    expect(dateFilterGroup.className).toContain("min-w-0")
    expect(dateFilterGroup.className).toContain("max-w-full")
  })
})
