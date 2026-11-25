import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"
import { describe, expect, it, vi } from "vitest"
import { momentParts } from "../src/lib/localDates"
import { scanTree } from "./brand-leak-check.mjs"

vi.hoisted(() => {
  const store = new Map<string, string>()
  const storage = {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => {
      store.set(k, String(v))
    },
    removeItem: (k: string) => {
      store.delete(k)
    },
    clear: () => {
      store.clear()
    },
  }
  if (typeof globalThis.localStorage === "undefined") {
    Object.defineProperty(globalThis, "localStorage", { value: storage, configurable: true })
  }
  if (typeof globalThis.window === "undefined") {
    Object.defineProperty(globalThis, "window", {
      value: {
        location: { pathname: "/" },
        addEventListener: () => {},
        removeEventListener: () => {},
        dispatchEvent: () => true,
      },
      configurable: true,
    })
  }
  if (typeof globalThis.document === "undefined") {
    Object.defineProperty(globalThis, "document", {
      value: {
        documentElement: {
          setAttribute: () => {},
          getAttribute: () => null,
        },
      },
      configurable: true,
    })
  }
})

import { adoptUiLocale, fmtAmount } from "../src/lib/money"
import { VOICE_LANGS } from "../src/components/VoiceButton"
import { LOCALES } from "../src/screens/settings/cards"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const languagesPath = path.join(root, "src/i18n/languages.json")
const catalogPath = path.join(root, "src/i18n/catalog.csv")
const frPath = path.join(root, "src/i18n/locales/fr.json")
const extractPath = path.join(root, "scripts/i18n-extract.mjs")

const FORBIDDEN_BRAND =
  /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|HUF|Tridz|BuildSuite)(?![a-z0-9])|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala/i

const FORBIDDEN_LITERAL_FR = [
  /\bVérifier dans\b/i,
  /\bSortir de caisse\b/i,
  /\bGraphique à bande\b/i,
  /\bGrand livre de ville\b/i,
  /\bPas de spectacle\b/i,
  /\bSurbooking\b/i,
  /\bEntrée à pied\b/i,
]

const ENGLISH_PROSE_WORD =
  /\b(?:the|and|with|from|this|that|your|have|will|when|after|before|only|must|been|into|here|cannot|can't|doesn't|isn't|aren't|would|should|could|check-in|check-out|booking|bookings|reservation|guest|guests|room|rooms|night|nights|cashier|till|invoice|payment|payments|property|properties|housekeeping)\b/i

function parseCsv(text: string): string[][] {
  if (text.charCodeAt(0) === 0xfeff) text = text.slice(1)
  const rows: string[][] = []
  let row: string[] = []
  let field = ""
  let quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"'
        i++
      } else if (c === '"') {
        quoted = false
      } else {
        field += c
      }
    } else if (c === '"') {
      quoted = true
    } else if (c === ",") {
      row.push(field)
      field = ""
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++
      row.push(field)
      if (row.some((cell) => cell.length)) rows.push(row)
      row = []
      field = ""
    } else {
      field += c
    }
  }
  if (field.length || row.length) {
    row.push(field)
    if (row.some((cell) => cell.length)) rows.push(row)
  }
  return rows
}

