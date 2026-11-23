#!/usr/bin/env node
/** M09: an "Agreed rate" action on the reservation screen (OPERA rate
 * override / negotiated rate for one stay). It previews with
 * rate_override_preview, shows the old rate, the new rate and the
 * difference per night plus the total, takes a Rate Change reason code,
 * asks a supervisor (Hotel Admin / Finance) for their PIN when the cut is
 * over the property's rate_override_limit_pct, then calls set_agreed_rate.
 * Only that stay changes; the rate card is never written. */
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

assert.ok(fs.existsSync(new URL("../src/lib/agreedRate.ts", import.meta.url)),
  "src/lib/agreedRate.ts holds the agreed-rate rules the panel uses")
const { agreedRateRows, agreedRateReady } = load("lib/agreedRate.ts")

// per night: the rate the folio bills now, the agreed one and the difference
const preview = {
  current_amount: 2000, new_amount: 1500, difference: -500,
  current_nightly: 1000, agreed_rate: 750, reduction_pct: 25, limit_pct: 10,
  needs_supervisor: true, can_set_rate: false, raises: false, needs_confirmation: true,
  nights: [{ date: "2031-03-17", rate: 750 }, { date: "2031-03-18", rate: 750 }],
  supervisors: [{ user: "gm@x", full_name: "GM" }],
}
const nightly = [{ date: "2031-03-17", rate: 1000 }, { date: "2031-03-18", rate: 1000 }]
assert.deepEqual(agreedRateRows(nightly, preview), [
  { date: "2031-03-17", was: 1000, now: 750, diff: -250 },
  { date: "2031-03-18", was: 1000, now: 750, diff: -250 },
])
// a night the stay did not have before (no locked rate) compares with nothing
assert.deepEqual(agreedRateRows([], { ...preview, nights: [{ date: "2031-03-17", rate: 750 }] }),
  [{ date: "2031-03-17", was: 0, now: 750, diff: 750 }])

// what must be filled in before Save
const base = { preview, reasonCode: "RATE-PROMO", codeNeedsSupervisor: false, supervisor: "", pin: "" }
assert.equal(agreedRateReady({ ...base, preview: null }), false, "no preview yet")
assert.equal(agreedRateReady({ ...base, reasonCode: "" }), false, "a Rate Change reason code is required")
assert.equal(agreedRateReady(base), false, "over the limit: a supervisor and their PIN are required")
assert.equal(agreedRateReady({ ...base, supervisor: "gm@x" }), false, "the supervisor's PIN is required")
assert.equal(agreedRateReady({ ...base, supervisor: "gm@x", pin: "4321" }), true)
const within = { ...preview, needs_supervisor: false, reduction_pct: 5 }
assert.equal(agreedRateReady({ ...base, preview: within }), true, "within the limit: no supervisor")
assert.equal(agreedRateReady({ ...base, preview: within, codeNeedsSupervisor: true }), false,
  "a reason code that needs a supervisor asks for one too (RATE-MGR)")
assert.equal(agreedRateReady({ ...base, preview: { ...within, can_set_rate: true }, codeNeedsSupervisor: true }), true,
  "a Revenue Manager / Hotel Admin sets it on their own authority")
assert.equal(agreedRateReady({ ...base, preview: { ...within, raises: true } }), false,
  "the front desk never raises a rate")
assert.equal(agreedRateReady({ ...base, preview: { ...within, raises: true, can_set_rate: true } }), true,
  "a manager may raise it")

// the panel calls the two endpoints and sends every argument set_agreed_rate takes
const panel = read("components/AgreedRatePanel.tsx")
assert.ok(panel.includes('"kamra.api.rate_override_preview"'), "the panel previews with rate_override_preview")
assert.ok(panel.includes('"kamra.api.set_agreed_rate"'), "the panel saves with set_agreed_rate")
// one list_reason_codes request serves the discount and the agreed rate;
// the agreed rate offers the Rate Change codes among them
assert.match(panel, /\.category === "Rate Change"/, "the panel lists Rate Change reason codes")
for (const arg of ["reason_code", "confirm_price", "supervisor", "pin"])
  assert.match(panel, new RegExp(`\\b${arg}\\b`), `set_agreed_rate gets ${arg}`)
assert.ok(panel.includes("agreedRateRows(") && panel.includes("agreedRateReady("),
  "the panel uses the rules in lib/agreedRate.ts")

const detail = read("screens/ReservationDetail.tsx")
assert.match(detail, /<AgreedRatePanel\b/, "ReservationDetail offers the Agreed rate action")

for (const language of ["ar", "es", "pt"]) {
  const dictionary = JSON.parse(read(`i18n/locales/${language}.json`))
  for (const key of ["Agreed rate", "A manager signs this off with their PIN.", "Save agreed rate"])
    assert.ok(dictionary[key]?.trim(), `${language}: translation for ${key}`)
}

console.log("agreed-rate-check: ok")
