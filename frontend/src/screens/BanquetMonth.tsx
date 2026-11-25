/*  The month, at a glance.

    A banquet office gets asked one question more than any other: "do you
    have the 14th of December?" Not "what are your hours" - a hall is sold
    by the session, and the answer is yes or no for a specific hall on a
    specific evening.

    So this is that answer, for a whole month at once: halls down the side
    split into morning / afternoon / evening, days across the top, every
    cell either open or carrying its function. Colour does the reading for
    you - the eye finds the free Saturday evening before you've finished
    the sentence. Click an open cell to start the enquiry already knowing
    the hall, the date and the session. */

import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { CalendarRange, ChevronLeft, ChevronRight, Sparkles } from "lucide-react"

import {
  banquet,
  type FunctionStatus,
  type MonthAvailability,
  type MonthCell,
} from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { Empty, ErrorNote, inr, StatusPill, today } from "./banquet/shared"
import { moneyLocale, useLocale } from "../lib/money"
import { t as translate, useUILanguage, uiDateLocale } from "../lib/i18n"

const SESSIONS = ["Morning", "Afternoon", "Evening"] as const

/** How a taken cell reads at a glance. Confirmed is solid; a tentative
 *  hold is drawn softly, because it's meant to be sellable over. */
const CELL: Record<string, string> = {
  Confirmed: "bg-success-fill text-on-solid",
  Completed: "bg-inverse-muted text-on-solid",
  Tentative:
    "bg-warning-muted text-warning-ink ring-1 ring-inset ring-dashed ring-amber-400",
  Enquiry: "bg-info-muted text-info-ink ring-1 ring-inset ring-sky-300",
}

