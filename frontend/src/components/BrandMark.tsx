import { logo, product } from "../lib/brand"
import { cn } from "../lib/utils"

export default function BrandMark({ className, decorative, legacyLogo }: {
  className?: string
  decorative?: boolean
  legacyLogo?: string | null
}) {
  const src = legacyLogo || logo()
  const label = decorative ? "" : product()
  if (src) return <img src={src} alt={label} className={cn("object-contain", className)} />
  const initials = product().split(/\s+/).slice(0, 2).map((word) => Array.from(word)[0]).join("").toUpperCase()
  return <span role={decorative ? undefined : "img"} aria-label={label || undefined}
    aria-hidden={decorative} className={cn("inline-grid shrink-0 place-items-center rounded-lg bg-accent-muted font-semibold text-accent-ink", className)}>
    {initials}
  </span>
}
