/*  P41: Night Audit Step 9 itemised posting summary card.
    Shows room nights, package element splits, recurring fixed charges, and
    settled package breakage formatted at the property's ISO 4217 digits. */

import { BedDouble, Layers, Repeat, Scale } from "lucide-react"
import { cur, fmtAmount } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"

export interface AuditPostingSummary {
  room_charges_posted?: number
  package_elements_posted?: number
  package_elements_amount?: number
  fixed_charges_posted?: number
  fixed_charges_amount?: number
  package_breakage_count?: number
  package_breakage_total?: number
  amount_posted?: number
}

export default function AuditSummaryCard({
  summary,
  currency,
}: {
  summary: AuditPostingSummary
  currency?: string
}) {
  useUILanguage()
  const sym = cur()
  const fmt = (n: unknown) => `${sym}${fmtAmount(n, currency || undefined)}`

  const items = [
    {
      key: "rooms",
      label: translate("Room nights posted"),
      value: String(Number(summary.room_charges_posted ?? 0)),
      sub: undefined as string | undefined,
      icon: BedDouble,
    },
    {
      key: "packages",
      label: translate("Package element splits"),
      value: String(Number(summary.package_elements_posted ?? 0)),
      sub:
        summary.package_elements_amount !== undefined
          ? fmt(summary.package_elements_amount)
          : undefined,
      icon: Layers,
    },
    {
      key: "fixed",
      label: translate("Fixed charges posted"),
      value: String(Number(summary.fixed_charges_posted ?? 0)),
      sub: fmt(summary.fixed_charges_amount ?? 0),
      icon: Repeat,
    },
    {
      key: "breakage",
      label: translate("Package breakage settled"),
      value: fmt(summary.package_breakage_total ?? 0),
      sub:
        summary.package_breakage_count !== undefined
          ? translate("{n} settled", { n: Number(summary.package_breakage_count) })
          : undefined,
      icon: Scale,
    },
  ]

  return (
    <section
      aria-label={translate("Night audit posting summary")}
      className="rounded-xl border border-line bg-surface p-4 space-y-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold text-ink">
          {translate("Night audit posting summary")}
        </h2>
        {summary.amount_posted !== undefined && (
          <span className="text-sm font-semibold tabular-nums text-ink">
            {translate("Total posted")}: {fmt(summary.amount_posted)}
          </span>
        )}
      </div>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {items.map((item) => {
          const Icon = item.icon
          return (
            <div
              key={item.key}
              className="rounded-lg border border-line bg-canvas p-3 text-start"
            >
              <div className="flex items-center gap-1.5 text-xs font-medium text-muted-ink">
                <Icon className="size-3.5 shrink-0 text-muted-ink" aria-hidden />
                <span>{item.label}</span>
              </div>
              <div className="mt-1 text-lg font-semibold tabular-nums text-ink">
                {item.value}
              </div>
              {item.sub && (
                <div className="mt-0.5 text-xs tabular-nums text-muted-ink">{item.sub}</div>
              )}
            </div>
          )
        })}
      </div>
    </section>
  )
}
