import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))
afterEach(() => vi.restoreAllMocks())

function referenceFixture(lines) {
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? lines.join("\r\n") : read(file, ...args))
  return vi.spyOn(console, "error").mockImplementation(() => {})
}

function expectFindings(lines, numbers) {
  const errors = referenceFixture(lines)
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n").split("\n")).toEqual(
    numbers.map(number => `api-reference.md:${number}: ${lines[number - 1]}`),
  )
}

it("resumes scanning when the list containing an unclosed fence ends", () => {
  expectFindings(["- Example", "", "  ~~~txt", "  safe", "", "Kamra Agent"], [6])
})

it("ignores blockquoted fences and indented code", () => {
  referenceFixture(["> ~~~txt", "> Kamra Agent", "> ~~~", "", "    Frappe example"])
  expect(checkSource).not.toThrow()
})

it("accepts normalized code spans for the real header and method", () => {
  referenceFixture(["Use ` X-Frappe-CSRF-Token ` or `` kamra.api.whoami ``."])
  expect(checkSource).not.toThrow()
})

it("checks unmatched backtick runs as visible text", () => {
  expectFindings(["Use `X-Frappe-CSRF-Token``-Something."], [1])
})

it("reports the source lines of HTML, captions, titles, headings and image text", () => {
  expectFindings([
    "<div>", "Kamra Agent", "</div>", "",
    "Public description", 'with <span title="Frappe">details</span>.', "",
    "[Kamra Agent](/setup)", "",
    '[Details](/setup "ERPNext setup")', "",
    "![Kamra Agent](/image.png)", "",
    "## Frappe setup", "",
    "[Download](/kamra.postman_collection.json)",
  ], [2, 6, 8, 10, 12, 14])
})
