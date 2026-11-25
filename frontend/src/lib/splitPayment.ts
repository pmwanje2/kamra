// Settling one bill with several payment modes at check-out (#597).
// Pure helpers, so the dialog stays small: totals, what is wrong, and how the
// lines are shared across the folios that owe money.

import { moneyEps, roundMoney } from "./money"

export type PayLine = { id: string; mode: string; amount: string; reference: string; key: string }

export const MAX_LINES = 20

// Cash is the only mode that can be taken without a reference.
export const isCash = (mode: string): boolean => mode === "Cash"

export const lineAmount = (l: PayLine): number => {
  const n = Number(l.amount)
  return Number.isFinite(n) ? n : 0
}

export function splitTotals(lines: PayLine[], due: number, currency?: string) {
  const entered = roundMoney(lines.reduce((s, l) => s + lineAmount(l), 0), currency)
  return { entered, left: roundMoney(due - entered, currency) }
}

// The first thing that stops the cashier collecting, or null when the lines
// are ready. `msg` turns a message into the user's language.
export function splitProblem(
  lines: PayLine[],
  due: number,
  msg: (s: string) => string,
  currency?: string,
): string | null {
  if (lines.length === 0) return msg("Add a payment line.")
  for (const l of lines) {
    if (!l.mode) return msg("Choose a payment mode on every line.")
    if (!(lineAmount(l) > 0)) return msg("Every line needs an amount above zero.")
    if (!isCash(l.mode) && !l.reference.trim()) return msg("Enter a reference on every non-cash line.")
  }
  const eps = moneyEps(currency)
  const { left } = splitTotals(lines, due, currency)
  if (left > eps) return msg("The lines do not cover the balance yet.")
  if (left < -eps) return msg("The lines add up to more than the balance.")
  return null
}

export type PaymentRow = {
  folio: string
  mode: string
  amount: number
  reference?: string
  kind: "Payment"
  idempotency_key: string
}

// Share the lines across the folios that owe, in order, splitting a line where
// one folio's balance ends. Each row carries a stable idempotency key (the
// line's key plus the folio) so a retry never pays twice.
export function allocate(
  lines: PayLine[],
  owed: { folio: string; amount: number }[],
  currency?: string,
): PaymentRow[] {
  const eps = moneyEps(currency)
  const rows: PaymentRow[] = []
  const need = owed.map((o) => ({ folio: o.folio, left: roundMoney(o.amount, currency) }))
  let f = 0
  for (const line of lines) {
    let todo = roundMoney(lineAmount(line), currency)
    while (todo > eps && f < need.length) {
      const take = roundMoney(Math.min(todo, need[f].left), currency)
      if (take > 0) {
        rows.push({
          folio: need[f].folio, mode: line.mode, amount: take, kind: "Payment",
          reference: line.reference.trim() || undefined,
          idempotency_key: `${line.key}:${need[f].folio}`,
        })
      }
      todo = roundMoney(todo - take, currency)
      need[f].left = roundMoney(need[f].left - take, currency)
      if (need[f].left <= eps) f += 1
    }
  }
  return rows
}
