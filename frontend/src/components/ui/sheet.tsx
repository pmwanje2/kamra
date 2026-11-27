import { useEffect } from "react"
import { X } from "lucide-react"
import { Button } from "./button"
import { t as translate, useUILanguage } from "../../lib/i18n"

/**
 * Inline-end slide-over drawer for create/edit forms.
 * Content scrolls; header and footer stay pinned.
 */
export function Sheet(props: {
  title: string
  description?: string
  onClose: () => void
  footer?: React.ReactNode
  children: React.ReactNode
  /** Wide surface (~2/3 screen) for rich detail panels. */
  wide?: boolean
}) {
  useUILanguage()
  const { onClose } = props
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose()
    }
    document.addEventListener("keydown", onKey)
    document.body.style.overflow = "hidden"
    return () => {
      document.removeEventListener("keydown", onKey)
      document.body.style.overflow = ""
    }
  }, [onClose])

  return (
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true">
      <div
        className="absolute inset-0 bg-black/40 animate-fade-in"
        onClick={props.onClose}
        aria-hidden
      />
      <div
        className={
          props.wide
            ? "absolute inset-y-0 end-0 flex w-full flex-col bg-surface shadow-2xl animate-sheet-in md:w-2/3"
            : "absolute inset-y-0 end-0 flex w-full max-w-md flex-col bg-surface shadow-2xl animate-sheet-in"
        }
      >
        <div className="flex items-start justify-between border-b border-line-subtle px-6 py-4">
          <div>
            <h2 className="text-lg font-semibold">{props.title}</h2>
            {props.description && (
              <p className="mt-0.5 text-sm text-muted-ink">
                {props.description}
              </p>
            )}
          </div>
          <Button variant="ghost" onClick={props.onClose} aria-label={translate("Close")}>
            <X className="size-5" />
          </Button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-5">{props.children}</div>

        {props.footer && (
          <div className="border-t border-line-subtle bg-canvas px-6 py-4">
            {props.footer}
          </div>
        )}
      </div>
    </div>
  )
}
