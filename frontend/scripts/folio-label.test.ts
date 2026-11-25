/* Folio window label shown in FolioView's bill switcher and transfer bar:
   shows the window's payee (Company, Travel Agent, named guest) rather than
   falling back to "Extra" for a Travel Agent window. */
import { describe, expect, it } from "vitest"
import { folioLabel, type SiblingFolio } from "../src/lib/folioLabel"

const t = (s: string, v?: Record<string, string | number>) =>
  v ? `[${s.replace(/\{(\w+)\}/g, (_, k) => String(v[k]))}]` : `[${s}]`

const base: SiblingFolio = {
  name: "F-1",
  folio_type: "Guest",
  status: "Open",
  balance: 0,
  grand_total: 0,
  payments_total: 0,
  window_no: 1,
  payee_type: "Guest",
}

describe("folioLabel", () => {
  it("labels a Travel Agent window 3 by its agent, not Extra", () => {
    const w1: SiblingFolio = { ...base }
    const w2: SiblingFolio = {
      ...base,
      name: "F-2",
      folio_type: "Company",
      window_no: 2,
      payee_type: "Company",
      payee_company: "Acme Safari Ltd",
      company: "Acme Safari Ltd",
    }
    const w3: SiblingFolio = {
      ...base,
      name: "F-3",
      folio_type: "Extra",
      window_no: 3,
      payee_type: "Travel Agent",
      payee_agent: "TA-001",
      agent_name: "Kigo Tours",
    }
    const siblings = [w1, w2, w3]
    expect(folioLabel(siblings, w1, t)).toBe("[Guest]")
    expect(folioLabel(siblings, w2, t)).toBe("[Company] · Acme Safari Ltd")
    expect(folioLabel(siblings, w3, t)).toBe("[Travel Agent] · Kigo Tours")
  })

  it("keeps plain Extra and named bill_to_name labels intact", () => {
    const w1: SiblingFolio = { ...base }
    const extra: SiblingFolio = {
      ...base,
      name: "F-4",
      folio_type: "Extra",
      window_no: 4,
      payee_type: "Guest",
    }
    const named: SiblingFolio = {
      ...base,
      name: "F-5",
      folio_type: "Extra",
      window_no: 5,
      payee_type: "Guest",
      bill_to_name: "Sarah Okello",
    }
    const siblings = [w1, extra, named]
    expect(folioLabel(siblings, extra, t)).toBe("[Extra]")
    expect(folioLabel(siblings, named, t)).toBe("Sarah Okello")
  })
})
