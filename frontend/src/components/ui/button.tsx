import { cn } from "../../lib/utils"
import type { ButtonHTMLAttributes } from "react"

type Variant = "primary" | "outline" | "ghost"

const variants: Record<Variant, string> = {
  primary:
    "bg-primary-button text-on-primary hover:bg-primary-button-hover focus-visible:outline-brand-600 " +
    "disabled:bg-primary-button-disabled disabled:opacity-100",
  outline:
    "border border-zinc-300 bg-white text-zinc-800 hover:bg-zinc-100 focus-visible:outline-zinc-400 disabled:opacity-50",
  ghost: "text-zinc-600 hover:bg-zinc-100 focus-visible:outline-zinc-400 disabled:opacity-50",
}

export function Button({
  className,
  variant = "primary",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      className={cn(
        "inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium",
        "transition-colors focus-visible:outline-2 focus-visible:outline-offset-2",
        "disabled:pointer-events-none",
        variants[variant],
        className,
      )}
      {...props}
    />
  )
}
