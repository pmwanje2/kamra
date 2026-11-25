import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Users } from "lucide-react"
import { call, getCurrentProperty, type ShareSummary } from "../lib/api"
import { useT } from "../lib/i18n"
import { fmtMoney, moneyDigits } from "../lib/money"
import { serverError } from "../lib/resource"
import { Button } from "./ui/button"

const modes = ["Split Equal", "Primary Pays", "Full Rate Each", "Custom"] as const
type Mode = typeof modes[number]
type Action = "add" | "combine" | "break"
type Choice = { name: string; label: string }

export type { ShareSummary } from "../lib/api"

const input = "min-h-10 w-full min-w-0 rounded-lg border border-control bg-surface px-2.5 py-1.5 text-ink"

export default function ShareReservations({ reservation, summary, reload, property, roomType, room, checkIn, checkOut }: {
  reservation: string
  summary: ShareSummary
  reload: () => void
  property?: string
  roomType?: string | null
  room?: string | null
  checkIn?: string
  checkOut?: string
}) {
  const { t } = useT()
  const [mode, setMode] = useState(summary.mode)
  const [rates, setRates] = useState<Record<string, string>>({})
  const [action, setAction] = useState<Action | null>(null)
  const [query, setQuery] = useState("")
  const [choices, setChoices] = useState<Choice[]>([])
  const [selection, setSelection] = useState("")
  const [arrive, setArrive] = useState(checkIn || "")
  const [depart, setDepart] = useState(checkOut || "")
  const [rate, setRate] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [searchError, setSearchError] = useState("")
  const digits = moneyDigits(summary.currency, summary.decimals)
  const step = 10 ** -digits
  const current = summary.members.find((m) => m.reservation === reservation)

  useEffect(() => {
    setMode(summary.mode)
    setRates(Object.fromEntries(summary.members.map((m) => [m.reservation, String(m.custom_rate)])))
  }, [summary])

  useEffect(() => {
    if (!action || (action !== "break" && query.trim().length < 2)) return
    let active = true
    const prop = property || getCurrentProperty()
    const timer = window.setTimeout(async () => {
      try {
        let rows: Choice[]
        if (action === "add") {
          const guests = await call<{ name: string; full_name: string }[]>("kamra.api.guest_search", { q: query.trim() })
          rows = guests.map((g) => ({ name: g.name, label: g.full_name }))
        } else if (action === "combine") {
          const bookings = await call<{ name: string; guest_name: string; room_type: string }[]>(
            "kamra.api.find_reservations", { property: prop, query: query.trim(), status: "Confirmed" })
          const members = new Set([reservation, ...summary.members.map((m) => m.reservation)])
          rows = bookings.filter((b) => !members.has(b.name) && b.room_type === roomType)
            .map((b) => ({ name: b.name, label: `${b.guest_name} · ${b.name}` }))
        } else {
          const rooms = await call<{ name: string; room_number: string }[]>("kamra.api.available_rooms", {
            property: prop, room_type: roomType, check_in_date: checkIn || current?.check_in,
            check_out_date: checkOut || current?.check_out, for_assignment: 1,
          })
          rows = rooms.filter((r) => r.name !== room).map((r) => ({ name: r.name, label: r.room_number }))
        }
        if (active) { setChoices(rows); setSearchError("") }
      } catch (err) {
        if (active) setSearchError(serverError(err))
      }
    }, action === "break" ? 0 : 250)
    return () => { active = false; window.clearTimeout(timer) }
  }, [action, query, property, roomType, room, reservation, checkIn, checkOut, current, summary.members])

  function open(next: Action | null) {
    setAction(next)
    setQuery("")
    setChoices([])
    setSelection("")
    setRate("")
    setArrive(checkIn || "")
    setDepart(checkOut || "")
    setError("")
    setSearchError("")
  }

  async function run(method: string, params: Record<string, unknown>) {
    setBusy(true)
    setError("")
    try {
      await call(method, { reservation, ...params })
      open(null)
      reload()
    } catch (err) {
      setError(serverError(err))
    } finally {
      setBusy(false)
    }
  }

  function changeMembership() {
    if (action === "add") {
      const guest = selection ? { guest: selection } : { guest_name: query.trim() }
      return run("kamra.shares.create_share_reservation", {
        ...guest, check_in_date: arrive, check_out_date: depart,
        ...(summary.mode === "Custom" ? { custom_rate: Number(rate) } : {}),
      })
    }
    if (action === "combine") {
      return run("kamra.shares.combine_into_share", {
        other_reservation: selection,
        ...(summary.mode === "Custom" ? { custom_rate: Number(rate) } : {}),
      })
    }
    return run("kamra.shares.break_share", { new_room: selection })
  }

  if (!summary.can_manage && !summary.group_id) return null
  const labels = {
    add: t("Add Sharer"),
    combine: t("Combine Existing Reservation"),
    break: t("Break Share"),
  }
  const modeLabels: Record<Mode, string> = {
    "Split Equal": t("Split Equal"), "Primary Pays": t("Primary Pays"),
    "Full Rate Each": t("Full Rate Each"), "Custom": t("Custom"),
  }

  return (
    <section aria-label={t("Share Reservations")} className="space-y-3 rounded-xl border border-line bg-surface p-4 text-start text-ink">
      <h2 className="flex items-center gap-2 text-sm font-semibold text-ink">
        <Users className="size-4" aria-hidden />{t("Share Reservations")}
      </h2>
      {summary.members.length > 0 && <ul className="divide-y divide-line text-sm">
        {summary.members.map((member) => <li key={member.reservation} className="flex flex-wrap items-center justify-between gap-2 py-2">
          <div className="min-w-0">
            <p className="break-words font-medium text-ink">
              {member.guest_name}
              {member.primary && <span className="ms-2 text-xs text-muted-ink">{t("Primary Sharer")}</span>}
            </p>
            <p className="text-xs text-muted-ink">
              <bdi>{member.check_in} – {member.check_out}</bdi>
              {summary.mode === "Custom" && <bdi className="ms-2">{fmtMoney(member.custom_rate, summary.currency, digits)}</bdi>}
            </p>
          </div>
          {member.folio && <Link className="text-accent-ink underline underline-offset-2"
            to={`/billing/${encodeURIComponent(member.folio)}`}>{t("Open Folio")}</Link>}
        </li>)}
      </ul>}
      {summary.can_manage && <>
        <div className="flex flex-wrap gap-2">
          <Button type="button" variant="outline" disabled={busy} onClick={() => open("add")}>{labels.add}</Button>
          <Button type="button" variant="outline" disabled={busy} onClick={() => open("combine")}>{labels.combine}</Button>
          {summary.group_id && <Button type="button" variant="outline" disabled={busy}
            onClick={() => open("break")}>{labels.break}</Button>}
        </div>
        {action && <form className="grid gap-2 rounded-lg border border-line p-3 text-sm"
          onSubmit={(event) => { event.preventDefault(); void changeMembership() }}>
          {action !== "break" && <label className="grid gap-1">
            {action === "add" ? t("Search guests or enter a new name") : t("Search reservations")}
            <input required className={input} value={query} onChange={(event) => {
              setQuery(event.target.value); setSelection(""); setChoices([])
            }} />
          </label>}
          {(action !== "add" || choices.length > 0) && <label className="grid gap-1">
            {action === "break" ? t("Destination Room") : action === "add" ? t("Guest profile") : t("Reservation")}
            <select className={input} value={selection} required={action !== "add"}
              onChange={(event) => setSelection(event.target.value)}>
              <option value="">{action === "add" ? t("Create new guest profile") : t("Select")}</option>
              {choices.map((item) => <option key={item.name} value={item.name}>{item.label}</option>)}
            </select>
          </label>}
          {action === "add" && <div className="grid gap-2 sm:grid-cols-2">
            <label className="grid gap-1">{t("Check-in date")}
              <input className={input} type="date" required value={arrive} onChange={(event) => setArrive(event.target.value)} />
            </label>
            <label className="grid gap-1">{t("Check-out date")}
              <input className={input} type="date" required min={arrive} value={depart} onChange={(event) => setDepart(event.target.value)} />
            </label>
          </div>}
          {action !== "break" && summary.mode === "Custom" && <label className="grid gap-1">
            {t("Custom Nightly Rate")} ({summary.currency})
            <input className={input} type="number" min="0" step={step} value={rate} required
              onChange={(event) => setRate(event.target.value)} />
          </label>}
          {searchError && <p role="alert" className="text-danger-text">{t(searchError)}</p>}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" disabled={busy}>{t("Confirm")}</Button>
            <Button type="button" variant="ghost" disabled={busy} onClick={() => open(null)}>{t("Cancel")}</Button>
          </div>
        </form>}
        {summary.group_id && <form className="grid gap-2 text-sm" onSubmit={(event) => {
          event.preventDefault()
          void run("kamra.shares.update_share_rate_mode", {
            mode, ...(mode === "Custom" ? { custom_rates: rates } : {}),
          })
        }}>
          <label className="grid gap-1">{t("Rate Split Mode")}
            <select aria-label={t("Rate Split Mode")} className={input} value={mode}
              onChange={(event) => setMode(event.target.value as Mode)}>
              {modes.map((key) => <option key={key} value={key}>{modeLabels[key]}</option>)}
            </select>
          </label>
          {mode === "Custom" && <div className="grid gap-2 sm:grid-cols-2">
            {summary.members.map((member) => <label key={member.reservation} className="grid gap-1">
              {member.guest_name} ({summary.currency})
              <input className={input} type="number" min="0" step={step} required
                value={rates[member.reservation] ?? ""}
                onChange={(event) => setRates((previous) => ({ ...previous, [member.reservation]: event.target.value }))} />
            </label>)}
          </div>}
          <Button type="submit" className="justify-self-start" disabled={busy}>{t("Save Rate Split")}</Button>
        </form>}
      </>}
      {error && <p role="alert" className="text-sm text-danger-text">{t(error)}</p>}
    </section>
  )
}
