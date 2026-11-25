import { useEffect, useId, useRef, useState, type KeyboardEvent } from "react"
import { Delete, X } from "lucide-react"
import { cn } from "../lib/utils"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"

type PinPadProps = {
  open: boolean
  title?: string
  subtitle?: string
  mode?: "unlock" | "enroll" | "confirm"
  submitLabel?: string
  error?: string | null
  busy?: boolean
  onSubmit: (pin: string) => void | Promise<void>
  onClose?: () => void
  maxLength?: number
}

const digits = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "", "0", "⌫"]

export function PinPad({
  open,
  title,
  subtitle,
  mode = "unlock",
  submitLabel,
  error,
  busy,
  onSubmit,
  onClose,
  maxLength = 8,
}: PinPadProps) {
  const { t } = useT()
  const dialog = useRef<HTMLDialogElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)
  const titleId = useId()
  const subtitleId = useId()
  const [pin, setPin] = useState("")

  useEffect(() => {
    const element = dialog.current
    if (!open || !element) return
    element.showModal()
    heading.current?.focus()
    return () => element.close()
  }, [open])
  useEffect(() => setPin(""), [open, mode])

  function push(digit: string) {
    if (!busy) setPin((value) => value.length < maxLength ? value + digit : value)
  }

  function erase() {
    if (!busy) setPin((value) => value.slice(0, -1))
  }

  function submit() {
    if (pin.length < 4 || busy) return
    setPin("")
    void onSubmit(pin)
  }

  function onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.ctrlKey || event.altKey || event.metaKey) return
    if (/^[0-9]$/.test(event.key)) push(event.key)
    else if (event.key === "Backspace") erase()
    else if (event.key === "Enter" && !(event.target instanceof HTMLButtonElement)) submit()
    else return
    event.preventDefault()
  }

  if (!open) return null

  return (
    <dialog
      ref={dialog}
      aria-labelledby={titleId}
      aria-describedby={subtitleId}
      className="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-sm overflow-y-auto rounded-2xl border border-zinc-200 bg-white p-5 text-zinc-900 shadow-xl backdrop:bg-zinc-950/65 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
      onKeyDown={onKeyDown}
      onCancel={(event) => {
        event.preventDefault()
        if (!busy && onClose) {
          dialog.current?.close()
          onClose()
        }
      }}
    >
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 ref={heading} tabIndex={-1} id={titleId} className="text-lg font-semibold">{title ?? t("Cashier PIN")}</h2>
          <p id={subtitleId} className="mt-0.5 text-sm text-zinc-600 dark:text-zinc-300">{subtitle ?? t("Enter your PIN to continue")}</p>
        </div>
        {onClose && (
          <button type="button" disabled={busy} aria-label={t("Close")} onClick={() => {
            dialog.current?.close()
            onClose()
          }}
            className="grid min-h-11 min-w-11 place-items-center rounded-lg text-zinc-700 hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-800">
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>

      <div
        dir="ltr"
        role="status"
        aria-label={t("{count} digits entered", { count: pin.length })}
        className="mb-4 flex h-12 items-center justify-center gap-2 rounded-xl bg-zinc-100 dark:bg-zinc-800"
      >
        {Array.from({ length: Math.max(4, pin.length) }, (_, i) => (
          <span key={i} aria-hidden className={cn("size-2.5 rounded-full",
            i < pin.length ? "bg-zinc-900 dark:bg-zinc-100" : "bg-zinc-500 dark:bg-zinc-400",
          )} />
        ))}
      </div>

      {error && <p role="alert" className="mb-3 text-center text-sm text-rose-700 dark:text-rose-300">{t(error)}</p>}

      <div dir="ltr" className="grid grid-cols-3 gap-2">
        {digits.map((digit) => digit === "" ? <div key={digit} /> : (
          <button key={digit} type="button" disabled={busy}
            aria-label={digit === "⌫" ? t("Delete last digit") : undefined}
            className="flex h-14 items-center justify-center rounded-xl bg-zinc-100 text-xl font-semibold text-zinc-900 hover:bg-zinc-200 active:bg-zinc-300 disabled:opacity-50 dark:bg-zinc-800 dark:text-zinc-50 dark:hover:bg-zinc-700 dark:active:bg-zinc-600"
            onClick={() => digit === "⌫" ? erase() : push(digit)}>
            {digit === "⌫" ? <Delete className="size-5" aria-hidden /> : t(digit)}
          </button>
        ))}
      </div>

      <Button type="button"
        className="mt-4 min-h-11 w-full justify-center bg-zinc-900 hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-950 dark:hover:bg-zinc-300"
        disabled={busy || pin.length < 4} onClick={submit}>
        {busy ? t("Checking…") : submitLabel ?? (mode === "enroll" ? t("Set PIN")
          : mode === "confirm" ? t("Confirm") : t("Unlock"))}
      </Button>
    </dialog>
  )
}
