import type { ComponentProps } from "react"

/** Keep native table semantics and a single set of interactive controls. */
export function ResponsiveList({className = "", ...props}: ComponentProps<"table">) {
  return <table {...props} className={`responsive-list ${className}`} />
}
export function Popin({label, importance = "medium", children, ...props}: ComponentProps<"td"> & {
  label: string; importance?: "high" | "medium" | "low"
}) {
  return <td {...props} data-importance={importance}><span aria-hidden="true" className="popin-label">{label}</span>{children}</td>
}
