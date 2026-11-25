import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))
afterEach(() => vi.restoreAllMocks())

it("publishes white-label API descriptions and retains the real method names", () => {
  const md = fs.readFileSync(reference, "utf8")
  expect(md).toContain("### `kamra.api.csrf_token`")
  expect(md).not.toMatch(/Which parts of Kamra|Frappe rotates the token|ERPNext accounting bridge/i)
})

it("checks Markdown prose and link captions while allowing wire examples and download URLs", () => {
  const original = fs.readFileSync.bind(fs)
  let markdown = [
    "Call `kamra.api.csrf_token` using `X-Frappe-CSRF-Token`.",
    "[Download the collection](/kamra.postman_collection.json)",
    "```http", "POST /api/method/kamra.api.check_in", "```",
  ].join("\n")
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? markdown : original(file, ...args))
  const errors = vi.spyOn(console, "error").mockImplementation(() => {})
  expect(checkSource).not.toThrow()
  markdown += "\nCall `kamra.api.check_in` from Frappe.\n[ERPNext setup](/setup)"
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n")).toMatch(/api-reference\.md.*Frappe/)
  expect(errors.mock.calls.flat().join("\n")).toMatch(/api-reference\.md.*ERPNext/)
})
