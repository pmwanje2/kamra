import { t } from "./i18n"

export function ratePlanLabel(label: string, plan: {
  is_default?: boolean | number
  rate_category?: string | null
  currency?: string | null
}, negotiated: boolean) {
  return [label, negotiated && t("Negotiated"), plan.is_default && t("Default"),
    plan.rate_category && plan.rate_category !== "Standard" && t(plan.rate_category),
    plan.currency].filter(Boolean).join(" · ")
}
