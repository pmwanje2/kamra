import fs from "node:fs"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

afterEach(() => vi.restoreAllMocks())

it.each(["index.md", "es/index.md", "ar/index.md", "self-hosting/bench.md"])("checks prose and code examples in %s", file => {
  const read = fs.readFileSync
  const marker = "Choose Kamra for this hotel."
  vi.spyOn(fs, "readFileSync").mockImplementation((path, options) => {
    const content = read(path, options)
    return String(path).endsWith("/docs-site/" + file) ? content + "\n`" + marker + "`\n" : content
  })
  const errors = vi.spyOn(console, "error").mockImplementation(() => {})
  expect(checkSource).toThrow()
  expect(errors.mock.calls.flat().join("\n")).toContain(marker)
})

