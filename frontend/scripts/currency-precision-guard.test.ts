/** Permanent CI guard: prevents hardcoded currency precision (`step="0.01"` or
 *  hardcoded `0.01` step variables/fallbacks, `minimumFractionDigits: 2`,
 *  `maximumFractionDigits: 0`, `.toFixed(2)` on money, `Math.round(...) / 100`,
 *  integer `Math.round(<money>)`, hardcoded `0.009`/`0.005`/`0.5` epsilons,
 *  and money `<input type="number">` missing `step`) and hardcoded `"en-US"`
 *  formatting locales across `frontend/src/`.
 *
 *  Files temporarily owned by parallel PRs are tracked in `PENDING_CROSS_PR_ALLOWLIST`
 *  with a strict exact-count ratchet: peer PRs decrement/remove their entries as
 *  they land; no unlisted file, increased count, or stale excess cap is permitted. */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"
import { describe, expect, it } from "vitest"

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

// Verified non-money `.toFixed(2)` sites (percentages & item quantities, never money).
const EXEMPT_TO_FIXED_2: Readonly<Record<string, number>> = {
  "src/lib/folioLabel.ts": 1, // split % ratio (:82)
  "src/screens/FolioView.tsx": 2, // split % ratios (:801, :825)
  "src/screens/BanquetDocument.tsx": 2, // line qty (:569, :684)
  "src/screens/banquet/Economics.tsx": 1, // quoted_qty (:238)
}

// Strict exact-count ratchet for parallel-PR-owned files outside PR 1's ownership boundary.
// Every entry must match its exact live finding count (no excess/stale caps permitted).
const PENDING_CROSS_PR_ALLOWLIST: Readonly<Record<string, number>> = {
  // Pack 2 PR 4 (#43)
  "src/screens/FolioHistory.tsx": 1,
  // Peer Banquet / Events (#45) & setup screens (pre-existing missing-step)
  "src/screens/BanquetCatalogue.tsx": 1,
  "src/screens/BanquetFunction.tsx": 7,
  "src/screens/banquet/Economics.tsx": 2,
  "src/screens/BookingEngine.tsx": 1,
  "src/screens/Setup.tsx": 4,
}

export interface PrecisionFinding {
  file: string
  line: number
  kind:
    | "step-0.01"
    | "fraction-digits"
    | "toFixed-2"
    | "en-US-locale"
    | "round-100"
    | "round-money"
    | "hardcoded-eps"
    | "missing-step"
  snippet: string
}

const MONEY_IDENT_RE =
  /(?:^|[^a-zA-Z])(amount|amt|rate|price|deposit|due|fee|charge|balance|total|float|damage|cost|discount|allowance|settle|payment|refund|credit|payable|receivable|impact|bill|difference)(?:$|[^a-zA-Z])/i
const NON_MONEY_IDENT_RE =
  /(?:^|[^a-zA-Z])(nights|adults|children|pets|guests|pax|attendees|rooms|rooms_blocked|count|qty|quantity|age|port|days|pct|percent|gst_rate|tax_rate|extra_beds|choice_of|start|end|free_child_age|min_pax|minutes|hours|secs|width|height|scroll|time|date|occupancy|ms|rtt|scale|unit|factor|index)(?:$|[^a-zA-Z])/i

function splitIdent(text: string): string {
  return text
    .replace(/gst_rate|tax_rate|service_?charge_?pct/gi, "")
    .replace(/([a-z])([A-Z])/g, "$1 $2")
}

function isLiteral001(node: ts.Node | undefined): boolean {
  if (!node) return false
  if (ts.isStringLiteral(node) && node.text === "0.01") return true
  if (ts.isNumericLiteral(node) && Number(node.text) === 0.01) return true
  return false
}

function containsLiteral001(node: ts.Node): boolean {
  let found = false
  function walk(n: ts.Node) {
    if (found) return
    if (isLiteral001(n)) {
      found = true
      return
    }
    ts.forEachChild(n, walk)
  }
  walk(node)
  return found
}

