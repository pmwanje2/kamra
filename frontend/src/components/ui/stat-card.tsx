import type { ReactNode } from "react"
import { Link } from "react-router-dom"
import { ArrowUpRight, ArrowDownRight } from "lucide-react"
import { cn } from "../../lib/utils"
import { Sparkline } from "./sparkline"
import { t as translate, useUILanguage } from "../../lib/i18n"

/**
 * One headline metric in the front-desk KPI strip: a soft-green icon chip,
 * a quiet label, the big number, and either a sparkline or a progress bar,
 * with an optional delta vs. a prior period.
 *
 * Layout rule: the value is never beside a 96px spark on a 6-up grid - that
 * used to draw the chart through the digits. Spark sits on its own row under
 * the number; sub text is full-width below that.
 */
export function StatCard({
  icon,
  label,
  value,
  spark,
  sparkColor,
  progress,
  progressLabel,
  delta,
  sub,
  accent,
  to,
  className,
}: {
  icon: ReactNode
  label: string
  value: ReactNode
  spark?: number[]
  sparkColor?: string
  progress?: number // 0..100
  progressLabel?: string
  delta?: { value: string; dir?: "up" | "down" } // e.g. { value: "20% vs yesterday", dir: "up" }
  sub?: ReactNode
  accent?: boolean
  /** When set, the whole card navigates here (e.g. Open tasks → HK). */
  to?: string
  className?: string
}) {
  useUILanguage()
  const down = delta?.dir === "down"
  const inner = (
    <>
      <div className="flex items-center gap-2">
        <span className="grid size-8 shrink-0 place-items-center rounded-lg bg-accent-subtle text-accent-ink">
          {icon}
        </span>
        <span className="truncate text-[13px] font-medium text-muted-ink">
          {translate(label)}
        </span>
      </div>
      <div className="mt-2.5 flex flex-wrap items-end justify-between gap-x-2 gap-y-1.5">
        <div className="max-w-full">
          <div
            className={cn(
              "text-2xl font-semibold tracking-tight tabular-nums",
              accent ? "text-accent-ink" : "text-ink",
            )}
          >
            {value}
          </div>
        </div>
        {spark ? (
          <div className="max-w-full shrink-0">
            <Sparkline data={spark} color={sparkColor} />
          </div>
        ) : null}
      </div>
      {/* the chart drops under the number when the card is too narrow for both, never over it */}
      {/* full card width, so it never wraps into a thin column beside the chart */}
      {sub ? <div className="mt-1 text-xs text-muted-ink">{translate(sub)}</div> : null}

      {progress !== undefined ? (
        <div className="mt-3">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-subtle">
            <div
              className="h-full rounded-full bg-accent-fill"
              style={{ width: `${Math.max(0, Math.min(100, progress))}%` }}
            />
          </div>
          {progressLabel ? (
            <div className="mt-1 text-xs text-muted-ink">{progressLabel}</div>
          ) : null}
        </div>
      ) : null}

      {delta ? (
        <div
          className={cn(
            "mt-3 flex items-center gap-1 text-xs font-medium",
            down ? "text-danger-text" : "text-success-text",
          )}
        >
          {down ? (
            <ArrowDownRight className="size-3.5" />
          ) : (
            <ArrowUpRight className="size-3.5" />
          )}
          <span className="text-muted-ink">{delta.value}</span>
        </div>
      ) : null}
    </>
  )

  const shell = cn(
    "flex min-w-0 flex-col rounded-xl border border-line bg-surface p-4",
    to && "transition-colors hover:border-accent-line-strong hover:bg-accent-subtle/30",
    className,
  )

  if (to) {
    return (
      <Link to={to} className={shell}>
        {inner}
      </Link>
    )
  }
  return <div className={shell}>{inner}</div>
}
