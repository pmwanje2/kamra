import { useEffect, useId, useRef, useState, type FormEvent } from "react"
import { X } from "lucide-react"
import { call, frappeFetch } from "../lib/api"
import { useCashierAuth } from "../lib/cashierAuth"
import { useT } from "../lib/i18n"
import { serverError } from "../lib/resource"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"

type PinStatus = {
  has_pin: boolean
  must_reset: boolean
  locked: boolean
  locked_until: string | null
}

async function getPinStatus(signal?: AbortSignal) {
  const { message } = await frappeFetch<{ message: PinStatus }>("/api/method/kamra.api.my_pin_status", { signal })
  return message
}

const inputClass = "min-h-11 w-full rounded-lg border border-zinc-400 bg-white px-3 text-zinc-950 dark:border-zinc-500 dark:bg-zinc-900 dark:text-zinc-50"

export function MyApprovalPinModal({ onClose, onSaved }: { onClose: () => void; onSaved?: () => void }) {
  const { t } = useT()
  const { refresh } = useCashierAuth()
  const dialog = useRef<HTMLDialogElement>(null)
  const title = useId()
  const [status, setStatus] = useState<PinStatus>()
  const [password, setPassword] = useState("")
  const [pin, setPin] = useState("")
  const [confirmPin, setConfirmPin] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    const element = dialog.current
    element?.showModal()
    const controller = new AbortController()
    getPinStatus(controller.signal).then(setStatus).catch((err) => {
      if (!controller.signal.aborted) setError(serverError(err))
    })
    return () => {
      controller.abort()
      element?.close()
    }
  }, [])

  function close() {
    dialog.current?.close()
    onClose()
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (busy || !status) return
    setSaved(false)
    if (!/^[0-9]{4,8}$/.test(pin)) {
      setError(t("The PIN must be 4 to 8 digits."))
      return
    }
    if (pin !== confirmPin) {
      setError(t("PINs do not match."))
      return
    }
    setBusy(true)
    setError("")
    try {
      await call("kamra.api.set_cashier_pin", { pin, password })
      setStatus({ has_pin: true, must_reset: false, locked: false, locked_until: null })
      setPin("")
      setConfirmPin("")
      setSaved(true)
      onSaved?.()
      void refresh()
    } catch (err) {
      setError(serverError(err))
      void getPinStatus().then(setStatus).catch(() => {})
    } finally {
      setPassword("")
      setBusy(false)
    }
  }

  return (
    <dialog
      ref={dialog}
      aria-labelledby={title}
      onCancel={(event) => {
        event.preventDefault()
        if (!busy) close()
      }}
      className="m-auto max-h-[calc(100dvh-2rem)] w-[calc(100%-2rem)] max-w-sm overflow-y-auto rounded-2xl border border-zinc-200 bg-white p-5 text-zinc-900 shadow-xl backdrop:bg-zinc-950/65 dark:border-zinc-700 dark:bg-zinc-900 dark:text-zinc-50"
    >
      <div className="mb-4 flex items-start justify-between gap-3">
        <div>
          <h2 id={title} className="text-lg font-semibold">{t("My Approval PIN")}</h2>
          <div className="mt-1" aria-live="polite">
            {!status ? (!error && <span className="text-sm">{t("Loading…")}</span>)
              : status.locked ? <Badge tone="rose">{t("Locked")}</Badge>
                : status.must_reset ? <Badge tone="amber">{t("PIN reset required")}</Badge>
                  : status.has_pin ? <Badge tone="green">{t("PIN set")}</Badge>
                    : <Badge className="dark:bg-zinc-800 dark:text-zinc-200">{t("Not enrolled")}</Badge>}
          </div>
        </div>
        <button type="button" disabled={busy} onClick={close} aria-label={t("Close")}
          className="grid min-h-11 min-w-11 place-items-center rounded-lg text-zinc-700 hover:bg-zinc-100 dark:text-zinc-200 dark:hover:bg-zinc-800">
          <X className="size-5" aria-hidden />
        </button>
      </div>
      <form onSubmit={submit} className="space-y-4 text-sm">
        <label className="block space-y-1">
          <span>{t("Account password")}</span>
          <input className={inputClass} type="password" autoComplete="current-password" autoFocus required
            disabled={busy} value={password} onChange={(event) => setPassword(event.target.value)} />
        </label>
        <label className="block space-y-1">
          <span>{t("New PIN (4–8 digits)")}</span>
          <input className={inputClass} type="password" autoComplete="off" inputMode="numeric" dir="ltr"
            required minLength={4} maxLength={8} disabled={busy} value={pin}
            onChange={(event) => setPin(event.target.value.replace(/[^0-9]/g, ""))} />
        </label>
        <label className="block space-y-1">
          <span>{t("Confirm PIN")}</span>
          <input className={inputClass} type="password" autoComplete="off" inputMode="numeric" dir="ltr"
            required minLength={4} maxLength={8} disabled={busy} value={confirmPin}
            onChange={(event) => setConfirmPin(event.target.value.replace(/[^0-9]/g, ""))} />
        </label>
        {error && <p role="alert" className="text-rose-700 dark:text-rose-300">{t(error)}</p>}
        {saved && <p role="status" className="text-emerald-700 dark:text-emerald-300">{t("Approval PIN saved.")}</p>}
        <Button type="submit" className="min-h-11 w-full justify-center bg-zinc-900 hover:bg-zinc-700 dark:bg-zinc-100 dark:text-zinc-950 dark:hover:bg-zinc-300"
          disabled={busy || !status || !password || pin.length < 4 || confirmPin.length < 4}>
          {busy ? t("Saving…") : t("Save PIN")}
        </Button>
      </form>
    </dialog>
  )
}
