import { execFileSync } from "node:child_process"
import { fileURLToPath } from "node:url"
import { expect, it } from "vitest"

it("keeps the Advance bill check compatible with currency-sensitive previews", () => {
  const script = fileURLToPath(new URL("./discount-advance-bill-check.mjs", import.meta.url))
  expect(execFileSync(process.execPath, [script], { encoding: "utf8", stdio: "pipe" }))
    .toContain("discount-advance-bill-check: ok")
})
