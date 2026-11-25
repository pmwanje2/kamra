import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"

// A letter or digit, not \b, bounds a name: "_" and "-" join words in
// identifiers, so kamra_mcp_disabled or kamra-theme in prose still leaks.
export const forbidden = /(?<![a-z0-9])(?:Kamra|Frappe|ERPNext|URY|Tridz|BuildSuite)(?![a-z0-9])|kamra-mark|kamrapms\.com|github\.com\/Kamra|HeyKoala|IndianRupee|BadgeIndianRupee|CurrencyRupee|\u20b9/i

const forbiddenIcons = new Set(["IndianRupee", "BadgeIndianRupee", "CurrencyRupee"])

const root = fileURLToPath(new URL("../", import.meta.url))

// Machine identifiers only. Whole-value matches keep prose containing an
// identifier (an error, link caption, etc.) in scope.
const exceptions = new Map(Object.entries({
  "src/lib/brand.ts": ["Kamra", "Kamra Agent", "kamra-mark.svg", "https://kamrapms.com",
    "https://github.com/Kamra-PMS/kamra-pms", "Frappe Desk", "ERPNext", "HeyKoala"],
  "src/lib/api.ts": ["kamra-session", "Kamra Demo Palace", "kamra.banquet."],
  "src/lib/dir.ts": ["kamra-guest-lang", "kamra-lang"],
  // The one place the SPA's mount is named; links go through toFullPath().
  "src/lib/routing.ts": ["/kamra"],
  "src/lib/theme.ts": ["kamra-theme"],
  "src/lib/thermal.ts": ["iframe[data-kamra-print]", "data-kamra-print"],
  "src/components/InfoTip.tsx": ["why-kamra"],
  "src/components/NotificationBell.tsx": ["Kamra Notification"],
  "src/components/SetupNag.tsx": ["kamra.setupnag."],
  "src/components/ui/data-table.tsx": ["kamra.table."],
  "src/screens/AccountingExport.tsx": ["erpnext"],
  "src/screens/Developers.tsx": ["claude mcp add --transport http kamra"],
  "src/screens/settings/cards.tsx": ["claude mcp add --transport http kamra", "kamra.connect.client."],
  "src/screens/settings/WebhooksPage.tsx": ["kamra.webhooks."],
  "src/screens/settings/AccessPages.tsx": ["kamra-job-titles"],
  "src/screens/Inventory.tsx": ["kamra.inventory."],
  "src/screens/Kitchen.tsx": ["kamra.pos."],
  "src/screens/PublicBooking.tsx": ["kamra-jsonld"],
  "src/screens/Tickets.tsx": ["kamra.tickets."],
}))

