import { useCallback, useEffect, useState } from "react"
import { ChevronLeft, ChevronRight, Target } from "lucide-react"

import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { cur, fmtAmount } from "../lib/money"
import { getLang } from "../lib/dir"
import { t as translate, useUILanguage } from "../lib/i18n"

const inr = (n: number) => fmtAmount(n || 0)

interface BvaRow {
  metric: string
  key: string
  actual: number
  target: number
  variance: number
  attainment: number | null
}
interface Bva {
  period: string
  days_elapsed: number
  total_days: number
  rows: BvaRow[]
  has_budget: boolean
  /** "posted": actual = Room charges on folios (End of Day posts them) */
  basis?: string
  posted_nights?: number
  posted_through?: string | null
  /** sold room nights in the elapsed window, posted or not */
  on_books_nights?: number
  unposted_nights?: number
}
interface Contrib {
  by: string
  total: number
  rows: { label: string; bookings: number; room_nights: number; revenue: number; commission: number; share: number }[]
}

function shiftMonth(period: string, by: number) {
  const [y, m] = period.split("-").map(Number)
  const d = new Date(y, m - 1 + by, 1)
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`
}
const isPct = (k: string) => k === "occupancy_pct"
const fmt = (k: string, n: number) =>
  isPct(k) ? `${n}%` : `${cur()}${inr(n)}`

function attainTone(a: number | null) {
  if (a === null) return "text-muted-ink"
  if (a >= 100) return "text-success-text"
  if (a >= 80) return "text-warning-text"
  return "text-danger-text"
}

// ---------------------------------------------------------------------------

function BudgetVsActual() {
  useUILanguage()
  const property = getCurrentProperty()
  const [period, setPeriod] = useState(() =>
    new Date().toISOString().slice(0, 7),
  )
  const [data, setData] = useState<Bva | null>(null)
  const [edit, setEdit] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    call<Bva>("kamra.reports.budget_vs_actual", { property, period })
      .then((d) => {
        setData(d)
        const dr: Record<string, string> = {}
        for (const r of d.rows) dr[r.key] = r.target ? String(r.target) : ""
        setDraft(dr)
      })
      .catch((e) => setError(serverError(e)))
  }, [property, period])
  useEffect(load, [load])

  const save = () =>
    act(() =>
      call("kamra.reports.save_budget", {
        property,
        period,
        room_revenue_target: Number(draft.room_revenue) || 0,
        occupancy_target: Number(draft.occupancy_pct) || 0,
        adr_target: Number(draft.adr) || 0,
        revpar_target: Number(draft.revpar) || 0,
      }).then(() => setEdit(false)),
    )

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      load()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle className="flex items-center gap-2">
            <Target className="size-4 text-accent-text" aria-hidden />{translate("Budget vs Actual")}</CardTitle>
          <p className="mt-0.5 text-xs text-muted-ink">{translate("Month-to-date performance against your targets.")}</p>
        </div>
        <div className="flex items-center gap-1.5">
          <Button variant="outline" onClick={() => setPeriod((p) => shiftMonth(p, -1))}>
            <ChevronLeft className="size-4" />
          </Button>
          <span className="min-w-20 text-center text-sm font-medium">{period}</span>
          <Button variant="outline" onClick={() => setPeriod((p) => shiftMonth(p, 1))}>
            <ChevronRight className="size-4" />
          </Button>
          <Button variant="outline" onClick={() => setEdit((e) => !e)}>
            {edit ? translate("Cancel") : data?.has_budget ? translate("Edit targets") : translate("Set targets")}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {error && (
          <div className="mb-3 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
            {translate(error)}
          </div>
        )}
        {data && (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
                <th className="py-2 pr-3">{translate("Metric")}</th>
                <th className="py-2 pr-3 text-right">{translate("Actual (MTD)")}</th>
                <th className="py-2 pr-3 text-right">{translate("Target")}</th>
                <th className="py-2 pr-3 text-right">{translate("Variance")}</th>
                <th className="py-2 text-right">{translate("Attainment")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {data.rows.map((r) => (
                <tr key={r.key}>
                  <td className="py-2.5 pr-3 font-medium">{translate(r.metric)}</td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">
                    {fmt(r.key, r.actual)}
                  </td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">
                    {edit ? (
                      <input
                        type="number"
                        className="w-24 rounded-lg border border-control px-2 py-1 text-right text-sm"
                        value={draft[r.key] ?? ""}
                        onChange={(e) =>
                          setDraft((d) => ({ ...d, [r.key]: e.target.value }))
                        }
                      />
                    ) : r.target ? (
                      fmt(r.key, r.target)
                    ) : (
                      <span className="text-muted-ink">-</span>
                    )}
                  </td>
                  <td
                    className={
                      "py-2.5 pr-3 text-right tabular-nums " +
                      (r.variance >= 0 ? "text-success-text" : "text-danger-text")
                    }
                  >
                    {r.target
                      ? (r.variance >= 0 ? "+" : "") + fmt(r.key, r.variance)
                      : "-"}
                  </td>
                  <td className="py-2.5 text-right">
                    {r.attainment !== null ? (
                      <span
                        className={
                          "font-semibold tabular-nums " +
                          attainTone(r.attainment)
                        }
                      >
                        {r.attainment}%
                      </span>
                    ) : (
                      <span className="text-muted-ink">-</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {data && (
          <div className="mt-3 space-y-1 text-xs text-muted-ink">
            <p>{translate("Actual counts room nights posted to folios - End of Day posts each night, so tonight counts after tonight's audit.")}{" "}
              {(getLang() !== "en") ? translate("Posted room nights: {n}. Through {date}. Occupancy and RevPAR use {elapsed} of {total} elapsed days.", {
                n: String(data.posted_nights ?? 0), date: String(data.posted_through ?? "-"), elapsed: String(data.days_elapsed), total: String(data.total_days),
              }) : <>
              {data.posted_nights ?? 0}{translate(" room night")}{(data.posted_nights ?? 0) === 1 ? "" : translate("s")}{translate(" posted")}{data.posted_through ? translate(" through {v0}", { v0: String(data.posted_through) }) : ""}{translate("; occupancy and RevPAR are over the ")}{data.days_elapsed}{translate(" of")}{" "}
              {data.total_days}{translate(" days elapsed.")}</>}</p>
            {(data.unposted_nights ?? 0) > 0 && (
              <p className="text-warning-ink">
                {(getLang() !== "en") ? translate("Unposted sold room nights: {pending} ({total} on the books this month). End of Day includes them once those dates close.", {
                  pending: String(data.unposted_nights), total: String(data.on_books_nights),
                }) : <>{data.unposted_nights}{translate(" sold room night")}{data.unposted_nights === 1 ? translate(" isn't") : translate("s aren't")}{translate(" posted yet (")}{data.on_books_nights}{translate(" on the books this month so far). They count once End of Day runs for those dates.")}</>}</p>
            )}
          </div>
        )}
        {edit && (
          <div className="mt-3 flex justify-end">
            <Button disabled={busy} onClick={save}>
              {busy ? translate("Saving...") : translate("Save targets")}
            </Button>
          </div>
        )}
        {data && !data.has_budget && !edit && (
          <p className="mt-3 text-xs text-muted-ink">{translate("No targets set for ")}{data.period}{translate(" yet - \"Set targets\" to track attainment.")}</p>
        )}
      </CardContent>
    </Card>
  )
}

// ---------------------------------------------------------------------------

const BY_OPTIONS: { key: string; label: string }[] = [
  { key: "source", label: "By source" },
  { key: "company", label: "By company" },
  { key: "travel_agent", label: "By travel agent" },
  { key: "channel", label: "By channel" },
]

function Contribution() {
  useUILanguage()
  const property = getCurrentProperty()
  const today = new Date().toISOString().slice(0, 10)
  const monthAgo = new Date(Date.now() - 30 * 864e5).toISOString().slice(0, 10)
  const [from, setFrom] = useState(monthAgo)
  const [to, setTo] = useState(today)
  const [by, setBy] = useState("source")
  const [data, setData] = useState<Contrib | null>(null)

  const load = useCallback(() => {
    call<Contrib>("kamra.reports.contribution", {
      property,
      from_date: from,
      to_date: to,
      by,
    }).then(setData)
  }, [property, from, to, by])
  useEffect(load, [load])

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{translate("Contribution analysis")}</CardTitle>
          <p className="mt-0.5 text-xs text-muted-ink">{translate("Who brings the business - revenue and share by channel.")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-1.5">
          <input
            type="date"
            className="rounded-lg border border-control px-2 py-1.5 text-sm"
            value={from}
            onChange={(e) => setFrom(e.target.value)}
            aria-label={translate("From")}
          />
          <span className="text-xs text-muted-ink">{translate("to")}</span>
          <input
            type="date"
            className="rounded-lg border border-control px-2 py-1.5 text-sm"
            value={to}
            onChange={(e) => setTo(e.target.value)}
            aria-label={translate("To")}
          />
          <select
            className="rounded-lg border border-control px-2.5 py-1.5 text-sm"
            value={by}
            onChange={(e) => setBy(e.target.value)}
            aria-label={translate("Group by")}
          >
            {BY_OPTIONS.map((o) => (
              <option key={o.key} value={o.key}>
                {translate(o.label)}
              </option>
            ))}
          </select>
        </div>
      </CardHeader>
      <CardContent>
        {data && data.rows.length === 0 && (
          <p className="py-6 text-center text-sm text-muted-ink">{translate("No bookings in this window.")}</p>
        )}
        {data && data.rows.length > 0 && (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
                <th className="py-2 pr-3">{translate(BY_OPTIONS.find((o) => o.key === by)?.label.replace("By ", ""))}</th>
                <th className="py-2 pr-3 text-right">{translate("Bookings")}</th>
                <th className="py-2 pr-3 text-right">{translate("Room nights")}</th>
                <th className="py-2 pr-3 text-right">{translate("Revenue ")}{cur()}</th>
                <th className="py-2 pr-3 text-right">{translate("Commission")}</th>
                <th className="py-2 text-right">{translate("Share")}</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {data.rows.map((r) => (
                <tr key={r.label}>
                  <td className="py-2.5 pr-3 font-medium">{by === "source" || r.label === "Direct" ? translate(r.label) : r.label}</td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">{r.bookings}</td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">{r.room_nights}</td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">{inr(r.revenue)}</td>
                  <td className="py-2.5 pr-3 text-right tabular-nums">{r.commission ? inr(r.commission) : "-"}</td>
                  <td className="py-2.5 text-right">
                    <div className="flex items-center justify-end gap-2">
                      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-subtle">
                        <div
                          className="h-full rounded-full bg-accent-fill"
                          style={{ width: `${r.share}%` }}
                        />
                      </div>
                      <span className="w-10 text-right tabular-nums">{r.share}%</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr className="border-t border-line font-semibold">
                <td className="py-2 pr-3">{translate("Total")}</td>
                <td />
                <td />
                <td className="py-2 pr-3 text-right tabular-nums">{inr(data.total)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">
                  {inr(data.rows.reduce((s, r) => s + (r.commission || 0), 0))}
                </td>
                <td className="py-2 text-right">100%</td>
              </tr>
            </tfoot>
          </table>
        )}
      </CardContent>
    </Card>
  )
}

export default function RevenueReports() {
  useUILanguage()
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">{translate("Revenue Reports")}</h1>
        <p className="mt-0.5 text-sm text-muted-ink">{translate("Targets vs performance, and where the business comes from.")}</p>
      </header>
      <BudgetVsActual />
      <Contribution />
    </div>
  )
}