const placeholders = (value: string) =>
  [...new Set([...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort()

describe("P58 French (fr) locale completeness & hospitality quality", () => {
  it("registers French (fr) in languages.json and i18n-extract.mjs", () => {
    const languages = JSON.parse(fs.readFileSync(languagesPath, "utf8")) as Array<{
      code: string
      nativeLabel: string
      englishLabel: string
      dir: string
    }>
    const fr = languages.find((row) => row.code === "fr")
    expect(fr).toEqual({
      code: "fr",
      nativeLabel: "Français",
      englishLabel: "French",
      dir: "ltr",
    })

    const extractSrc = fs.readFileSync(extractPath, "utf8")
    expect(extractSrc).toMatch(/\[\s*["']fr["']\s*,\s*["']french["']\s*\]/)
  })

  it("populates catalog.csv french column and fr.json with 100% coverage, exact placeholders, and zero vendor/English leaks", () => {
    expect(fs.existsSync(frPath), "frontend/src/i18n/locales/fr.json must exist").toBe(true)

    const [header, ...records] = parseCsv(fs.readFileSync(catalogPath, "utf8"))
    const keyIdx = header.indexOf("key")
    const frColIdx = header.indexOf("french")
    const sourcesIdx = header.indexOf("sources")
    expect(keyIdx).toBe(0)
    expect(frColIdx).toBeGreaterThan(0)
    expect(frColIdx).toBeLessThan(sourcesIdx)

    const frDict = JSON.parse(fs.readFileSync(frPath, "utf8")) as Record<string, string>
    const missing: string[] = []
    const badPlaceholders: string[] = []
    const badLines: string[] = []
    const brandLeaks: string[] = []
    const badLiteralFr: string[] = []
    const englishFallbacks: string[] = []

    for (const row of records) {
      const key = row[keyIdx]
      const csvVal = row[frColIdx] ?? ""
      const jsonVal = frDict[key]
      if (!csvVal.trim() || typeof jsonVal !== "string" || !jsonVal.trim()) {
        missing.push(key)
        continue
      }
      if (JSON.stringify(placeholders(jsonVal)) !== JSON.stringify(placeholders(key))) {
        badPlaceholders.push(`${key} => ${jsonVal}`)
      }
      if (jsonVal.split("\n").length !== key.split("\n").length) {
        badLines.push(`${key} => ${jsonVal}`)
      }
      if (FORBIDDEN_BRAND.test(jsonVal) || FORBIDDEN_BRAND.test(csvVal)) {
        brandLeaks.push(`${key} => ${jsonVal}`)
      }
      for (const bad of FORBIDDEN_LITERAL_FR) {
        if (bad.test(jsonVal)) badLiteralFr.push(`${key} => ${jsonVal}`)
      }
      const cleanWords = key
        .replace(/\{[^}]+\}/g, " ")
        .match(/[A-Za-z][A-Za-z'-]*/g)
      if (
        jsonVal === key &&
        !key.startsWith("http") &&
        !key.includes("://") &&
        !key.startsWith("claude ") &&
        cleanWords &&
        cleanWords.length >= 3 &&
        ENGLISH_PROSE_WORD.test(key)
      ) {
        englishFallbacks.push(key)
      }
    }

    expect(missing.slice(0, 20), `${missing.length} missing fr keys`).toEqual([])
    expect(badPlaceholders.slice(0, 20)).toEqual([])
    expect(badLines.slice(0, 20)).toEqual([])
    expect(brandLeaks.slice(0, 20)).toEqual([])
    expect(badLiteralFr.slice(0, 20)).toEqual([])
    expect(englishFallbacks.slice(0, 20), `${englishFallbacks.length} English fallback leaks`).toEqual([])

    // Core hospitality French spot-checks
    expect(frDict["Phone"]).toBe("Téléphone")
    expect(frDict["Today"]).toBe("Aujourd'hui")
    expect(frDict["{n} night"]).toBe("{n} nuit")
    expect(frDict["{n} nights"]).toBe("{n} nuits")
    expect(frDict["Foreign currency drawers"]).toBeTruthy()
    expect(frDict["Foreign currency drawers"]).not.toBe("Foreign currency drawers")
  })

  it("supports French day-first dates, fr-FR voice recognition, and French/Arabic locale options", () => {
    const now = new Date(2026, 10, 17, 12, 0)
    expect(momentParts("2026-11-17 09:15:00", "fr", { now })).toEqual({
      date: "17-11",
      time: "09:15",
    })
    expect(momentParts("2025-12-30 23:05:59", "fr", { now })).toEqual({
      date: "30-12-2025",
      time: "23:05",
    })

    expect(VOICE_LANGS).toContainEqual(["fr-FR", "Français"])
    for (const loc of ["fr-FR", "fr-CA", "fr-CH", "fr-BE", "ar-AE", "ar-EG"]) {
      expect(LOCALES, `LOCALES must include ${loc}`).toContain(loc)
    }
  })

  it("passes brand-leak-check.mjs with the french catalog.csv column and fr.json", () => {
    const { findings } = scanTree(root)
    expect(findings).toEqual([])
  }, 30000)
})

describe("P58 en-US hardcoded locale-lock removal & Cashier/Banquet dynamic precision", () => {
  const OWNED_SCREENS = [
    "src/screens/CashierBoard.tsx",
    "src/screens/CashierSessions.tsx",
    "src/screens/CashierShiftReport.tsx",
    "src/screens/CashierTill.tsx",
    "src/screens/BanquetMonth.tsx",
    "src/screens/banquet/shared.tsx",
  ]

  it("removes all hardcoded en-US calls and hardcoded 0dp/2dp Intl options from owned Cashier and Banquet screens", () => {
    const violations: string[] = []
    for (const rel of OWNED_SCREENS) {
      const full = path.join(root, rel)
      const sf = ts.createSourceFile(full, fs.readFileSync(full, "utf8"), ts.ScriptTarget.Latest, true)
      function visit(node: ts.Node) {
        if (ts.isStringLiteralLike(node) && node.text === "en-US") {
          const line = sf.getLineAndCharacterOfPosition(node.getStart()).line + 1
          violations.push(`${rel}:${line}: hardcoded "en-US"`)
        }
        if (
          ts.isPropertyAssignment(node) &&
          ts.isIdentifier(node.name) &&
          (node.name.text === "minimumFractionDigits" || node.name.text === "maximumFractionDigits") &&
          ts.isNumericLiteral(node.initializer) &&
          (node.initializer.text === "0" || node.initializer.text === "2")
        ) {
          const line = sf.getLineAndCharacterOfPosition(node.getStart()).line + 1
          violations.push(`${rel}:${line}: hardcoded ${node.name.text}: ${node.initializer.text}`)
        }
        ts.forEachChild(node, visit)
      }
      visit(sf)
    }
    expect(violations).toEqual([])
  })

  it("wires Cashier screens to fmtAmount(value, currency) and Banquet screens to fmtMoney & moneyLocale()", () => {
    for (const rel of [
      "src/screens/CashierBoard.tsx",
      "src/screens/CashierSessions.tsx",
      "src/screens/CashierShiftReport.tsx",
      "src/screens/CashierTill.tsx",
    ]) {
      const text = fs.readFileSync(path.join(root, rel), "utf8")
      expect(text, `${rel} must import fmtAmount from lib/money`).toMatch(/\bfmtAmount\b/)
      expect(text, `${rel} must pass currency to foreign drawer formatter`).toMatch(
        /fmtAmount\([^)]*,\s*(?:fd|tx)\.currency\)/,
      )
    }

    const banquetShared = fs.readFileSync(path.join(root, "src/screens/banquet/shared.tsx"), "utf8")
    expect(banquetShared).toMatch(/\bfmtMoney\b/)
    expect(banquetShared).toMatch(/uiDateLocale\(moneyLocale\(\)\)/)

    const banquetMonth = fs.readFileSync(path.join(root, "src/screens/BanquetMonth.tsx"), "utf8")
    expect(banquetMonth).toMatch(/uiDateLocale\(moneyLocale\(\)\)/)
  })

  it("formats UGX (0dp), USD (2dp), and KWD (3dp) dynamically in French locale via fmtAmount", () => {
    adoptUiLocale({ locale: "fr-FR", currency: "USD", currency_symbol: "$" })
    expect(fmtAmount(50000, "UGX")).toBe(
      new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 0, maximumFractionDigits: 0 }).format(50000),
    )
    expect(fmtAmount(12.5, "USD")).toBe(
      new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(12.5),
    )
    expect(fmtAmount(12.345, "KWD")).toBe(
      new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 3, maximumFractionDigits: 3 }).format(12.345),
    )
  })
})