function identifier(value, node, filename) {
  if (!node || ts.isJsxText(node)) return false
  if (exceptions.get(filename)?.includes(value)) return true
  if (/^(?:kamra|frappe|erpnext)(?:[.:_]\w[\w:-]*)+$/.test(value)) return true
  // Asset and API paths only. A screen path such as "/kamra/hk" is a link a
  // person follows and sees, so it is built with toFullPath() instead.
  if (/^\/(?:assets\/kamra\/|api\/method\/(?:kamra|frappe)\.)[\w./?=%&#:-]*$/.test(value)) return true
  if (/^x-(?:frappe-csrf-token|kamra-(?:event(?:-id)?|timestamp|signature|property))$/i.test(value)) return true
  if (filename === "src/screens/Login.tsx" && /^(?:admin|gm|frontdesk|revenue|finance|hk)@kamra\.local$/.test(value)) return true
  if (ts.isPropertyAssignment(node.parent) && node.parent.name.getText() === "doctype" &&
      value === "Kamra Notification") return true
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

// Sound prefilter (no lookbehind) before expensive TypeScript AST parsing:
// skips files that cannot contain any forbidden token or hex/unicode escape.
const rawCandidate = /kamra|frappe|erpnext|ury|tridz|buildsuite|heykoala|indianrupee|badgeindianrupee|currencyrupee|\u20b9|\\[ux]/i

export function scanTree(base = root) {
  let sourceCount = 0
  const findings = []
  const scan = (value, location, node, filename) => {
    if (forbidden.test(value.trim()) && !identifier(value.trim(), node, filename)) findings.push(`${location}: ${value.trim()}`)
  }
  function walk(dir) {
    for (const item of fs.readdirSync(dir, { withFileTypes: true })) {
      const name = path.join(dir, item.name)
      if (item.isDirectory()) walk(name)
      else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) {
        const text = fs.readFileSync(name, "utf8")
        if (!rawCandidate.test(text)) continue
        const source = ts.createSourceFile(name, text, ts.ScriptTarget.Latest, true)
        const filename = path.relative(base, name)
        function visit(node) {
          if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node) || ts.isJsxText(node)) {
            const before = findings.length
            scan(node.text, `${filename}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}`, node, filename)
            sourceCount += findings.length - before
          } else if (ts.isImportSpecifier(node)) {
            const imported = (node.propertyName ?? node.name).text
            if (forbiddenIcons.has(imported)) {
              findings.push(`${filename}:${source.getLineAndCharacterOfPosition(node.getStart()).line + 1}: ${imported}`)
              sourceCount++
            }
          } else if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
            const tag = node.tagName
            if (ts.isIdentifier(tag) && forbiddenIcons.has(tag.text)) {
              findings.push(`${filename}:${source.getLineAndCharacterOfPosition(tag.getStart()).line + 1}: <${tag.text} />`)
              sourceCount++
            }
          }
          ts.forEachChild(node, visit)
        }
        visit(source)
      }
    }
  }
  walk(path.join(base, "src"))
  const locales = path.join(base, "src/i18n/locales")
  for (const file of fs.readdirSync(locales).filter((f) => f.endsWith(".json"))) {
    for (const [key, value] of Object.entries(JSON.parse(fs.readFileSync(path.join(locales, file), "utf8")))) {
      scan(key, file); scan(value, `${file} (${key})`)
    }
  }
  const [header, ...rows] = csvRows(fs.readFileSync(path.join(base, "src/i18n/catalog.csv"), "utf8"))
  const valuesEnd = header?.indexOf("sources")
  if (valuesEnd < 6 || !rows.length) throw new Error("Unexpected catalog.csv schema")
  for (const [i, row] of rows.entries()) {
    if (row.length !== header.length) throw new Error(`catalog.csv:${i + 2}: unexpected column count`)
    for (let col = 0; col < valuesEnd; col++) scan(row[col], `catalog.csv:${i + 2}`)
  }
  const html = fs.readFileSync(path.join(base, "index.html"), "utf8")
  for (const match of html.matchAll(/<title>([^<]*)<\/title>|\bcontent="([^"]*)"/g)) {
    scan(match[1] ?? match[2], "index.html")
  }
  return { findings, sourceCount }
}

// Inspect display text recursively. Request transport fields and JSON examples
// are the public API, and retain their actual callable identifiers.
export function generatedApiFindings(docs) {
  const findings = []
  const inspect = (text, where) => {
    const prose = String(text ?? "")
      .replace(/\bkamra\.(?:[a-z_][a-z_0-9]*\.)+[a-z_][a-z_0-9]*\b/g, "")
      .replace(/\b(?:frappe\.(?:call|get_list|session\.user|model\.workflow\.apply_workflow)|kamra\.credit|kamra_demo_mode)\b/g, "")
      .replace(/\/kamra\.postman_collection\.json|kamra\.<module>\.<function>/g, "")
    if (forbidden.test(prose)) findings.push(where + ": " + text)
  }
  const collection = JSON.parse(fs.readFileSync(path.join(docs, "public/kamra.postman_collection.json"), "utf8"))
  const visit = (item, where) => {
    inspect(item.name, where + ".name")
    for (const [field, value] of [["description", item.description], ["request.description", item.request?.description]]) {
      inspect(typeof value === "object" ? value?.content : value, where + "." + field)
    }
    for (const [i, child] of (item.item ?? []).entries()) visit(child, where + ".item[" + i + "]")
  }
  visit(collection.info, "collection.info")
  collection.item.forEach((item, i) => visit(item, "collection.item[" + i + "]"))
  fs.readFileSync(path.join(docs, "api-reference.md"), "utf8").split("\n")
    .forEach((line, i) => inspect(line, "api-reference.md:" + (i + 1)))
  return findings
}

export function checkSource() {
  const { findings, sourceCount } = scanTree()
  findings.push(...generatedApiFindings(path.resolve(root, "../docs-site")))
  if (findings.length) {
    console.error(findings.join("\n"))
    throw new Error(`${sourceCount} UI source findings; ${findings.length} total branding findings`)
  }
  console.log("Brand leak check passed: source, locales, catalog and shell")
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) checkSource()
