#!/usr/bin/env node
/** The Late check-out step starts on the property's own fee and stops a fee
 * bigger than one night (TKH-12): a cashier once typed the whole bill
 * (423,619) into the late-fee box. */
import assert from "node:assert/strict"
import fs from "node:fs"
import ts from "typescript"

const load = (rel) => {
  const filename = new URL(rel, import.meta.url)
  const code = ts.transpileModule(fs.readFileSync(filename, "utf8"), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  new Function("exports", code)(exports)
  return exports
}

const { lateFeeDefault, lateFeeExceedsNight } = load("../src/lib/lateFee.ts")

// the property's rule fills the box; no rule leaves it empty
assert.equal(lateFeeDefault({ suggested_fee: 25000 }), "25000")
assert.equal(lateFeeDefault({ suggested_fee: 40000.5 }), "40000.5")
assert.equal(lateFeeDefault({ suggested_fee: null }), "")
assert.equal(lateFeeDefault({ suggested_fee: 0 }), "")
assert.equal(lateFeeDefault(null), "")

// more than one night's room rate needs an explicit confirmation
assert.equal(lateFeeExceedsNight(423619, 80000), true)
assert.equal(lateFeeExceedsNight(80000.01, 80000), true)
assert.equal(lateFeeExceedsNight(80000, 80000), false)
assert.equal(lateFeeExceedsNight(25000, 80000), false)
// no night rate known (nothing posted yet): nothing to compare against
assert.equal(lateFeeExceedsNight(423619, 0), false)
assert.equal(lateFeeExceedsNight(423619, undefined), false)

const dialog = fs.readFileSync(new URL("../src/components/CheckOutDialog.tsx", import.meta.url), "utf8")
assert.ok(dialog.includes("lateFeeDefault("), "CheckOutDialog pre-fills the late fee from lateFeeDefault")
assert.ok(dialog.includes("lateFeeExceedsNight("), "CheckOutDialog checks the fee against one night")
assert.ok(dialog.includes("This late fee exceeds one night's room rate"),
  "CheckOutDialog warns when the late fee exceeds one night")

console.log("late check-out fee: ok")
