import fs from "node:fs"
import { describe, it, expect } from "vitest"
import { csvRows, scanPublishedApi } from "./brand-leak-check.mjs"

const read = (file) => fs.readFileSync(new URL(file, import.meta.url), "utf8")

describe("share validation translations", () => {
  const source = read("../../kamra/shares.py")
  const keys = [...new Set([...source.matchAll(/_\("([^"]+)"\)/g)].map((m) => m[1]))]
  for (const lang of ["fr", "es", "ar"]) {
    it(`translates all backend share messages and front desk actions in ${lang}`, () => {
      const csv = new Map(csvRows(read(`../../kamra/translations/${lang}.csv`)))
      expect(keys.length).toBeGreaterThanOrEqual(20)
      for (const key of keys) {
        expect(csv.get(key), key).toBeTruthy()
        expect(csv.get(key)).not.toEqual(key)
        for (const slot of key.match(/\{\d+\}/g) || []) expect(csv.get(key)).toContain(slot)
      }
      const ui = JSON.parse(read(`../src/i18n/locales/${lang}.json`))
      for (const key of ["Add Sharer", "Break Share", "Combine Existing Reservation", "Save Rate Split"]) {
        expect(ui[key], key).toBeTruthy()
        expect(ui[key]).not.toEqual(key)
      }
    })
  }
})

describe("published API branding", () => {
  it("checks reference prose and nested Postman display text", () => {
    const findings = scanPublishedApi("Kamra instructions", {
      info: { name: "Kamra API" },
      item: [{ name: "Kamra folder", item: [{ name: "Kamra item", request: {
        description: "Visit kamrapms.com", url: { raw: "{{base_url}}/api/method/kamra.api.check_in" },
      } }] }],
    })
    expect(findings).toHaveLength(5)
  })
  it("keeps the generated reference and collection white-labelled", () => {
    expect(scanPublishedApi(read("../../docs-site/api-reference.md"),
      JSON.parse(read("../../docs-site/public/kamra.postman_collection.json")))).toEqual([])
  })
})
