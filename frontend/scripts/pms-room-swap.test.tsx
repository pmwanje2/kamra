// @vitest-environment jsdom
import { act } from "react"
import { createRoot, type Root } from "react-dom/client"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

const rpc = vi.hoisted(() => {
  Object.defineProperty(globalThis, "localStorage", { configurable: true, value: jsdom.window.localStorage })
  return vi.fn()
})
vi.mock("../src/lib/api", async (importOriginal) => ({
  ...await importOriginal<typeof import("../src/lib/api")>(),
  call: (method: string, params?: Record<string, unknown>) => rpc(method, params),
}))
import RoomChangeDialog from "../src/components/RoomChangeDialog"
import { setLang } from "../src/lib/dir"

let host: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  host = document.createElement("div")
  document.body.append(host)
  root = createRoot(host)
  rpc.mockReset().mockImplementation(async (method: string) => {
    if (method === "kamra.api.movable_rooms") return [{
      name: "Occupied-102", room_number: "102", room_type: "Standard", room_type_name: "Standard",
      housekeeping_status: "Ready", free: false, same_type: true, current: false, swap_reservation: "RES-2",
    }]
    if (method === "kamra.api.move_room") return { room: "Occupied-102", room_type: "Standard" }
    throw new Error(`Unexpected RPC ${method}`)
  })
})
afterEach(async () => {
  await act(async () => root.unmount())
  host.remove()
  setLang("en")
  vi.unstubAllGlobals()
})

it("offers an occupied same-type room and confirms a swap with both stays", async () => {
  const moved = vi.fn()
  await act(async () => root.render(<RoomChangeDialog reservation="RES-1" currentRoomNumber="101"
    onClose={() => {}} onMoved={moved} />))
  const target = [...host.querySelectorAll("button")].find((node) => node.textContent?.startsWith("102"))!
  expect(target.disabled).toBe(false)
  await act(async () => target.click())
  expect(host.textContent).toContain("Both reservations keep their booked rates.")
  const confirm = [...host.querySelectorAll("button")].find((node) => node.textContent === "Swap with room 102")!
  expect(confirm.disabled).toBe(false)
  await act(async () => confirm.click())
  expect(rpc).toHaveBeenCalledWith("kamra.api.move_room", expect.objectContaining({
    reservation: "RES-1", new_room: "Occupied-102", swap_reservation: "RES-2",
  }))
  expect(moved).toHaveBeenCalledOnce()
})

it("uses logical room search positions and padding with Arabic direction", async () => {
  setLang("ar")
  await act(async () => root.render(<RoomChangeDialog reservation="RES-1" onClose={() => {}} onMoved={() => {}} />))
  expect(document.documentElement.dir).toBe("rtl")
  const search = host.querySelector('header + div label input')!
  const icon = search.parentElement!.querySelector("svg")!
  expect(search.classList.contains("ps-8")).toBe(true)
  expect(search.classList.contains("pe-2")).toBe(true)
  expect(icon.classList.contains("start-2.5")).toBe(true)
  expect(search.className).not.toMatch(/\b(?:pl|pr)-/)
  expect(icon.getAttribute("class")).not.toMatch(/\bleft-/)
})
