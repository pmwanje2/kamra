import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))
afterEach(() => vi.restoreAllMocks())

function checkReference(lines, badLines = []) {
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? lines.join("\r\n") : read(file, ...args))
  const errors = vi.spyOn(console, "error").mockImplementation(() => {})
  if (!badLines.length) {
    expect(checkSource).not.toThrow()
    return
  }
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n").split("\n")).toEqual(
    badLines.map(number => `api-reference.md:${number}: ${lines[number - 1]}`),
  )
}

it("checks branded destinations and unused or duplicate reference definitions at their source lines", () => {
  checkReference([
    "[manual]: file:///Kamra",
    "[manual]:", "  <https://example.com/Frappe>",
    '[download]: /public "ERPNext setup"',
    "", String.raw`[guide](file\:///Kamra)`,
    "[guide](https://example.com/Kamra)",
    "[lookalike](#kamra-api-unknown)",
    "[lookalike](/api/method/kamra.api.whoami?next=1)",
    "[download](/kamra.postman_collection.json.bak)",
  ], [1, 3, 4, 6, 7, 8, 9, 10])
})

it("detects names split by formatting, escapes or inline HTML in prose, captions and image alt text", () => {
  const lines = [
    "![ERP**Next** setup](/logo.png)",
    "ERP**Next**", "Ka_m_ra", "Frap~~pe~~", String.raw`Ka\mra`,
    String.raw`Ka\*mra`, String.raw`ERP\Next`,
    'Read ERP<span title="next > part">Next</span> setup.',
    "[ERP**Next** setup](/guide)",
  ]
  checkReference(lines, lines.map((_, i) => i + 1))
})

it("accepts exact documented wire destinations, method spans and the CSRF header", () => {
  checkReference([
    "[method]: </api/method/kamra.api.csrf_token>",
    "", "[Endpoint](#kamra-api-csrf-token) or [call][method].",
    "[Call](/api/method/kamra.api.whoami)",
    "[Download](/kamra.postman_collection.json)",
    "Use `kamra.api.whoami` with ` X-Frappe-CSRF-Token `.",
    "```http", "POST https://example.com/api/method/kamra.api.check_in", "```",
  ])
})
