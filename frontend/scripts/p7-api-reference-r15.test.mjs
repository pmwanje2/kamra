import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))
afterEach(() => vi.restoreAllMocks())

function checkLines(lines, expected) {
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? lines.join("\n") : read(file, ...args))
  const errors = vi.spyOn(console, "error").mockImplementation(() => {})
  if (expected.length) expect(checkSource).toThrow()
  else expect(checkSource).not.toThrow()
  expect(errors.mock.calls.flat().flatMap(message => message.split("\n"))).toEqual(
    expected.map(n => `api-reference.md:${n}: ${lines[n - 1]}`),
  )
}

it("finds a visible name across single-line and multiline comments at its starting line", () => {
  checkLines(["Use ERP<!-- product -->Next.", "See Ka<!-- internal", "note -->mra."], [1, 2])
})

it("joins rendered inline and reference link labels to surrounding prose", () => {
  checkLines([
    "[guide]: /manual", "", "Read Ka[m](/guide)ra.",
    'Read E[RP](/guide(v2) "manual")Next.', "Read Ka[m][guide]ra.",
    "Read Ka[m][undefined]ra.", "Read [manual](/Kamra).",
  ], [3, 4, 5, 7])
})

it("treats breaks, paragraphs, list items and table cells as word boundaries", () => {
  const tags = ["<br>", "<br/>", "<br />", '<BR title="a > b">', "<hr>", "<p>", "</p>",
    "<div>", "</div>", "<li>", "</li>", "<tr>", "<td>", "</td><th>", "</th>"]
  checkLines(tags.map(tag => `ERP${tag}Next`), [])
})

it("still joins text across inline formatting tags", () => {
  checkLines(["ERP<span>Next</span>"], [1])
})
