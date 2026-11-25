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
  requirePassword?: boolean
  onSubmit: (pin: string, currentPassword?: string) => void | Promise<void>
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
  requirePassword = false,
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
  const [password, setPassword] = useState("")

  useEffect(() => {
    const element = dialog.current
    if (!open || !element) return
    element.showModal()
    heading.current?.focus()
    return () => element.close()
  }, [open])
  useEffect(() => {
    setPin("")
    setPassword("")
  }, [open, mode])

  function push(digit: string) {
    if (!busy) setPin((value) => value.length < maxLength ? value + digit : value)
  }

  function erase() {
    if (!busy) setPin((value) => value.slice(0, -1))
  }

  function submit() {
    if (pin.length < 4 || busy || (requirePassword && !password)) return
    setPin("")
    setPassword("")
    if (requirePassword) void onSubmit(pin, password)
    else void onSubmit(pin)
  }

  function onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.ctrlKey || event.altKey || event.metaKey || event.target instanceof HTMLInputElement) return
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
      className="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-sm overflow-y-auto rounded-2xl border border-line bg-surface p-5 text-ink shadow-xl backdrop:bg-scrim/65"
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
          <p id={subtitleId} className="mt-0.5 text-sm text-secondary-ink">{subtitle ?? t("Enter your PIN to continue")}</p>
        </div>
        {onClose && (
          <button type="button" disabled={busy} aria-label={t("Close")} onClick={() => {
            dialog.current?.close()
            onClose()
          }}
            className="grid min-h-11 min-w-11 place-items-center rounded-lg text-secondary-ink hover:bg-subtle">
            <X className="size-4" aria-hidden />
          </button>
        )}
      </div>

      <div
        dir="ltr"
        role="status"
        aria-label={t("{count} digits entered", { count: pin.length })}
        className="mb-4 flex h-12 items-center justify-center gap-2 rounded-xl bg-subtle"
      >
        {Array.from({ length: Math.max(4, pin.length) }, (_, i) => (
          <span key={i} aria-hidden className={cn("size-2.5 rounded-full",
            i < pin.length ? "bg-accent-control" : "bg-fill-prominent",
          )} />
        ))}
      </div>

      {error && <p role="alert" className="mb-3 text-center text-sm text-danger-ink dark:text-rose-300">{t(error)}</p>}

      <div dir="ltr" className="grid grid-cols-3 gap-2">
        {digits.map((digit) => digit === "" ? <div key={digit} /> : (
          <button key={digit} type="button" disabled={busy}
            aria-label={digit === "⌫" ? t("Delete last digit") : undefined}
            className="flex h-14 items-center justify-center rounded-xl bg-subtle text-xl font-semibold text-ink hover:bg-muted active:bg-fill-strong disabled:opacity-50"
            onClick={() => digit === "⌫" ? erase() : push(digit)}>
            {digit === "⌫" ? <Delete className="size-5" aria-hidden /> : t(digit)}
          </button>
        ))}
      </div>

      {requirePassword && (
        <label className="mt-4 block space-y-1 text-sm">
          <span>{t("Account password")}</span>
          <input type="password" autoComplete="current-password" required disabled={busy} value={password}
            className="min-h-11 w-full rounded-lg border border-control-strong bg-surface px-3 text-ink"
            onChange={(event) => setPassword(event.target.value)} />
        </label>
      )}
      <Button type="button" variant="inverse"
        className="mt-4 min-h-11 w-full justify-center"
        disabled={busy || pin.length < 4 || (requirePassword && !password)} onClick={submit}>
        {busy ? t("Checking…") : submitLabel ?? (mode === "enroll" ? t("Set PIN")
          : mode === "confirm" ? t("Confirm") : t("Unlock"))}
      </Button>
    </dialog>
  )
}
