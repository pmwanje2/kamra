#!/usr/bin/env node
/** Check locale parameters and guard against translating text twice. */
import assert from "node:assert/strict"
import fs from "node:fs"
import path from "node:path"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"
import ts from "typescript"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")
const localeDirectory = path.join(root, "src/i18n/locales")
const dictionaries = { en: {} }
const parameters = (value) => [...new Set([...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]))].sort()

for (const filename of fs.readdirSync(localeDirectory).filter((name) => name.endsWith(".json"))) {
  const language = filename.slice(0, -5)
  const source = fs.readFileSync(path.join(localeDirectory, filename), "utf8")
  const document = ts.parseJsonText(filename, source)
  const names = document.statements[0].expression.properties.map((property) => property.name.text)
  assert.equal(new Set(names).size, names.length, `${language}: duplicate dictionary keys`)
  const dictionary = JSON.parse(source)
  dictionaries[language] = dictionary
  for (const [key, value] of Object.entries(dictionary)) {
    assert.equal(typeof value, "string", `${language}: non-string translation for ${key}`)
    assert.ok(value.trim(), `${language}: empty translation for ${key}`)
    assert.deepEqual(parameters(value), parameters(key), `${language}: placeholder mismatch for ${key}`)
  }
}

// catalog.csv is the source `i18n:import` writes locale files from, so each of
// its cells must already satisfy the rules above. A row pasted inside another
// row's multi-line quoted cell parses as part of that cell: the cell then gains
// a line, and usually the pasted row's placeholders, that its key does not have.
function parseCsv(text) {
  const rows = []
  let row = []
  let field = ""
  let quoted = false
  for (let i = text.charCodeAt(0) === 0xfeff ? 1 : 0; i < text.length; i++) {
    const c = text[i]
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') {
        field += '"'
        i++
      } else if (c === '"') quoted = false
      else field += c
    } else if (c === '"') quoted = true
    else if (c === ",") {
      row.push(field)
      field = ""
    } else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++
      row.push(field)
      if (row.some((cell) => cell.length)) rows.push(row)
      row = []
      field = ""
    } else field += c
  }
  assert.ok(!quoted, "catalog.csv: unterminated quoted cell")
  row.push(field)
  if (row.some((cell) => cell.length)) rows.push(row)
  return rows
}
const [header, ...records] = parseCsv(fs.readFileSync(path.join(root, "src/i18n/catalog.csv"), "utf8"))
const keyColumn = header.indexOf("key")
const textColumns = header.filter((name) => !["key", "sources", "placeholders", "status"].includes(name))
assert.ok(keyColumn >= 0, "catalog.csv: missing key column")
for (const record of records) {
  const key = record[keyColumn]
  assert.equal(record.length, header.length, `catalog.csv: ${JSON.stringify(key)} has ${record.length} cells, not ${header.length}`)
  for (const name of textColumns) {
    const value = record[header.indexOf(name)]
    if (!value.trim()) continue
    assert.deepEqual(parameters(value), parameters(key), `catalog.csv: ${name} placeholder mismatch for ${JSON.stringify(key)}`)
    assert.equal(
      value.split("\n").length,
      key.split("\n").length,
      `catalog.csv: ${name} for ${JSON.stringify(key)} has a different number of lines than its key (a row pasted inside a multi-line cell?)`,
    )
  }
}

const translatorNames = new Set(["t", "tn", "translate", "translateUI"])
function isTranslation(node) {
  return ts.isCallExpression(node) && ts.isIdentifier(node.expression) && translatorNames.has(node.expression.text)
}
function containsTranslation(node) {
  return isTranslation(node) || !!ts.forEachChild(node, containsTranslation)
}
function checkDirectory(directory) {
  for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
    const filename = path.join(directory, entry.name)
    if (entry.isDirectory()) checkDirectory(filename)
    else if (/\.tsx?$/.test(filename)) {
      const source = ts.createSourceFile(filename, fs.readFileSync(filename, "utf8"), ts.ScriptTarget.Latest, true)
      function visit(node) {
        if (isTranslation(node) && node.arguments[0] && containsTranslation(node.arguments[0])) {
          const line = source.getLineAndCharacterOfPosition(node.getStart()).line + 1
          assert.fail(`${path.relative(root, filename)}:${line}: nested translation`)
        }
        ts.forEachChild(node, visit)
      }
      visit(source)
    }
  }
}
checkDirectory(path.join(root, "src"))

// Exercise the real translator with a selected language and real dictionaries.
// React hooks are imported normally; these checks call only the pure t()/tn().
const require = createRequire(import.meta.url)
let language = "en"
const output = ts.transpileModule(fs.readFileSync(path.join(root, "src/lib/i18n.ts"), "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
const api = {}
new Function("require", "exports", output)((name) => {
  if (name === "react") return require("react")
  if (name === "./dir") return { getLang: () => language }
  if (name === "./locales") return { dictionaryFor: (key) => dictionaries[key] ?? {} }
  if (name === "./brand") return { product: () => "Maximus Hotel", assistant: () => "Eve" }
  throw new Error(`Unexpected translator import: ${name}`)
}, api)
assert.equal(api.t("Phone"), "Phone")
assert.equal(api.t("Room {n}", { n: 12 }), "Room 12")
assert.equal(api.t("{product} · {assistant}"), "Maximus Hotel · Eve")
assert.equal(api.tn(1, "{n} night", "{n} nights"), "1 night")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 nights")
language = "es"
assert.equal(api.t("Phone"), "Teléfono")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 noches")
for (const raw of ["Room types not configured", "Room 17: unexpected error", "constructor", "__proto__"]) {
  assert.equal(api.t(raw), raw, "Unknown runtime text must remain unchanged")
}
assert.equal(api.t(undefined), undefined)
const content = { message: "React content" }
assert.equal(api.t(content), content)
language = "ar"
assert.equal(api.t("Phone"), "الهاتف")
language = "pt"
assert.equal(api.t("Phone"), "Telefone")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 noites")
language = "id"
assert.equal(api.t("Phone"), "Telepon")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 malam")
language = "de"
assert.equal(api.t("Phone"), "Telefon")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 Nächte")
language = "fil"
assert.equal(api.t("Phone"), "Telepono")
assert.equal(api.t("Today"), "Ngayon")
assert.equal(api.tn(1, "{n} child", "{n} children"), "1 bata")
assert.equal(api.tn(2, "{n} child", "{n} children"), "2 bata")
language = "fr"
assert.equal(api.t("Phone"), "Téléphone")
assert.equal(api.t("Today"), "Aujourd'hui")
assert.equal(api.tn(1, "{n} night", "{n} nights"), "1 nuit")
assert.equal(api.tn(2, "{n} night", "{n} nights"), "2 nuits")
language = "unknown"
assert.equal(api.t("Phone"), "Phone")
const app = fs.readFileSync(path.join(root, "src/App.tsx"), "utf8")
assert.ok(!/(?:translate|translateUI|t)\(this\.state\.error\.message\)/.test(app), "Keep raw ErrorBoundary diagnostics intact")
console.log("i18n checks passed: dictionaries, placeholders, duplicate calls, English/Spanish/Arabic/Portuguese/Indonesian/German/Filipino/French and fallback behavior")
