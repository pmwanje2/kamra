import { Popin, ResponsiveList } from "../components/ui/responsive-list"
import { useCallback, useEffect, useState } from "react"
import { useNavigate, useOutletContext, useSearchParams } from "react-router-dom"
import { ChevronLeft, ChevronRight, LogIn, LogOut, Receipt, Users } from "lucide-react"

import { getSnapshot, reservationDetail, type ReservationRow, type Snapshot } from "../lib/api"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import { Avatar } from "../components/ui/avatar"
import CheckInDialog from "../components/CheckInDialog"
import CheckOutDialog from "../components/CheckOutDialog"
import type { ShellContext } from "../AppShell"
import { cn, roomLabel, formatDate } from "../lib/utils"
import { shiftDate } from "../lib/stay"
import { fmtMoney } from "../lib/money"
import { serverError } from "../lib/resource"
import { useT, t as translate } from "../lib/i18n"

/** Everyone at the property on a day, in one place: who is due in, who is
 *  in house and who is due out, each with the desk's actions. */

type Tab = "arrivals" | "in-house" | "departures" | "overdue" | "checked-out"
const TABS: { id: Tab; label: string; icon: typeof LogIn }[] = [
  { id: "arrivals", label: "Arrivals", icon: LogIn },
  { id: "in-house", label: "In house", icon: Users },
  { id: "departures", label: "Due today", icon: LogOut },
  { id: "overdue", label: "Overdue", icon: LogOut },
  { id: "checked-out", label: "Checked out", icon: LogOut },
]

// "" for a bad ?date= in the URL rather than an Invalid Date crash
const shift = shiftDate
const money = (n: number) => fmtMoney(n || 0)
function Balance({ row }: { row: ReservationRow }) {
  const { t } = useT()
  const due = Number(row.balance_due ?? 0)
  const paid = Number(row.paid_total ?? 0)
  if (due <= 0 && paid > 0) return <Badge tone="green">{t("Paid")}</Badge>
  if (due > 0) return <span className="font-medium tabular-nums text-warning-ink">{money(due)}</span>
  return <span className="text-muted-ink">-</span>
}

