#!/usr/bin/env node
/** #57: before the night audit the manager flash shows ADR and RevPAR
 * projected from the rooms in house, labelled "projected", beside the
 * posted figures (OPERA's flash shows in-house rate projections). */
import assert from "node:assert/strict"
import fs from "node:fs"

const read = (rel) => fs.readFileSync(new URL(`../src/${rel}`, import.meta.url), "utf8")
const reports = read("screens/Reports.tsx")

for (const key of ["projected_room_revenue", "projected_adr", "projected_revpar"])
  assert.match(reports, new RegExp(`\\b${key}\\?: number`), `the Day type carries ${key}`)

// shown only while the day is on the in-house basis, and labelled projected
assert.match(reports, /todayData\?\.projected_adr\s*!=\s*null/, "the projected ADR shows when the server sends it")
assert.ok(reports.includes('"projected {v} (in house)"'), "the projection is labelled projected")
// the posted ADR / RevPAR tiles stay
assert.ok(reports.includes('label={t("ADR")}') && reports.includes('label={t("RevPAR")}'),
  "the posted ADR and RevPAR tiles stay")

for (const language of ["ar", "es", "pt"]) {
  const dictionary = JSON.parse(read(`i18n/locales/${language}.json`))
  assert.ok(dictionary["projected {v} (in house)"]?.trim(), `${language}: translation for the projected label`)
}

console.log("flash-projected-check: ok")
