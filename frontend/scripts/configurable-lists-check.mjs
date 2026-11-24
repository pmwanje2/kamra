#!/usr/bin/env node
/**
 * PMS parity PR-5: hotel taxonomies come from the server's masters, never a
 * list typed into a screen, and the setup wizard suggests no rupee amounts.
 *
 * Market codes, booker relations and cancellation reasons are client
 * masters (Settings data, OPERA Market / Cancellation Reason codes): the
 * desk reads them from property_locale. A string array in a screen holding
 * those values is the hardcoding this check exists to catch. The meal plan
 * form picks a Meal Plan Code master instead of a fixed EP/CP/MAP/AP list.
 */
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const read = (rel) => fs.readFileSync(path.join(root, rel), "utf8")
const parse = (rel) => ts.createSourceFile(rel, read(rel), ts.ScriptTarget.Latest, true)

/** Every array literal made only of string literals, as plain string lists. */
function stringArrays(source) {
  const out = []
  const visit = (node) => {
    if (ts.isArrayLiteralExpression(node) && node.elements.length > 1
        && node.elements.every((e) => ts.isStringLiteral(e))) {
      out.push(node.elements.map((e) => e.text))
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return out
}

/** Property assignments `name: "literal"` anywhere in the file. */
function literalProps(source, name) {
  const out = []
  const visit = (node) => {
    if (ts.isPropertyAssignment(node) && node.name.getText(source) === name
        && ts.isStringLiteral(node.initializer)) out.push(node.initializer.text)
    ts.forEachChild(node, visit)
  }
  visit(source)
  return out
}

const MASTER_VALUES = {
  "market codes": ["Airline Crew", "Long Stay", "MICE", "Government"],
  "booker relations": ["Company Travel Desk", "Assistant"],
  "cancellation reasons": ["Change of plans", "Duplicate booking", "Weather / travel disruption"],
}

for (const rel of ["src/components/BookingDialog.tsx", "src/components/CancelPanel.tsx",
  "src/screens/CRS.tsx"]) {
  for (const list of stringArrays(parse(rel))) {
    for (const [what, values] of Object.entries(MASTER_VALUES)) {
      assert.ok(!values.some((v) => list.includes(v)),
        `${rel}: ${what} are a client master (property_locale), not a typed list: ${JSON.stringify(list)}`)
    }
  }
}

const booking = read("src/components/BookingDialog.tsx")
assert.match(booking, /market_codes/, "BookingDialog: market segment options come from loc.market_codes")
assert.match(booking, /booker_relations/, "BookingDialog: relation options come from loc.booker_relations")
for (const rel of ["src/components/CancelPanel.tsx", "src/screens/CRS.tsx"]) {
  assert.match(read(rel), /cancellation_reasons/, `${rel}: reasons come from loc.cancellation_reasons`)
}

// the meal plan form links the Meal Plan Code master
const configs = read("src/screens/configs.ts")
const mealPlans = configs.slice(configs.indexOf("export const mealPlansConfig"),
  configs.indexOf("export const", configs.indexOf("export const mealPlansConfig") + 1))
assert.ok(!stringArrays(ts.createSourceFile("m.ts", mealPlans, ts.ScriptTarget.Latest, true))
  .some((l) => l.includes("MAP") && l.includes("AP")), "configs.ts: meal plan code is not a fixed list")
assert.match(mealPlans, /linkDoctype:\s*"Meal Plan Code"/, "configs.ts: meal plan code links Meal Plan Code")

// the setup wizard suggests no money amount in a currency it does not know
const setup = parse("src/screens/Setup.tsx")
assert.ok(!literalProps(setup, "security_deposit_amount").some((v) => Number(v) > 0),
  "Setup.tsx: no default security deposit amount (it would be in rupees)")
assert.ok(!literalProps(setup, "price_per_adult").some((v) => Number(v) > 0),
  "Setup.tsx: no default meal plan price (it would be in rupees)")

console.log("configurable-lists-check: ok")