function monthShift(month: string, by: number) {
  const [y, m] = month.split("-").map(Number)
  const d = new Date(y, m - 1 + by, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
}
const monthLabel = (m: string) =>
  new Date(m + "-01T00:00:00").toLocaleDateString(uiDateLocale(moneyLocale()), {
    month: "long",
    year: "numeric",
  })
const weekend = (iso: string) => [0, 6].includes(new Date(iso + "T00:00:00").getDay())

export default function BanquetMonth() {
  useUILanguage()
  useLocale()
  const navigate = useNavigate()
  const [month, setMonth] = useState(today().slice(0, 7))
  const [data, setData] = useState<MonthAvailability | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [hover, setHover] = useState<string | null>(null)

  const load = useCallback(() => {
    setData(null)
    banquet
      .monthAvailability(month)
      .then(setData)
      .catch((e) => setError(serverError(e)))
  }, [month])
  useEffect(load, [load])

  /** The headline a salesperson actually wants: how much of the month is
   *  still sellable, and which prime slots are still open. */
  const openPrime = useMemo(() => {
    if (!data) return []
    return data.dates
      .filter(weekend)
      .flatMap((d) =>
        data.rows
          .filter((r) => r.session === "Evening" && !(r.by_date[d]?.length))
          .map((r) => ({ date: d, venue: r.venue_name, row: r })),
      )
  }, [data])

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <div>
            <CardTitle>
              <span className="inline-flex items-center gap-1.5">
                <CalendarRange className="size-4" />
                {monthLabel(month)}
              </span>
            </CardTitle>
            <p className="mt-0.5 text-xs text-muted-ink">{translate("Every hall, every session. Click an open slot to start an enquiry on it.")}</p>
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              variant="outline"
              onClick={() => setMonth(monthShift(month, -1))}
              aria-label={translate("Previous month")}
            >
              <ChevronLeft className="size-4 rtl:rotate-180" />
            </Button>
            <Button variant="outline" onClick={() => setMonth(today().slice(0, 7))}>{translate("This month")}</Button>
            <Button
              variant="outline"
              onClick={() => setMonth(monthShift(month, 1))}
              aria-label={translate("Next month")}
            >
              <ChevronRight className="size-4 rtl:rotate-180" />
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          <ErrorNote error={error} />
          {!data ? (
            <Empty>{translate("Loading the month…")}</Empty>
          ) : data.rows.length === 0 ? (
            <Empty>{translate("No halls yet - add them under Halls & Venues.")}</Empty>
          ) : (
            <>
              <div className="mb-4 flex flex-wrap items-center gap-4">
                <Gauge value={data.utilisation} />
                {openPrime.length > 0 && (
                  <p className="flex items-center gap-1.5 text-sm text-muted-ink">
                    <Sparkles className="size-4 text-warning-text" />
                    <span>
                      <span className="font-medium text-ink">
                        {openPrime.length}{translate(" weekend evening")}{openPrime.length === 1 ? "" : translate("s")}
                      </span>{" "}{translate("still open - the slots worth selling first.")}</span>
                  </p>
                )}
              </div>

              {/* table-fixed with narrow day columns: 31 days plus the two
                  sticky columns land inside a normal desktop width, so the
                  month reads in one look instead of two scrolls. */}
              <div className="-mx-1 overflow-x-auto px-1">
                <table className="w-full table-fixed border-separate border-spacing-0 text-sm">
                  <thead>
                    <tr>
                      <th className="sticky start-0 z-20 w-28 bg-surface p-2 text-start text-[11px] font-medium uppercase tracking-wider text-muted-ink">{translate("Hall")}</th>
                      <th className="sticky start-28 z-20 w-20 bg-surface p-2 text-start text-[11px] font-medium uppercase tracking-wider text-muted-ink">{translate("Session")}</th>
                      {data.dates.map((d) => (
                        <th
                          key={d}
                          className={
                            "border-b border-line px-0 pb-1.5 text-center text-[10px] font-medium " +
                            (d === today()
                              ? "bg-accent-subtle text-accent-ink"
                              : weekend(d)
                                ? "text-secondary-ink"
                                : "text-muted-ink")
                          }
                        >
                          <div className="text-[9px] uppercase">
                            {new Date(d + "T00:00:00")
                              .toLocaleDateString(uiDateLocale(moneyLocale()), { weekday: "narrow" })}
                          </div>
                          {Number(d.slice(8))}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {data.rows.map((r, i) => {
                      const firstOfVenue =
                        i === 0 || data.rows[i - 1].venue !== r.venue
                      return (
                        <tr key={r.venue + r.session}>
                          {firstOfVenue ? (
                            <td
                              rowSpan={SESSIONS.length}
                              className="sticky start-0 z-10 w-28 border-b border-line-subtle bg-surface p-2 align-top"
                            >
                              <div className="truncate font-medium" title={translate(r.venue_name)}>
                                {r.venue_name}
                              </div>
                              <div className="text-[11px] leading-tight text-muted-ink">
                                {r.capacity ? <>{r.capacity}{translate(" pax")}<br /></> : null}
                                {inr(r.base_price)}
                              </div>
                            </td>
                          ) : null}
                          <td
                            className={
                              "sticky start-28 z-10 w-20 whitespace-nowrap bg-surface px-2 py-1 text-xs text-muted-ink " +
                              (r.session === "Evening"
                                ? "border-b border-line-subtle"
                                : "")
                            }
                          >
                            {translate(r.session)}
                          </td>
                          {data.dates.map((d) => {
                            const cells = r.by_date[d] ?? []
                            const b = cells[0]
                            const key = `${r.venue}|${r.session}|${d}`
                            return (
                              <td
                                key={d}
                                className={
                                  "px-px py-0.5 " +
                                  (r.session === "Evening"
                                    ? "border-b border-line-subtle"
                                    : "") +
                                  (d === today() ? " bg-accent-subtle/40" : "")
                                }
                                onMouseEnter={() => setHover(key)}
                                onMouseLeave={() => setHover(null)}
                              >
                                <Slot
                                  cell={b}
                                  extra={cells.length - 1}
                                  showing={hover === key}
                                  weekend={weekend(d)}
                                  onOpen={() =>
                                    b
                                      ? navigate(
                                          `/banquet/${encodeURIComponent(b.name)}`,
                                        )
                                      : navigate(
                                          `/banquet?venue=${encodeURIComponent(
                                            r.venue,
                                          )}&date=${d}&session=${r.session}`,
                                        )
                                  }
                                />
                              </td>
                            )
                          })}
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>

              <div className="mt-4 flex flex-wrap items-center gap-3 text-xs text-muted-ink">
                {(Object.keys(CELL) as FunctionStatus[]).map((s) => (
                  <span key={s} className="flex items-center gap-1.5">
                    <span className={"inline-block size-3 rounded " + CELL[s]} />
                    {translate(s)}
                  </span>
                ))}
                <span className="flex items-center gap-1.5">
                  <span className="inline-block size-3 rounded border border-dashed border-control" />{translate("Open")}</span>
              </div>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

/** One hall-session-day. Free slots stay quiet until you hover them; taken
 *  ones carry their colour and reveal the booking on hover. */
function Slot({
  cell,
  extra,
  showing,
  weekend: isWeekend,
  onOpen,
}: {
  cell: MonthCell | undefined
  extra: number
  showing: boolean
  weekend: boolean
  onOpen: () => void
}) {
  useUILanguage()
  return (
    <div className="relative">
      <button
        onClick={onOpen}
        aria-label={
          (cell
            ? `${cell.customer_name}, ${cell.event_type}`
            : translate("Open - start an enquiry"))
        }
        className={
          "block h-7 w-full rounded transition-all " +
          (cell
            ? (CELL[cell.status] ?? "bg-fill-strong") + " hover:brightness-110"
            : "border border-dashed " +
              (isWeekend
                ? "border-control hover:border-accent-control hover:bg-accent-subtle"
                : "border-line hover:border-accent-control hover:bg-accent-subtle/60"))
        }
      >
        {extra > 0 && (
          <span className="text-[9px] font-semibold">+{extra + 1}</span>
        )}
      </button>
      {showing && cell && (
        <div className="pointer-events-none absolute bottom-full start-1/2 z-30 mb-1 w-52 -translate-x-1/2 rtl:translate-x-1/2 rounded-lg bg-inverse px-2.5 py-2 text-start text-xs text-on-inverse shadow-lg">
          <div className="flex items-center justify-between gap-2">
            <span className="font-semibold">{cell.customer_name}</span>
            <StatusPill status={cell.status} className="!bg-on-solid/15 !text-on-solid !ring-0" />
          </div>
          <div className="mt-0.5 text-on-solid/70">
            {cell.event_name || translate(cell.event_type)}
            {cell.pax ? translate(" · {v0} pax", { v0: String(cell.pax) }) : ""}
          </div>
          <div className="mt-1 text-on-solid/90">
            {inr(cell.grand_total)}
            {cell.balance_due > 0 && (
              <span className="text-amber-300"> · {inr(cell.balance_due)}{translate(" due")}</span>
            )}
          </div>
          {cell.spans_day && (
            <div className="mt-0.5 text-[10px] text-on-solid/50">{translate("Part of a multi-day function")}</div>
          )}
        </div>
      )}
    </div>
  )
}

/** How much of the month is sold - one number a manager reads instantly. */
function Gauge({ value }: { value: number }) {
  useUILanguage()
  return (
    <div className="flex items-center gap-3">
      <div className="relative h-2 w-40 overflow-hidden rounded-full bg-subtle">
        <div
          className={
            "h-full rounded-full transition-all " +
            (value > 70
              ? "bg-emerald-500"
              : value > 35
                ? "bg-amber-400"
                : "bg-fill-strong")
          }
          style={{ width: `${Math.min(100, value)}%` }}
        />
      </div>
      <span className="text-sm">
        <span className="font-semibold tabular-nums">{value}%</span>
        <span className="text-muted-ink">{translate(" of hall-sessions sold")}</span>
      </span>
    </div>
  )
}
