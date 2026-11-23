/* Liquor is a transaction code like any other (OPERA routes only what an
   instruction or a company's billing rules include). No screen may still
   promise the old hidden rule - that liquor never reaches a company or group
   bill, or always stays with the guest. Checked over every UI string the
   app ships: the source and the translation catalog. */
import fs from "node:fs"
import path from "node:path"
import { describe, expect, it } from "vitest"

const root = path.resolve(__dirname, "..")
const PROMISE = /\b(liquor|alcohol)\b[^.;:"]*\b(never|always)\b|\b(never|always)\b[^.;:"]*\b(liquor|alcohol)\b/i

function sources(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const file = path.join(dir, entry.name)
    if (entry.isDirectory()) return sources(file)
    return /\.tsx?$/.test(file) ? [file] : []
  })
}

describe("No liquor rule is promised", () => {
  it("in the screens' text", () => {
    const found = sources(path.join(root, "src")).flatMap((file) =>
      fs.readFileSync(file, "utf8").split("\n").flatMap((line, i) =>
        /["'`]/.test(line) && PROMISE.test(line) ? [`${path.relative(root, file)}:${i + 1}`] : []))
    expect(found).toEqual([])
  })

  it("in the translation catalog", () => {
    const english = fs.readFileSync(path.join(root, "src/i18n/catalog.csv"), "utf8")
      .split("\n").filter((line) => PROMISE.test(line)).map((line) => line.slice(0, 80))
    expect(english).toEqual([])
  })
})
