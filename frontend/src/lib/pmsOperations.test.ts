// @vitest-environment jsdom
import { describe, expect, it } from "vitest"
import { formatMoveDelta, housekeepingWorkRows } from "./pmsOperations"

describe("room moves", () => {
  it.each([
    ["UGX", 0, "UGX 12"],
    ["USD", 2, "$12.35"],
    ["KWD", 3, "KWD 12.346"],
  ])("formats a server quote in %s with %s minor units", (currency, places, expected) => {
    expect(formatMoveDelta(12.3456, currency, places)).toBe(expected)
  })
})

describe("housekeeping board", () => {
  const rows = [
    { room: "101", discrepancy: "Skip", turndown_status: "Pending" },
    { room: "102", discrepancy: "Sleep", turndown_status: "DND" },
    { room: "103", discrepancy: null, turndown_status: "Completed" },
    { room: "104", discrepancy: null, turndown_status: "Not Required" },
  ]

  it("keeps both discrepancies visible and omits rooms without turndown from the evening board", () => {
    expect(housekeepingWorkRows(rows, "discrepancies").map((row) => row.room)).toEqual(["101", "102"])
    expect(housekeepingWorkRows(rows, "turndown").map((row) => row.room)).toEqual(["101", "102", "103"])
  })
})