function isPercentBranchTernary(cond: ts.ConditionalExpression, source: ts.SourceFile): boolean {
  const condText = cond.condition.getText(source)
  // e.g. d.type === "Amount" ? amountStep : "0.01"  OR  d.kind === "Percent" ? "0.01" : moneyStep()
  if (isLiteral001(cond.whenFalse) && /===\s*["']Amount["']/.test(condText) && !containsLiteral001(cond.whenTrue)) {
    return true
  }
  if (isLiteral001(cond.whenTrue) && /===\s*["']Percent["']/.test(condText) && !containsLiteral001(cond.whenFalse)) {
    return true
  }
  return false
}

function isPercentInput(jsxElem: ts.JsxOpeningLikeElement): boolean {
  let hasMin0 = false
  let hasMax100 = false
  let hasPercentHint = false
  for (const prop of jsxElem.attributes.properties) {
    if (!ts.isJsxAttribute(prop) || !ts.isIdentifier(prop.name)) continue
    const attrName = prop.name.text
    const init = prop.initializer
    const raw = init && ts.isStringLiteral(init)
      ? init.text
      : init && ts.isJsxExpression(init) && init.expression
        ? init.expression.getText()
        : ""
    if (attrName === "min" && raw === "0") hasMin0 = true
    if (attrName === "max" && raw === "100") hasMax100 = true
    if ((attrName === "aria-label" || attrName === "placeholder") && /%|percent/i.test(raw)) {
      hasPercentHint = true
    }
  }
  return hasMin0 && hasMax100 && hasPercentHint
}

function insideFormattingCall(node: ts.Node): boolean {
  let cur: ts.Node | undefined = node.parent
  while (cur) {
    if (ts.isCallExpression(cur)) {
      const text = cur.expression.getText()
      if (text.endsWith(".toLocaleString") || text === "Intl.NumberFormat") return true
    }
    if (ts.isNewExpression(cur) && cur.expression.getText() === "Intl.NumberFormat") {
      return true
    }
    if (ts.isFunctionLike(cur)) break
    cur = cur.parent
  }
  return false
}

function isMathRoundCall(node: ts.Node, source: ts.SourceFile): node is ts.CallExpression {
  return ts.isCallExpression(node) && node.expression.getText(source) === "Math.round"
}

function numericLiteralValue(node: ts.Node): number | null {
  if (ts.isNumericLiteral(node)) return Number(node.text)
  if (
    ts.isPrefixUnaryExpression(node) &&
    node.operator === ts.SyntaxKind.MinusToken &&
    ts.isNumericLiteral(node.operand)
  ) {
    return -Number(node.operand.text)
  }
  return null
}

export function scanSource(relFile: string, content: string): PrecisionFinding[] {
  const source = ts.createSourceFile(relFile, content, ts.ScriptTarget.Latest, true)
  const findings: PrecisionFinding[] = []
  const lineOf = (n: ts.Node) => source.getLineAndCharacterOfPosition(n.getStart(source)).line + 1

  function visit(node: ts.Node) {
    // 1a. Hardcoded step="0.01", step={0.01}, step={"0.01"}, or conditional step={... "0.01" ...}
    if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) && node.name.text === "step") {
      const init = node.initializer
      let isHardcoded = false
      if (init && isLiteral001(init)) {
        isHardcoded = true
      } else if (init && ts.isJsxExpression(init) && init.expression) {
        const expr = init.expression
        if (isLiteral001(expr)) {
          isHardcoded = true
        } else if (ts.isConditionalExpression(expr)) {
          if (containsLiteral001(expr) && !isPercentBranchTernary(expr, source)) {
            isHardcoded = true
          }
        }
      }
      if (isHardcoded) {
        const parentElem = node.parent?.parent
        const pct = parentElem && (ts.isJsxOpeningElement(parentElem) || ts.isJsxSelfClosingElement(parentElem))
          ? isPercentInput(parentElem)
          : false
        if (!pct) {
          findings.push({
            file: relFile,
            line: lineOf(node),
            kind: "step-0.01",
            snippet: node.getText(source),
          })
        }
      }
    }

    // 1b. Variable-assigned hardcoded "0.01" step (e.g. `const amountStep = digits > 2 ? ... : "0.01"`)
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      /step/i.test(node.name.text) &&
      !/percent|pct/i.test(node.name.text) &&
      node.initializer &&
      containsLiteral001(node.initializer)
    ) {
      findings.push({
        file: relFile,
        line: lineOf(node),
        kind: "step-0.01",
        snippet: node.getText(source),
      })
    }

    // 2. Hardcoded minimumFractionDigits: 2 or maximumFractionDigits: 0 in formatting calls
    if (ts.isPropertyAssignment(node) && ts.isIdentifier(node.name)) {
      const prop = node.name.text
      if (
        (prop === "minimumFractionDigits" && ts.isNumericLiteral(node.initializer) && node.initializer.text === "2") ||
        (prop === "maximumFractionDigits" && ts.isNumericLiteral(node.initializer) && node.initializer.text === "0")
      ) {
        if (insideFormattingCall(node)) {
          findings.push({
            file: relFile,
            line: lineOf(node),
            kind: "fraction-digits",
            snippet: node.getText(source),
          })
        }
      }
    }

    // 3. Hardcoded .toFixed(2)
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      node.expression.name.text === "toFixed" &&
      node.arguments.length === 1 &&
      ts.isNumericLiteral(node.arguments[0]) &&
      node.arguments[0].text === "2"
    ) {
      findings.push({
        file: relFile,
        line: lineOf(node),
        kind: "toFixed-2",
        snippet: node.getText(source),
      })
    }

    // 4. Hardcoded "en-US" passed as first arg to toLocaleString / toLocaleDateString / Intl.NumberFormat / uiDateLocale
    if (ts.isCallExpression(node) || ts.isNewExpression(node)) {
      const exprText = node.expression.getText(source)
      const isTarget =
        exprText.endsWith(".toLocaleString") ||
        exprText.endsWith(".toLocaleDateString") ||
        exprText === "Intl.NumberFormat" ||
        exprText === "uiDateLocale"
      const firstArg = node.arguments?.[0]
      if (isTarget && firstArg && ts.isStringLiteral(firstArg) && firstArg.text === "en-US") {
        findings.push({
          file: relFile,
          line: lineOf(firstArg),
          kind: "en-US-locale",
          snippet: node.getText(source),
        })
      }
    }

    // 5. Hardcoded 2dp rounding: `Math.round(...) / 100`
    if (
      ts.isBinaryExpression(node) &&
      node.operatorToken.kind === ts.SyntaxKind.SlashToken &&
      isMathRoundCall(node.left, source) &&
      ts.isNumericLiteral(node.right) &&
      node.right.text === "100"
    ) {
      findings.push({
        file: relFile,
        line: lineOf(node),
        kind: "round-100",
        snippet: node.getText(source),
      })
    }

    // 6. Hardcoded integer rounding on money expressions: `Math.round(groupBillDue)`
    if (isMathRoundCall(node, source) && node.arguments.length === 1) {
      const parent = node.parent
      const isDivided =
        ts.isBinaryExpression(parent) &&
        parent.operatorToken.kind === ts.SyntaxKind.SlashToken &&
        parent.left === node
      const isFracCheck =
        ts.isBinaryExpression(parent) &&
        parent.operatorToken.kind === ts.SyntaxKind.MinusToken &&
        parent.right === node
      if (!isDivided && !isFracCheck) {
        const argText = node.arguments[0].getText(source)
        const isPercentage = /\*\s*100\b/.test(argText)
        const cleaned = splitIdent(argText)
        if (!isPercentage && MONEY_IDENT_RE.test(cleaned) && !NON_MONEY_IDENT_RE.test(cleaned)) {
          findings.push({
            file: relFile,
            line: lineOf(node),
            kind: "round-money",
            snippet: node.getText(source),
          })
        }
      }
    }

    // 7a. Hardcoded epsilon constants (`const EPS = 0.009`, `const SPLIT_EPS = 0.009`)
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      /(?:^|_)EPS$/i.test(node.name.text) &&
      node.initializer
    ) {
      const val = numericLiteralValue(node.initializer)
      if (val !== null && (val === 0.009 || val === 0.005 || val === 0.5)) {
        findings.push({
          file: relFile,
          line: lineOf(node),
          kind: "hardcoded-eps",
          snippet: node.getText(source),
        })
      }
    }

    // 7b. Hardcoded money balance/due/difference epsilon comparisons (`> 0.5`, `< -0.5`, `> 0.009`, `>= 0.005`)
    if (
      ts.isBinaryExpression(node) &&
      [
        ts.SyntaxKind.GreaterThanToken,
        ts.SyntaxKind.GreaterThanEqualsToken,
        ts.SyntaxKind.LessThanToken,
        ts.SyntaxKind.LessThanEqualsToken,
      ].includes(node.operatorToken.kind)
    ) {
      const rhs = numericLiteralValue(node.right)
      if (rhs !== null && [0.5, -0.5, 0.009, -0.009, 0.005, -0.005].includes(rhs)) {
        const cleaned = splitIdent(node.left.getText(source))
        if (MONEY_IDENT_RE.test(cleaned) && !NON_MONEY_IDENT_RE.test(cleaned)) {
          findings.push({
            file: relFile,
            line: lineOf(node),
            kind: "hardcoded-eps",
            snippet: node.getText(source),
          })
        }
      }
    }

    // 8. Money `<input type="number">` missing explicit `step` (defaults browser to integer step="1")
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) &&
      node.tagName.getText(source) === "input"
    ) {
      let isNum = false
      let hasStep = false
      let hasDecMode = false
      let hasCurHint = false
      let attrText = ""
      for (const prop of node.attributes.properties) {
        if (!ts.isJsxAttribute(prop) || !ts.isIdentifier(prop.name)) continue
        const k = prop.name.text
        const v = prop.initializer ? prop.initializer.getText(source) : ""
        if (k === "type" && (v === '"number"' || v === "'number'")) isNum = true
        if (k === "step") hasStep = true
        if (k === "inputMode" && (v === '"decimal"' || v === "'decimal'")) hasDecMode = true
        if (["value", "placeholder", "aria-label", "name", "id"].includes(k)) {
          attrText += ` ${v}`
          if (/\bcur\s*\(/.test(v)) hasCurHint = true
        }
      }
      if (isNum && !hasStep) {
        const cleaned = splitIdent(attrText)
        const isMoney =
          hasDecMode ||
          hasCurHint ||
          (MONEY_IDENT_RE.test(cleaned) && !NON_MONEY_IDENT_RE.test(cleaned))
        if (isMoney) {
          findings.push({
            file: relFile,
            line: lineOf(node),
            kind: "missing-step",
            snippet: node.getText(source).replace(/\s+/g, " ").slice(0, 120),
          })
        }
      }
    }

    ts.forEachChild(node, visit)
  }

  visit(source)
  return findings
}