export default function InHouse() {
  const { t } = useT()
  const navigate = useNavigate()
  const { refreshKey } = useOutletContext<ShellContext>()
  const [params, setParams] = useSearchParams()
  const tab = (TABS.some((x) => x.id === params.get("tab")) ? params.get("tab") : "arrivals") as Tab
  const [snap, setSnap] = useState<Snapshot | null>(null)
  const today = snap?.business_date || ""
  const requestedDate = params.get("date") || undefined
  const date = requestedDate || today
  // expected arrivals over a range: date..until (one day when not set)
  const until = tab === "arrivals" && (params.get("until") ?? "") > date ? params.get("until")! : ""
  const [rangeArrivals, setRangeArrivals] = useState<ReservationRow[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [checkingIn, setCheckingIn] = useState<string | null>(null)
  const [checkingOut, setCheckingOut] = useState<string | null>(null)

  const load = useCallback(() => {
    getSnapshot(requestedDate)
      .then((s) => {
        setSnap(s)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [requestedDate])
  useEffect(load, [load, refreshKey])

  // ponytail: one snapshot per day, capped at 14 days; add a range endpoint
  // if desks want longer lists
  useEffect(() => {
    if (!until) { setRangeArrivals(null); return }
    const days: string[] = []
    for (let d = shift(date, 1); d && d <= until && days.length < 13; d = shift(d, 1)) days.push(d)
    let live = true
    Promise.all(days.map((d) => getSnapshot(d)))
      .then((snaps) => { if (live) setRangeArrivals(snaps.flatMap((s) => s.arrivals ?? [])) })
      .catch((e) => setError(serverError(e)))
    return () => { live = false }
  }, [date, until, refreshKey])

  const setQuery = (next: { tab?: Tab; date?: string; until?: string }) => {
    const p = new URLSearchParams(params)
    if (next.tab) p.set("tab", next.tab)
    if (next.until !== undefined) {
      if (next.until) p.set("until", next.until)
      else p.delete("until")
    }
    if (next.date !== undefined) {
      if (next.date === today) p.delete("date")
      else p.set("date", next.date)
    }
    setParams(p, { replace: true })
  }

  const rows: Record<Tab, ReservationRow[]> = {
    arrivals: [...(snap?.arrivals ?? []), ...(rangeArrivals ?? [])],
    "in-house": snap?.in_house ?? [],
    departures: snap?.departures_due ?? [],
    overdue: snap?.departures_overdue ?? [],
    "checked-out": snap?.checked_out ?? [],
  }
  const shown = rows[tab]

  async function openFolio(name: string) {
    try {
      const d = await reservationDetail(name)
      if (d.folio_name) navigate(`/billing/${encodeURIComponent(d.folio_name)}`)
      else navigate(`/grc/${encodeURIComponent(name)}`)
    } catch (e) {
      setError(serverError(e))
    }
  }

  const prettyDate = formatDate(date)

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink">{t("In House")}</h1>
          <p className="text-xs text-muted-ink">{t("Arrivals, guests in house and departures, in one list.")}</p>
        </div>
        <div className="flex max-w-full flex-wrap items-center gap-1 rounded-lg border border-line bg-surface p-0.5 text-sm">
          <button
            aria-label={t("Previous day")}
            className="rounded-md p-1.5 text-muted-ink hover:bg-subtle"
            onClick={() => setQuery({ date: shift(date, -1) })}
          >
            <ChevronLeft className="size-4" />
          </button>
          <button
            className={cn("rounded-md px-2.5 py-1 font-medium", date === today ? "text-accent-ink" : "text-secondary-ink")}
            onClick={() => setQuery({ date: today })}
            title={t("Back to today")}
          >
            {prettyDate}
          </button>
          <button
            aria-label={t("Next day")}
            className="rounded-md p-1.5 text-muted-ink hover:bg-subtle"
            onClick={() => setQuery({ date: shift(date, 1) })}
          >
            <ChevronRight className="size-4" />
          </button>
          {tab === "arrivals" && (
            <label className="flex items-center gap-1 border-s border-line ps-2 text-xs text-muted-ink">
              {t("Until")}
              <input type="date" className="rounded-md px-1 py-1 text-sm text-secondary-ink" value={until}
                min={shift(date, 1)} max={shift(date, 13)}
                onChange={(e) => setQuery({ until: e.target.value })} />
            </label>
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-2 sm:grid-cols-5" role="tablist">
        {TABS.map((x) => (
          <button
            key={x.id}
            role="tab"
            aria-selected={tab === x.id}
            onClick={() => setQuery({ tab: x.id })}
            className={cn(
              "flex flex-col-reverse items-start gap-0.5 rounded-xl border px-3 py-2 text-start transition-colors sm:flex-row sm:items-center sm:justify-between sm:gap-2 sm:py-2.5",
              tab === x.id
                ? "border-accent-control bg-accent-subtle text-accent-ink"
                : "border-line bg-surface text-secondary-ink hover:border-control",
            )}
          >
            <span className="flex min-w-0 max-w-full items-center gap-1.5 text-xs font-medium sm:gap-2 sm:text-sm">
              <x.icon className="hidden size-4 shrink-0 sm:block" aria-hidden />
              <span className="truncate">{t(x.label)}</span>
            </span>
            <span className="text-lg font-semibold tabular-nums">{rows[x.id].length}</span>
          </button>
        ))}
      </div>

      {error && (
        <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
          {translate(error)}
        </div>
      )}

      <div className="overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
        {shown.length === 0 ? (
          <p className="px-4 py-10 text-center text-sm text-muted-ink">
            {tab === "arrivals"
              ? t("No arrivals on this day.")
              : tab === "departures"
                ? t("No departures on this day.")
                : tab === "overdue" ? t("No overdue stays.")
                : tab === "checked-out" ? t("No completed checkouts.")
                : t("Nobody is in house.")}
          </p>
        ) : (
          <div className="overflow-x-auto">
            <ResponsiveList className="w-full text-sm">
              <thead>
                <tr className="border-b border-line-subtle text-left text-[11px] font-medium uppercase tracking-wider text-muted-ink">
                  <th className="px-4 py-2.5">{t("Guest")}</th>
                  <th className="px-3 py-2.5">{t("Room")}</th>
                  <th className="hidden px-3 py-2.5 sm:table-cell">{t("Stay")}</th>
                  <th className="hidden px-3 py-2.5 md:table-cell">{t("Source")}</th>
                  <th className="px-3 py-2.5 text-right">{t("Due")}</th>
                  <th className="px-4 py-2.5 text-right">{t("Actions")}</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line-subtle">
                {shown.map((row) => (
                  <tr
                    key={row.name}
                    className="cursor-pointer hover:bg-canvas"
                    onClick={() => navigate(`/grc/${encodeURIComponent(row.name)}`)}
                  >
                    <Popin label={t("Guest")} importance="high" className="px-4 py-2.5">
                      <div className="flex items-center gap-2.5">
                        <Avatar name={row.guest_name} />
                        <div className="min-w-0">
                          <div className="truncate font-medium text-ink">{row.guest_name}</div>
                          {tab === "arrivals" && row.pre_checked_in &&
                            <Badge tone="green">{t("Pre-checked-in")}</Badge>}
                          <div className="text-xs text-muted-ink">
                            {t("{n} ad", { n: row.adults })}
                            {row.children ? translate(" + {v0} ch", { v0: String(row.children) }) : ""}
                            {row.eta && translate(" · ETA {v0}", { v0: String(row.eta) })}
                          </div>
                        </div>
                      </div>
                    </Popin>
                    <Popin label={t("Room")} importance="high" className="px-3 py-2.5 tabular-nums">
                      {row.room ? <span>{roomLabel(row)}</span> : <span className="text-muted-ink">{t("Unassigned")}</span>}
                    </Popin>
                    <Popin label={t("Stay")} importance="medium" className="hidden px-3 py-2.5 text-secondary-ink sm:table-cell">
                      {formatDate(row.check_in_date)} - {formatDate(row.check_out_date)}
                      <span className="text-muted-ink"> · {t("{n} night{s}", { n: row.nights, s: row.nights === 1 ? "" : "s" })}</span>
                    </Popin>
                    <Popin label={t("Source")} importance="low" className="hidden px-3 py-2.5 md:table-cell">
                      <Badge tone={row.source === "OTA" ? "indigo" : row.source === "AI Agent" ? "brand" : "zinc"}>
                        {row.source === "OTA" ? row.channel || t("OTA") : t(row.source)}
                      </Badge>
                    </Popin>
                    <Popin label={t("Due")} importance="medium" className="px-3 py-2.5 text-right">
                      <Balance row={row} />
                    </Popin>
                    <Popin label={t("Actions")} importance="low" className="px-4 py-2.5" onClick={(e) => e.stopPropagation()}>
                      <div className="flex justify-end gap-1.5">
                        {tab === "arrivals" && date === today && (
                          <Button className="px-2.5 py-1 text-xs" onClick={() => setCheckingIn(row.name)}>
                            {t("Check in")}
                          </Button>
                        )}
                        {tab !== "arrivals" && tab !== "checked-out" && (
                          <Button
                            variant={tab === "departures" ? "primary" : "outline"}
                            className="px-2.5 py-1 text-xs"
                            onClick={() => setCheckingOut(row.name)}
                          >
                            {t("Check out")}
                          </Button>
                        )}
                        {tab !== "arrivals" && (
                          <Button
                            variant="ghost"
                            className="px-2 py-1 text-xs"
                            title={t("Open folio")}
                            aria-label={t("Open folio")}
                            onClick={() => void openFolio(row.name)}
                          >
                            <Receipt className="size-3.5" aria-hidden />
                          </Button>
                        )}
                      </div>
                    </Popin>
                  </tr>
                ))}
              </tbody>
            </ResponsiveList>
          </div>
        )}
      </div>

      {checkingIn && (
        <CheckInDialog
          reservation={checkingIn}
          onClose={() => setCheckingIn(null)}
          onDone={() => {
            setCheckingIn(null)
            load()
          }}
        />
      )}
      {checkingOut && (
        <CheckOutDialog
          reservation={checkingOut}
          onClose={() => setCheckingOut(null)}
          onDone={() => {
            setCheckingOut(null)
            load()
          }}
          onOpenReservation={(name) => setCheckingOut(name)}
        />
      )}
    </div>
  )
}
