import { cn } from "../../lib/utils"

// Soft, legible tints - initials on a colour picked deterministically from
// the name, so the same guest always reads the same.
const PALETTE = [
  "bg-accent-subtle text-accent-ink",
  "bg-info-subtle text-info-ink",
  "bg-warning-subtle text-warning-ink",
  "bg-danger-subtle text-danger-ink",
  "bg-indigo-subtle text-indigo-ink",
  "bg-success-subtle text-success-ink",
]

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (!parts.length) return "?"
  const first = parts[0][0] ?? ""
  const last = parts.length > 1 ? parts[parts.length - 1][0] : ""
  return (first + last).toUpperCase()
}

function pick(name: string) {
  let h = 0
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) | 0
  return PALETTE[Math.abs(h) % PALETTE.length]
}

export function Avatar({
  name,
  className,
}: {
  name: string
  className?: string
}) {
  return (
    <span
      className={cn(
        "grid size-8 shrink-0 place-items-center rounded-full text-[11px] font-semibold",
        pick(name),
        className,
      )}
      aria-hidden
    >
      {initials(name)}
    </span>
  )
}
