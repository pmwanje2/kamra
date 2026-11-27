import fs from "node:fs"
import { expect, it } from "vitest"

const read = path => fs.readFileSync(new URL("../../" + path, import.meta.url), "utf8")
const method = "kamra.public_api.create_multi_room_booking"

it("documents the required checkout reference and supplies a usable Postman example", () => {
  const section = read("docs-site/api-reference.md").split("### `" + method + "`")[1].split("\n### ")[0]
  expect.soft(section).toMatch(/^\| `idempotency_key` \| yes \| {2}\|$/m)
  const collection = JSON.parse(read("docs-site/public/kamra.postman_collection.json"))
  const request = collection.item.flatMap(folder => folder.item)
    .find(item => item.request.url.path.at(-1) === method).request
  expect(JSON.parse(request.body.raw).idempotency_key).toMatch(/^[A-Za-z0-9_-]{16,128}$/)
})

it("runs the ordered owner-mobile and P7 suite under a single hosted bench invocation", () => {
  expect(read(".github/workflows/ci.yml")).toMatch(
    /bench --site test\.localhost run-tests --module kamra\.tests\.owner_mobile_p7_suite\b/,
  )
})
