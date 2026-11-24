/* FolioView charge sharing (OPERA): an even 3-way split must not send
   33.33% (which skews 100,001 UGX by 22 shillings). Leaving shares at the
   even default omits `shares` so the backend cuts exact 1/N parts in whole
   minor units with the remainder on the source window; explicit custom
   percentages are forwarded as entered. */
import fs from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"
import * as FolioLabelModule from "../src/lib/folioLabel"

const shareFolioArgs = (
  FolioLabelModule as unknown as {
    shareFolioArgs?: (
      fromFolio: string,
      chargeRows: string[],
      toFolios: string[],
      shares: Record<string, string>,
    ) => { from_folio: string; charge_rows: string[]; to_folios: string[]; shares?: number[] }
  }
).shareFolioArgs

describe("FolioView share charges payload", () => {
  it("omits rounded 33.33% shares on an even 3-way split so the backend uses exact 1/3", () => {
    expect(typeof shareFolioArgs).toBe("function")
    const untouched = shareFolioArgs!("F-1", ["ROW-1", "ROW-2"], ["F-2", "F-3"], {})
    expect(untouched).toEqual({
      from_folio: "F-1",
      charge_rows: ["ROW-1", "ROW-2"],
      to_folios: ["F-2", "F-3"],
    })
    expect("shares" in untouched).toBe(false)

    // Still exact 1/3 if inputs hold the displayed default "33.33"
    const displayedDefault = shareFolioArgs!("F-1", ["ROW-1"], ["F-2", "F-3"], {
      "F-2": "33.33",
      "F-3": "33.33",
    })
    expect("shares" in displayedDefault).toBe(false)
  })

  it("forwards custom percentages when the operator overrides any bill's share", () => {
    expect(typeof shareFolioArgs).toBe("function")
    const custom = shareFolioArgs!("F-1", ["ROW-1"], ["F-2", "F-3"], {
      "F-2": "25",
      "F-3": "40",
    })
    expect(custom).toEqual({
      from_folio: "F-1",
      charge_rows: ["ROW-1"],
      to_folios: ["F-2", "F-3"],
      shares: [25, 40],
    })
  })

  it("wires FolioView.tsx share_folio_charges through shareFolioArgs (never hardcoded toFixed(2) shares)", () => {
    const src = fs.readFileSync(
      path.resolve(__dirname, "../src/screens/FolioView.tsx"),
      "utf8",
    )
    expect(src).toContain("shareFolioArgs(")
    expect(src).not.toMatch(/share_folio_charges[\s\S]{0,250}shares:\s*bills\.map/)
  })
})
