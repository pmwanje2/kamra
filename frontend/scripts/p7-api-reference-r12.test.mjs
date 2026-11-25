import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))
afterEach(() => vi.restoreAllMocks())

function useReference(markdown) {
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? markdown : read(file, ...args))
  return vi.spyOn(console, "error").mockImplementation(() => {})
}

it("scans the caption following a triple-backtick inline span", () => {
  const caption = "See [`Kamra Agent`](/setup) · roles: staff"
  const errors = useReference(["```GET```", "", caption].join("\n"))
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n")).toContain(`api-reference.md:3: ${caption}`)
})

it("flags an invented header displayed as a link caption", () => {
  const caption = "Use [`X-Frappe-Kamra-Agent`](/setup)."
  const errors = useReference(caption)
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n")).toContain(`api-reference.md:1: ${caption}`)
})

it("accepts the CSRF header and skips examples only until their matching fence closes", () => {
  const example = [
    "Use `X-Frappe-CSRF-Token`.",
    "   ````http", "```", "~~~", "```` trailing text", "Frappe in a code example", "`````\t",
    "~~~lang with `backticks`", "```", "Kamra in a second example", "~~~~",
  ]
  const caption = "See [`Kamra Agent`](/setup)."
  const errors = useReference([...example, caption].join("\r\n"))
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n").split("\n")).toEqual([
    `api-reference.md:${example.length + 1}: ${caption}`,
  ])
})
