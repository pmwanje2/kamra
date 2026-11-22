import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"

export const forbidden = /\b(?:Kamra|Frappe|ERPNext|URY)\b|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala/i

const root = fileURLToPath(new URL("../", import.meta.url))

// Machine identifiers only. Whole-value matches keep prose containing an
// identifier (an error, link caption, etc.) in scope.
function identifier(value, node) {
  if (/^(?:kamra|frappe|erpnext)(?:[.:_][\w:-]+)+$/i.test(value)) return true
  if (/^\/(?:assets\/kamra|api\/method\/(?:kamra|frappe)\.|kamra(?:\/|$))/i.test(value)) return true
  if (/^(?:X-Frappe-CSRF-Token|x-frappe-csrf-token|x-kamra-[\w-]+|kamra|frappe)$/i.test(value) &&
      value !== "Kamra" && value !== "Frappe") return true
  if (node && ts.isExternalModuleReference(node.parent)) return true
  return false
}

export function csvRows(text) {
  const rows = []
  let row = [], field = "", quoted = false
  for (let i = 0; i < text.length; i++) {
    const c = text[i]
    if (c === '"') {
      if (quoted && text[i + 1] === '"') { field += '"'; i++ }
      else quoted = !quoted
    } else if (!quoted && (c === "," || c === "\n")) {
      row.push(field); field = ""
      if (c === "\n") { rows.push(row); row = [] }
    } else if (quoted || c !== "\r") field += c
  }
  if (row.length || field) rows.push([...row, field])
  if (quoted) throw new Error("Unterminated quoted CSV value")
  return rows
}

export function checkSource() {
  let sourceCount = 0
  const findings = []
  const scan = (value, location, node) => {
    if (forbidden.test(value.trim()) && !identifier(value.trim(), node)) findings.push(`${location}: ${value.trim()}`)
  }
  function walk(dir) {
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const name = path.join(dir, item.name)
      if (item.isDirectory()) walk(name)
      else if (/\.tsx?$/.test(name)) {
        const source = ts.createSourceFile(name, fs.readFileSync(name, "utf8"), ts.ScriptTarget.Latest, true)
        function visit(node) {
          if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node) || ts.isJsxText(node)) {
            const before = findings.length
            scan(node.text, `${path.relative(root, name)}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`, node)
            sourceCount += findings.length - before
          }
          ts.forEachChild(node, visit)
        }
        visit(source)
      }
    }
  }
  walk(path.join(root, "src"))
  const locales = path.join(root, "src/i18n/locales")
  for (const file of fs.readdirSync(locales).filter((f) => f.endsWith(".json"))) {
    for (const [key, value] of Object.entries(JSON.parse(fs.readFileSync(path.join(locales, file), "utf8")))) {
      scan(key, file); scan(value, `${file} (${key})`)
    }
  }
  const [header, ...rows] = csvRows(fs.readFileSync(path.join(root, "src/i18n/catalog.csv"), "utf8"))
  for (const [i, row] of rows.entries()) {
    if (row.length !== header.length) throw new Error(`catalog.csv:${i + 2}: unexpected column count`)
    for (let col = 0; col < header.indexOf("sources"); col++) scan(row[col], `catalog.csv:${i + 2}`)
  }
  const html = fs.readFileSync(path.join(root, "index.html"), "utf8")
  for (const match of html.matchAll(/<title>([^<]*)<\/title>|\bcontent="([^"]*)"/g)) {
    scan(match[1] ?? match[2], "index.html")
  }
  if (findings.length) {
    console.error(findings.join("\n"))
    throw new Error(`${sourceCount} UI source findings; ${findings.length} total branding findings`)
  }
  console.log("Brand leak check passed: source, locales, catalog and shell")
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) checkSource()
