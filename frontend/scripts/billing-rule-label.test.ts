/* A company billing rule shown at check-in and check-out: a charge-type rule
   by its (translated) charge type, a transaction-code rule by its code. */
import { describe, expect, it } from "vitest"
import { ruleKey, ruleLabel } from "../src/lib/billingRules"

const t = (s: string) => `[${s}]`

describe("billing rule label", () => {
  it("names a charge-type rule by its charge type", () => {
    const rule = { charge_type: "Room", pay_by: "Company" }
    expect([ruleLabel(rule, t), ruleKey(rule)]).toEqual(["[Room]", "Room"])
  })
  it("names a code rule by its code, untranslated", () => {
    const rule = { charge_type: null, transaction_code: "2000",
      code_label: "2000 Food & Beverage", pay_by: "Company" }
    expect([ruleLabel(rule, t), ruleKey(rule)]).toEqual(["2000 Food & Beverage", "#2000"])
  })
})
