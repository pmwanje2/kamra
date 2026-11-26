import fs from "node:fs"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

afterEach(() => vi.restoreAllMocks())
it.each(["collection", "item", "request", "response", "originalRequest"])("flags structured %s descriptions", level => {
  const description = { type: "text/plain", content: "Use Kamra for this payment." }
  const node = (where: string) => level === where ? { description } : {}
  const payload = { info: node("collection"), item: [{ name: "Folder", item: [{ name: "Pay", ...node("item"),
    request: node("request"), response: [{ ...node("response"), originalRequest: node("originalRequest") }],
  }] }] }
  const readFile = fs.readFileSync
  vi.spyOn(fs, "readFileSync").mockImplementation((path, options) => String(path).endsWith(".postman_collection.json")
    ? JSON.stringify(payload) : readFile(path, options))
  const error = vi.spyOn(console, "error").mockImplementation(() => {})
  expect(checkSource).toThrow(/branding findings/)
  expect(error.mock.calls.flat().join(" ")).toContain(description.content)
})

