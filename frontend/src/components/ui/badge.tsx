import { cn } from "../../lib/utils"
import type { HTMLAttributes } from "react"

type Tone = "green" | "amber" | "orange" | "rose" | "sky" | "zinc" | "indigo" | "brand"

const tones: Record<Tone, string> = {
  green: "bg-success-subtle text-success-ink ring-emerald-600/20",
  amber: "bg-warning-subtle text-warning-ink ring-amber-600/20",
  orange: "bg-notice-subtle text-notice-ink ring-orange-600/20",
  rose: "bg-danger-subtle text-danger-ink ring-rose-600/20",
  sky: "bg-info-subtle text-info-ink ring-sky-600/20",
  indigo: "bg-indigo-subtle text-indigo-ink ring-indigo-600/20",
  zinc: "bg-subtle text-secondary-ink ring-control-strong/20",
  brand: "bg-accent-subtle text-accent-ink ring-accent-control/20",
}

export function Badge({
  className,
  tone = "zinc",
  ...props
}: HTMLAttributes<HTMLSpanElement> & { tone?: Tone }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset",
        tones[tone],
        className,
      )}
      {...props}
    />
  )
}