function collectFiles(dir: string): string[] {
  const out: string[] = []
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) out.push(...collectFiles(full))
    else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full)
  }
  return out
}

describe("currency precision & locale AST guard", () => {
  it("flags hardcoded step, fraction digits, .toFixed(2), en-US, round-100, round-money, eps, and missing-step on fixtures", () => {
    const bad = scanSource("src/screens/Fixture.tsx", [
      'export const A = () => <input type="number" step="0.01" />',
      'export const B = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })',
      'export const C = (n: number) => n.toLocaleString(loc, { maximumFractionDigits: 0 })',
      'export const D = (n: number) => n.toFixed(2)',
      'export const E = () => uiDateLocale("en-US")',
      'export const amountStep = digits > 2 ? (10 ** -digits).toFixed(digits) : "0.01"',
      'export const F = (a: number, b: number) => Math.round((a - b) * 100) / 100',
      'export const G = (groupBillDue: number) => String(Math.round(groupBillDue))',
      'export const EPS = 0.009',
      'export const H = (own_balance: number) => own_balance > 0.5',
      'export const I = (amount: string) => <input type="number" inputMode="decimal" value={amount} />',
    ].join("\n"))
    expect(bad.map((f) => f.kind)).toEqual([
      "step-0.01",
      "en-US-locale",
      "fraction-digits",
      "fraction-digits",
      "toFixed-2",
      "en-US-locale",
      "step-0.01",
      "round-100",
      "round-money",
      "hardcoded-eps",
      "hardcoded-eps",
      "missing-step",
    ])
  })

  it("permits dynamic moneyStep/roundMoney/moneyEps, percent branches, and integer count inputs", () => {
    const good = scanSource("src/screens/Good.tsx", [
      'export const A = () => <input type="number" step={moneyStep()} value={amount} />',
      'export const B = () => <input type="number" min="0" max="100" step="0.01" aria-label="Discount %" />',
      'export const B2 = (d: { type: string }) => <input type="number" step={d.type === "Amount" ? moneyStep() : "0.01"} />',
      'export const C = (n: number) => fmtMoney(n)',
      'export const D = () => [["en-US", "English (US)"]]',
      'export const E = (groupBillDue: number) => roundMoney(groupBillDue)',
      'export const F = (own_balance: number) => own_balance > moneyEps()',
      'export const G = (nights: number) => <input type="number" min={1} value={nights} />',
      'export const H = (p: { picked_up: number; rooms_blocked: number }) => Math.round((p.picked_up / p.rooms_blocked) * 100)',
    ].join("\n"))
    expect(good).toEqual([])
  })

  it("enforces zero hardcoded precision/en-US leaks outside verified exemptions and exact cross-PR ratchet", () => {
    const files = collectFiles(path.join(FRONTEND_ROOT, "src"))
    const unexpected: string[] = []
    const ratchetExceeded: string[] = []
    const staleAllowlist: string[] = []
    const countsByFile = new Map<string, number>()

    for (const full of files) {
      const rel = path.relative(FRONTEND_ROOT, full).replace(/\\/g, "/")
      if (rel === "src/lib/money.ts") continue
      const all = scanSource(rel, fs.readFileSync(full, "utf8"))
      // Subtract verified non-money `.toFixed(2)` exemptions
      const exemptToFixed = EXEMPT_TO_FIXED_2[rel] ?? 0
      let skipped = 0
      const active = all.filter((f) => {
        if (f.kind === "toFixed-2" && skipped < exemptToFixed) {
          skipped++
          return false
        }
        return true
      })
      countsByFile.set(rel, active.length)
      if (active.length === 0) continue
      const cap = PENDING_CROSS_PR_ALLOWLIST[rel]
      if (cap === undefined) {
        for (const f of active) unexpected.push(`${f.file}:${f.line} [${f.kind}] ${f.snippet}`)
      } else if (active.length > cap) {
        ratchetExceeded.push(`${rel}: found ${active.length}, ratchet cap is ${cap}`)
      }
    }

    for (const [rel, cap] of Object.entries(PENDING_CROSS_PR_ALLOWLIST)) {
      const actual = countsByFile.get(rel) ?? 0
      if (actual < cap) {
        staleAllowlist.push(`${rel}: found ${actual}, allowlist cap is ${cap} (tighten or remove stale entry)`)
      }
    }

    expect(unexpected, `Hardcoded precision/en-US findings in owned files:\n${unexpected.join("\n")}`).toEqual([])
    expect(ratchetExceeded, `Cross-PR ratchet exceeded:\n${ratchetExceeded.join("\n")}`).toEqual([])
    expect(staleAllowlist, `Stale/excess cross-PR allowlist caps:\n${staleAllowlist.join("\n")}`).toEqual([])
  })
})
