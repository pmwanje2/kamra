import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { expect, it } from "vitest"
import { scanPublishedApi } from "./brand-leak-check.mjs"

const reference = fileURLToPath(new URL("../../docs-site/api-reference.md", import.meta.url))

it("publishes white-label API descriptions and retains the real method names", () => {
  const md = fs.readFileSync(reference, "utf8")
  expect(md).toContain("### `kamra.api.csrf_token`")
  expect(md).not.toMatch(/Which parts of Kamra|Frappe rotates the token|ERPNext accounting bridge/i)
})

it("checks Markdown prose and link captions while allowing wire examples and download URLs", () => {
  const markdown = [
    "Call `kamra.api.csrf_token` using `X-Frappe-CSRF-Token`.",
    "[Download the collection](/kamra.postman_collection.json)",
    "```http", "POST /api/method/kamra.api.check_in", "```",
  ].join("\n")
  const collection = { item: ["kamra.api.csrf_token", "kamra.api.check_in"].map(method => ({
    request: { url: { path: ["api", "method", method] } },
  })) }
  expect(scanPublishedApi(markdown, collection)).toEqual([])
  const leaks = markdown + "\nCall `kamra.api.check_in` from Frappe.\n[ERPNext setup](/setup)"
  expect(scanPublishedApi(leaks, collection)).toEqual([
    expect.stringMatching(/api-reference\.md.*Frappe/),
    expect.stringMatching(/api-reference\.md.*ERPNext/),
  ])
})
