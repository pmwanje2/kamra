import { useState } from "react"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { Button } from "./ui/button"

export default function WalkReservation({ reservation, protectedGuest, currency, onDone }: {
  reservation: string; protectedGuest: boolean; currency?: string; onDone: () => void
}) {
  const { t } = useT()
  const [open, setOpen] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  if (!open) return <Button variant="outline" onClick={() => setOpen(true)}>{t("Walk reservation")}</Button>
  return <form className="grid w-full gap-3 rounded-lg border border-control p-3 sm:grid-cols-2" onSubmit={async e => {
    e.preventDefault()
    const fields = Object.fromEntries(new FormData(e.currentTarget))
    setBusy(true)
    try {
      await call("kamra.api.walk_reservation", { reservation, ...fields })
      onDone()
    } catch (err) { setError(serverError(err)) } finally { setBusy(false) }
  }}>
    {Object.entries({ relocated_property: "Relocated property", reason: "Relocation reason",
      cost: "Relocation cost", recovery_reference: "Recovery reference" }).map(([name, label]) =>
      <label key={name}>{t(label)} {name === "cost" && currency}
        <input name={name} required={name !== "recovery_reference"} defaultValue={name === "cost" ? "0" : ""}
          inputMode={name === "cost" ? "decimal" : "text"} className="block w-full rounded border border-control bg-surface p-2" />
      </label>)}
    {protectedGuest && <label className="text-warning-ink"><input required type="checkbox" name="confirm_protected" value="1" />
      {t("Confirm relocation of this protected guest.")}</label>}
    {error && <p role="alert" className="text-danger-ink">{error}</p>}
    <div className="flex flex-wrap gap-2">
      <Button type="submit" disabled={busy}>{t("Confirm relocation")}</Button>
      <Button type="button" disabled={busy} variant="outline" onClick={() => setOpen(false)}>{t("Cancel")}</Button>
    </div>
  </form>
}
