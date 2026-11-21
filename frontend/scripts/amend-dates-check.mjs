#!/usr/bin/env node
/** M14: changing a booking's dates on the reservation screen must save once
 * the desk confirms the new price. OPERA re-prices, shows the new total and
 * saves on confirm.
 *
 * The screen used to gate amend_stay on window.confirm(). A dialog the
 * browser suppresses (automation, an embedded web view, "prevent this page
 * from creating additional dialogs") returns false at once: the preview ran,
 * amend_stay was never called, no error showed and the typed dates stayed on
 * screen as if saved (Tank Hill round 2, RES-2026-00163). The price now shows
 * in the page, as on the Tape Chart, and a button sends amend_stay with
 * confirm_price taken from that preview. */
import assert from "node:assert/strict"
import fs from "node:fs"
import ts from "typescript"

const read = (rel) => fs.readFileSync(new URL(`../src/${rel}`, import.meta.url), "utf8")

function calls(rel, match) {
  const source = ts.createSourceFile(rel, read(rel), ts.ScriptTarget.Latest, true)
  const found = []
  const visit = (node) => {
    if (ts.isCallExpression(node) && match(node.expression.getText(source))) found.push([node, source])
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

const detail = "screens/ReservationDetail.tsx"

// no blocking browser dialog decides whether the new dates are saved
const dialogs = calls(detail, (name) => name === "window.confirm" || name === "confirm")
  .filter(([node, source]) => /re-price|amend/i.test(node.getText(source)))
assert.equal(dialogs.length, 0,
  "ReservationDetail gates the date change on window.confirm; a suppressed or dismissed dialog drops the save with no request and no error")
assert.ok(!read("lib/api.ts").includes("amendStayConfirmed"),
  "api.ts keeps amendStayConfirmed, which resolves null (nothing saved, nothing shown) when the confirm dialog returns false")

// the screen previews the new price in the page and saves with that preview's flag
assert.equal(calls(detail, (name) => name === "amendStayPreview").length, 1,
  "ReservationDetail asks amend_stay_preview for the new price")
const saves = calls(detail, (name) => name === "amendStay")
assert.equal(saves.length, 1, "ReservationDetail saves the dates with one amendStay call")
const [save, source] = saves[0]
assert.equal(save.arguments.length, 4, "amendStay gets reservation, check-in, check-out and confirm_price")
assert.match(save.arguments[3].getText(source), /needs_confirmation/,
  "confirm_price comes from the preview the desk was shown")

const screen = read(detail)
for (const key of [
  "{n} nights. Stay total {from} to {to} (+{amt}).",
  "{n} nights. Stay total {from} to {to} (-{amt}).",
  "{n} nights. No change to the price.",
  "Confirm new dates and price",
]) {
  assert.ok(screen.includes(`"${key}"`), `ReservationDetail shows "${key}" in the page`)
}

// the strings are the Tape Chart's, already translated
for (const language of ["ar", "es", "pt"]) {
  const dictionary = JSON.parse(read(`i18n/locales/${language}.json`))
  for (const key of ["Confirm new dates and price", "{n} nights. Stay total {from} to {to} (+{amt})."]) {
    assert.ok(dictionary[key]?.trim(), `${language}: translation for ${key}`)
  }
}

console.log("amend-dates-check: ok")
