/** Permanent CI guard: prevents hardcoded currency precision (`step="0.01"`,
 *  `minimumFractionDigits: 2`, `maximumFractionDigits: 0`, `.toFixed(2)` on money)
 *  and hardcoded `"en-US"` formatting locales across `frontend/src/`.
 *
 *  Files temporarily owned by parallel PRs are tracked in `PENDING_CROSS_PR_ALLOWLIST`
 *  with a strict monotonic count ceiling; peer PRs decrement/remove their entries as
 *  they land, and no unlisted file or increased count is ever permitted. */
import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"
import { describe, expect, it } from "vitest"

const FRONTEND_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

// Verified non-money `.toFixed(2)` sites (percentages & item quantities, never money).
const EXEMPT_TO_FIXED_2: Readonly<Record<string, number>> = {
  "src/screens/FolioView.tsx": 2, // split % ratios (:835, :859)
  "src/screens/BanquetDocument.tsx": 2, // line qty (:569, :684)
  "src/screens/banquet/Economics.tsx": 1, // quoted_qty (:238)
}

// Monotonic ratchet for parallel-PR-owned files outside PR 1's ownership boundary.
// Each entry caps the maximum allowed findings in that file until its owning PR lands.
const PENDING_CROSS_PR_ALLOWLIST: Readonly<Record<string, number>> = {
  // Wave 0 #53
  "src/components/RowCorrection.tsx": 1,
  "src/screens/Receivables.tsx": 2,
  // Wave 0 #41 / Pack 1
  "src/components/SecurityDepositPanel.tsx": 1,
  "src/screens/AuditPack.tsx": 3,
  // Pack 3 PR 2 (PMS-P0-TAPECHART-RESPONSIVE)
  "src/screens/EndOfDayWizard.tsx": 1,
  // Pack 2 PR 4 (#43)
  "src/screens/FolioHistory.tsx": 1,
  // Pack 3 PR 3 (PMS-P0-FRENCH-RTL-I18N)
  "src/screens/CashierBoard.tsx": 3,
  "src/screens/CashierSessions.tsx": 3,
  "src/screens/CashierShiftReport.tsx": 3,
  "src/screens/CashierTill.tsx": 3,
  "src/screens/banquet/shared.tsx": 4,
  "src/screens/BanquetMonth.tsx": 2,
}

export interface PrecisionFinding {
  file: string
  line: number
  kind: "step-0.01" | "fraction-digits" | "toFixed-2" | "en-US-locale"
  snippet: string
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

export function scanSource(relFile: string, content: string): PrecisionFinding[] {
  const source = ts.createSourceFile(relFile, content, ts.ScriptTarget.Latest, true)
  const findings: PrecisionFinding[] = []
  const lineOf = (n: ts.Node) => source.getLineAndCharacterOfPosition(n.getStart(source)).line + 1

  function visit(node: ts.Node) {
    // 1. Hardcoded step="0.01" or step={0.01} / step={"0.01"}
    if (ts.isJsxAttribute(node) && ts.isIdentifier(node.name) && node.name.text === "step") {
      const init = node.initializer
      const isHardcoded =
        (init && ts.isStringLiteral(init) && init.text === "0.01") ||
        (init && ts.isJsxExpression(init) && init.expression && (
          (ts.isNumericLiteral(init.expression) && Number(init.expression.text) === 0.01) ||
          (ts.isStringLiteral(init.expression) && init.expression.text === "0.01")
        ))
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
  it("flags hardcoded step, fraction digits, .toFixed(2), and en-US formatting on fixtures", () => {
    const bad = scanSource("src/screens/Fixture.tsx", [
      'export const A = () => <input type="number" step="0.01" />',
      'export const B = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })',
      'export const C = (n: number) => n.toLocaleString(loc, { maximumFractionDigits: 0 })',
      'export const D = (n: number) => n.toFixed(2)',
      'export const E = () => uiDateLocale("en-US")',
    ].join("\n"))
    expect(bad.map((f) => f.kind)).toEqual([
      "step-0.01",
      "en-US-locale",
      "fraction-digits",
      "fraction-digits",
      "toFixed-2",
      "en-US-locale",
    ])
  })

  it("permits dynamic moneyStep/fmtMoney and percentage inputs with 0..100 bounds", () => {
    const good = scanSource("src/screens/Good.tsx", [
      'export const A = () => <input type="number" step={moneyStep()} />',
      'export const B = () => <input type="number" min="0" max="100" step="0.01" aria-label="Discount %" />',
      'export const C = (n: number) => fmtMoney(n)',
      'export const D = () => [["en-US", "English (US)"]]',
    ].join("\n"))
    expect(good).toEqual([])
  })

  it("enforces zero hardcoded precision/en-US leaks outside verified exemptions and shrinking cross-PR ratchet", () => {
    const files = collectFiles(path.join(FRONTEND_ROOT, "src"))
    const unexpected: string[] = []
    const ratchetExceeded: string[] = []

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
      if (active.length === 0) continue
      const cap = PENDING_CROSS_PR_ALLOWLIST[rel]
      if (cap === undefined) {
        for (const f of active) unexpected.push(`${f.file}:${f.line} [${f.kind}] ${f.snippet}`)
      } else if (active.length > cap) {
        ratchetExceeded.push(`${rel}: found ${active.length}, ratchet cap is ${cap}`)
      }
    }

    expect(unexpected, `Hardcoded precision/en-US findings in owned files:\n${unexpected.join("\n")}`).toEqual([])
    expect(ratchetExceeded, `Cross-PR ratchet exceeded:\n${ratchetExceeded.join("\n")}`).toEqual([])
  })
})
