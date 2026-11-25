#!/usr/bin/env node
/** fdchat: a guest who pays at check-in wants the discount now, before the
 * night audit has posted any room night. The refusal names the way forward
 * (OPERA advance billing): post the stay with Advance bill, then discount, or
 * price it at booking with a rate plan or promo code. The discount panel
 * offers the Advance bill action right there, and a folio link with
 * ?action=advance-bill brings the desk straight to that button. The screen
 * keys on the server's exception class, never on the message wording. */
import assert from "node:assert/strict"
import fs from "node:fs"
import ts from "typescript"

const read = (rel) => fs.readFileSync(new URL(`../src/${rel}`, import.meta.url), "utf8")
const load = (rel) => {
  const code = ts.transpileModule(read(rel), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  }).outputText
  const exports = {}
  new Function("exports", code)(exports)
  return exports
}

assert.ok(fs.existsSync(new URL("../src/lib/discountHint.ts", import.meta.url)),
  "src/lib/discountHint.ts holds the no-nights rule the discount panel uses")
const { NO_ROOM_NIGHTS, isNoRoomNights, advanceBillHref, ADVANCE_BILL_ACTION } = load("lib/discountHint.ts")

assert.equal(NO_ROOM_NIGHTS, "NoRoomNightsToDiscount", "the server's exception class")
const body = (exc_type) => JSON.stringify({ exc_type, _server_messages: "[]" })
assert.equal(isNoRoomNights({ status: 417, body: body("NoRoomNightsToDiscount") }), true)
assert.equal(isNoRoomNights({ status: 417, body: body("ValidationError") }), false,
  "any other refusal shows as it is")
assert.equal(isNoRoomNights({ status: 417, body: "<html>proxy</html>" }), false, "a non-JSON body is not the hint")
assert.equal(isNoRoomNights(new Error("network")), false)
assert.equal(isNoRoomNights(null), false)

assert.equal(ADVANCE_BILL_ACTION, "advance-bill")
assert.equal(advanceBillHref("F-0001"), "/billing/F-0001?action=advance-bill")
assert.equal(advanceBillHref("F 1/2"), "/billing/F%201%2F2?action=advance-bill", "the folio name is encoded")

const panel = read("components/FolioDiscount.tsx")
assert.match(panel, /onAdvanceBill\?: \(\) => void/, "the panel takes the folio's Advance bill action")
assert.match(panel, /isNoRoomNights\(e\)/, "the preview keys on the exception class")
assert.match(panel, /onClick=\{onAdvanceBill\}[\s\S]{0,200}\{t\("Advance bill"\)\}/,
  "the hint offers the Advance bill action")

const view = read("screens/FolioView.tsx")
assert.match(view, /id=\{ADVANCE_BILL_ACTION\}/, "the Advance bill button is addressable")
assert.match(view, /searchParams\.get\("action"\) === ADVANCE_BILL_ACTION/, "a folio link can open the Advance bill action")
assert.match(view, /<FolioDiscount[\s\S]{0,200}onAdvanceBill=/, "the folio hands its Advance bill action to the panel")

// The server message is translated where Frappe translates it (kamra/translations).
const message = "No nights are posted yet. Use Advance bill to post the stay now, then give the discount, " +
  "or use a rate plan or promo code at booking."
for (const language of ["ar", "es", "pt"]) {
  const csv = fs.readFileSync(new URL(`../../kamra/translations/${language}.csv`, import.meta.url), "utf8")
  // Frappe reads these with Python's csv module: a key with commas is quoted
  const key = `"${message}",`
  assert.ok(csv.split("\n").some((line) => line.startsWith(key) && line.replace(key, "").replace(/^"|"\r?$/g, "").trim()),
    `${language}: translation for the no-nights message`)
  assert.ok(!csv.includes("give it after night audit"), `${language}: the old message is gone`)
}

// Review of c6a48bfc6: after Advance bill posts the nights the hint must go (the preview re-runs when the folio
// reloads), and the deep link focuses the button once per visit, not on every reload.
assert.match(panel, /refreshKey\?: unknown/, "the panel takes a value that changes when the folio reloads")
assert.match(panel, /\[folio,\s*promo,[^\]\n]*\brefreshKey\b[^\]\n]*\]/, "the preview re-runs on reload")
assert.match(view, /refreshKey=\{/, "FolioView passes its reload marker")
assert.match(view, /focusedAdvanceBill\.current/, "the deep link focuses once")

console.log("discount-advance-bill-check: ok")
