import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { test } from "node:test"

test("published API reference presents the Maximus title and navigation", () => {
  const html = readFileSync(new URL("../.vitepress/dist/api-reference.html", import.meta.url), "utf8")
  assert.match(html.match(/<title>(.*?)<\/title>/s)?.[1] ?? "", /^Maximus REST API reference \| Maximus Docs$/)
  assert.doesNotMatch(html, /Kamra Docs|Why Kamra|Kamra PMS|kamrapms\.com/i)
})

