import fs from "node:fs"
import { fileURLToPath } from "node:url"
import { afterEach, expect, it, vi } from "vitest"
import { checkSource } from "./brand-leak-check.mjs"

const docs = new URL("../../docs-site/", import.meta.url)
const reference = fileURLToPath(new URL("api-reference.md", docs))
const markdown = fs.readFileSync(reference, "utf8")
const collection = JSON.parse(fs.readFileSync(new URL("public/kamra.postman_collection.json", docs), "utf8"))

afterEach(() => vi.restoreAllMocks())

it("publishes Maximus Assistant in the enabled_modules role list and removes the internal role label throughout", () => {
  const moduleRoles = markdown.match(/^### `kamra\.api\.enabled_modules`\n\n([^\n]+)/m)?.[1]
  expect(moduleRoles).toContain("`Maximus Assistant`")
  expect(markdown.split("\n").filter(line => line.includes("Kamra Agent"))).toEqual([])
})

it("describes list permissions and workflow approval without exposing server symbols in either publication", () => {
  const internal = /\b(?:frappe|erpnext)\.\w+|\bkamra\.(?:credit|privacy|checkout|checkin|night_audit)\b/i
  const descriptions = collection.item.flatMap(folder => folder.item.map(item => item.request.description))
  for (const lines of [markdown.split("\n").filter(line => !line.startsWith("### ")), descriptions]) {
    expect(lines.filter(line => internal.test(line))).toEqual([])
    expect(lines.some(line => line.includes("User Permissions"))).toBe(true)
    expect(lines.some(line => /workflow.*(?:action|engine)/i.test(line))).toBe(true)
  }
})

it("flags internal role captions and private symbols while allowing documented calls and wire headers", () => {
  const badLines = [
    "See [`Kamra Agent`](/setup) · roles: staff",
    "Access checks use `frappe.get_list`.",
    "Approvals call `frappe.model.workflow.apply_workflow`.",
    "Read `kamra.api._private_helper`.",
  ]
  const allowed = "Call `kamra.api.whoami` with `X-Frappe-CSRF-Token`."
  const read = fs.readFileSync.bind(fs)
  vi.spyOn(fs, "readFileSync").mockImplementation((file, ...args) =>
    file === reference ? [allowed, ...badLines].join("\n") : read(file, ...args))
  const errors = vi.spyOn(console, "error").mockImplementation(() => {})
  expect(checkSource).toThrow()
  const output = errors.mock.calls.flat().join("\n")
  for (const line of badLines) expect(output).toContain(line)
  expect(output).not.toContain(allowed)
})
