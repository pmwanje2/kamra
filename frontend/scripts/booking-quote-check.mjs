#!/usr/bin/env node
/** M17: a quotation for one booking, laid out like the group quotation,
 * that the browser can print or save as PDF, and an "Email to guest"
 * button that sends it through Frappe email to the booker's address only.
 * Front Desk and above. */
import assert from "node:assert/strict"
import fs from "node:fs"
import ts from "typescript"

const read = (rel) => fs.readFileSync(new URL(`../src/${rel}`, import.meta.url), "utf8")
const exists = (rel) => fs.existsSync(new URL(`../src/${rel}`, import.meta.url))

assert.ok(exists("screens/BookingQuotation.tsx"), "screens/BookingQuotation.tsx renders one booking's quotation")
const screen = read("screens/BookingQuotation.tsx")
assert.ok(screen.includes('"kamra.api.booking_quotation"'), "the quotation reads kamra.api.booking_quotation")
assert.ok(screen.includes("window.print()"), "Print / Save as PDF uses the browser's print, like GroupQuotation")
assert.ok(screen.includes('"kamra.api.email_booking_quotation"'), "Email to guest calls kamra.api.email_booking_quotation")

// the email goes to the address on record only: the screen sends the booking, never an address
const source = ts.createSourceFile("BookingQuotation.tsx", screen, ts.ScriptTarget.Latest, true)
const emailCalls = []
const visit = (node) => {
  if (ts.isCallExpression(node) && node.arguments.some((a) => ts.isStringLiteral(a) && a.text === "kamra.api.email_booking_quotation"))
    emailCalls.push(node)
  ts.forEachChild(node, visit)
}
visit(source)
assert.equal(emailCalls.length, 1, "one email call")
const args = emailCalls[0].arguments[1]
assert.ok(args && ts.isObjectLiteralExpression(args), "the email call passes an object")
assert.deepEqual(args.properties.map((p) => p.name?.getText(source)), ["reservation"],
  "the screen sends only the reservation; the server picks the booker's address")

// the same layout as the group quotation, reached from the booking
const app = read("App.tsx")
assert.match(app, /path="booking-quote\/:name"\s+element=\{<BookingQuotation \/>\}/, "route /booking-quote/:name")
assert.match(read("screens/ReservationDetail.tsx"), /to=\{`\/booking-quote\/\$\{encodeURIComponent\(/,
  "ReservationDetail links to the booking's quotation")
for (const shared of ["Print / Save as PDF", "Rate / night", "Total", "Terms"])
  assert.ok(screen.includes(`"${shared}"`), `same wording as GroupQuotation: ${shared}`)

// review on #16: the price line by line (room nights, meal plan, discount,
// fees) adding up to the total, and the terms the server wrote for this booking
assert.match(screen, /\blines:\s*\{/, "the Quote type carries the priced lines")
assert.match(screen, /d\.lines\.map\(/, "each priced line is shown")
assert.match(screen, /d\.terms\.map\(/, "the terms come from the server, matching the lines")
assert.ok(!screen.includes("Room prices exclude meals"), "no fixed 'prices exclude meals' term")
// the mail is queued by Frappe, not delivered when the call returns
assert.ok(!screen.includes('"Quotation sent to {email}."'), "the screen does not claim the mail was sent")
assert.ok(screen.includes('"Quotation queued for sending to {email}."'), "the screen says the mail is queued")

for (const language of ["ar", "es", "pt"]) {
  const dictionary = JSON.parse(read(`i18n/locales/${language}.json`))
  for (const key of ["Quotation", "Email to guest", "Quotation queued for sending to {email}."])
    assert.ok(dictionary[key]?.trim(), `${language}: translation for ${key}`)
}

console.log("booking-quote-check: ok")
