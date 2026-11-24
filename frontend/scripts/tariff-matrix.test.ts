import { describe, expect, it } from "vitest"
import { adjustRate, adultsTier, extraKind, roomMatrix, roundRate } from "../src/lib/tariffMatrix"

const room = (item_code: string, occupancy: string | null, extra: Record<string, unknown> = {}) => ({
  item_code, item_name: item_code, category: "Room", room_type: "Deluxe", occupancy,
  price_list: "Corporate USD", customer: null as string | null, ...extra,
})
const supplement = (item_code: string, extra: Record<string, unknown> = {}) => ({
  item_code, item_name: item_code, category: "Supplement", room_type: null as string | null, occupancy: null,
  price_list: "Corporate USD", customer: null as string | null, ...extra,
})

describe("OPERA pricing schedule tiers", () => {
  it.each([
    ["Single", null, 1], ["Double", null, 2], ["Triple", null, 3], ["Quad", null, 4],
    ["1 Adult", null, 1], ["3 Adults", null, 3], ["5 adults", null, 5],
    [null, "TAR-DELUXE-2A", 2], [null, "TAR-SUITE-4A", 4], [null, "DLX", null], ["Family", "DLX", null],
  ])("reads %s / %s as %s adults", (occupancy, code, tier) => {
    expect(adultsTier(occupancy, code ?? undefined)).toBe(tier)
  })

  it("recognises extra adult, child and bed supplements by occupancy or legacy code", () => {
    expect(extraKind(supplement("TAR-EXTRA-ADULT"))).toBe("adult")
    expect(extraKind(supplement("TAR-CHILD"))).toBe("child")
    expect(extraKind(supplement("TAR-EXTRA-BED"))).toBe("bed")
    expect(extraKind(supplement("DLX-EA", { occupancy: "Extra Adult" }))).toBe("adult")
    expect(extraKind(supplement("DLX-EC", { occupancy: "Extra Child" }))).toBe("child")
    expect(extraKind(supplement("COT", { item_name: "Baby cot" }))).toBeNull()
  })
})

describe("rate rounding and reference adjustments", () => {
  it("rounds to a step in each mode, or to the currency minor unit", () => {
    expect(roundRate(526268.4, 1000)).toBe(526000)
    expect(roundRate(526500, 1000)).toBe(527000)
    expect(roundRate(526001, 1000, "Up")).toBe(527000)
    expect(roundRate(526999, 1000, "Down")).toBe(526000)
    expect(roundRate(117.25, 5, "Up")).toBe(120)
    expect(roundRate(12.33, 0.05)).toBe(12.35)
    expect(roundRate(136.125, 0, "Nearest", "USD")).toBe(136.13)
    expect(roundRate(566751.02, 0, "Nearest", "UGX")).toBe(566751)
    expect(roundRate(1.2345, 0, "Nearest", "KWD")).toBe(1.235)
  })

  it("adjusts a reference rate by percent, fixed amount or an exact figure", () => {
    expect(adjustRate(130, { kind: "percent", value: 5 })).toBe(136.5)
    expect(adjustRate(130, { kind: "percent", value: -10 })).toBe(117)
    expect(adjustRate(130, { kind: "amount", value: -10 })).toBe(120)
    expect(adjustRate(130, { kind: "exact", value: 99 })).toBe(99)
  })
})

describe("room pricing matrix", () => {
  it("puts legacy and multi-tier occupancy rows of a room type side by side", () => {
    const rows = roomMatrix([
      room("DLX", "Double"), room("DLX-1A", "Single"), room("DLX-3A", "3 Adults"),
      room("DLX", "Double", { customer: "Client Co" }),
      room("STE-2A", null, { room_type: "Suite" }), room("STE-X", "Family", { room_type: "Suite" }),
    ], [])
    expect(rows.map((row) => [row.key, [...row.tiers.keys()], row.others.map((item) => item.item_code)])).toEqual([
      ["Corporate USD|Deluxe|", [1, 2, 3], []],
      ["Corporate USD|Deluxe|Client Co", [2], []],
      ["Corporate USD|Suite|", [2], ["STE-X"]],
    ])
    expect(rows[0].tiers.get(1)?.item_code).toBe("DLX-1A")
  })

  it("attaches room-type extras before the price list's global supplements", () => {
    const rows = roomMatrix([room("DLX", "Double"), room("STD", "Double", { room_type: "Standard" })], [
      supplement("TAR-EXTRA-ADULT"), supplement("TAR-CHILD"),
      supplement("DLX-EA", { room_type: "Deluxe", occupancy: "Extra Adult" }),
      supplement("TAR-EXTRA-BED", { price_list: "Local rates" }),
    ])
    expect(rows.map((row) => [row.roomType, row.extras.adult?.item_code, row.extras.child?.item_code, row.extras.bed]))
      .toEqual([["Deluxe", "DLX-EA", "TAR-CHILD", undefined], ["Standard", "TAR-EXTRA-ADULT", "TAR-CHILD", undefined]])
  })
})
