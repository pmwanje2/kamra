#!/usr/bin/env node
/** Post a charge starts on a tax rate the property actually charges (TKH-08).
 * A hotel whose rates are 0% must not open the box on 5%: the bill would
 * carry made-up tax, or the ERP refuses the line. */
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

const { defaultChargeTaxRate } = load("../src/lib/chargeTax.ts")

// a property that charges no tax: only 0% is offered, and the box starts there
assert.equal(defaultChargeTaxRate([0], 0), "0")
// generic pack, property rate 0%: 0%, not the first taxed option
assert.equal(defaultChargeTaxRate([0, 5, 10, 12, 15, 20], 0), "0")
// the property's own services rate when the pack offers it
assert.equal(defaultChargeTaxRate([0, 5, 10, 12, 15, 20], 12), "12")
assert.equal(defaultChargeTaxRate([0, 5, 12, 18, 28], 18), "18")
// a services rate the pack does not offer: the lowest offered rate, never a refused one
assert.equal(defaultChargeTaxRate([0, 7], 5), "0")
assert.equal(defaultChargeTaxRate([5, 15], undefined), "5")
// locale not loaded yet
assert.equal(defaultChargeTaxRate([], undefined), "0")

const folio = fs.readFileSync(new URL("../src/screens/FolioView.tsx", import.meta.url), "utf8")
assert.ok(!/gst_rate:\s*"5"/.test(folio), "FolioView must not hardcode a 5% charge tax")
assert.ok(folio.includes("defaultChargeTaxRate("), "FolioView starts the charge tax at defaultChargeTaxRate")

console.log("charge tax default: ok")
