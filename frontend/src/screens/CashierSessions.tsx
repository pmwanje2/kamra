import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { Badge } from "../components/ui/badge"
import { fmtAmount, useLocale } from "../lib/money"
import { t as translate, useUILanguage } from "../lib/i18n"

type ForeignDrawer = {
  currency: string
  expected_fx: number
  counted_fx: number | null
  variance_fx: number | null
}

type Row = {
  name: string
  cashier_id?: string
  user: string
  business_date: string
  status: string
  opening_float: number
  system_cash: number
  expected_cash: number
  counted_cash: number
  variance: number
  foreign_drawers?: ForeignDrawer[]
  opened_at: string
  closed_at?: string
}

export default function CashierSessions() {
  useUILanguage()
  useLocale()
  const [rows, setRows] = useState<Row[]>([])
  const [date, setDate] = useState("")
  const [err, setErr] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const out = await call<{ sessions: Row[]; business_date: string }>(
        "kamra.cashier.list_sessions",
        {
          property: getCurrentProperty(),
          business_date: date || undefined,
        },
      )
      setRows(out.sessions)
      if (!date) setDate(out.business_date)
    } catch (e) {
      setErr(serverError(e))
    }
  }, [date])

  useEffect(() => {
    void load()
  }, [load])

  return (
    <div className="w-full max-w-none space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-ink">{translate("Cashier")}</p>
          <h1 className="text-2xl font-semibold">{translate("Sessions")}</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-ink">{translate("Open and close tills on")}{" "}
            <Link to="/cashier" className="font-medium text-accent-ink hover:underline">{translate("My Till")}</Link>{translate(". There is no separate “create till” form - opening a session starts the shift.")}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Link
            to="/cashier"
            className="rounded-lg bg-accent-fill px-3 py-1.5 text-sm font-medium text-on-solid hover:bg-accent-fill-hover"
          >{translate("Open / close till")}</Link>
          <label className="text-sm">
            <span className="me-2 text-muted-ink">{translate("Business date")}</span>
            <input
              type="date"
              className="rounded-lg border border-control px-2 py-1.5"
              value={date}
              onChange={(e) => setDate(e.target.value)}
            />
          </label>
        </div>
      </div>
      {err ? (
        <p className="text-sm text-danger-text">{translate(err)}</p>
      ) : null}
      <div className="overflow-x-auto rounded-xl border border-line bg-surface">
        <table className="min-w-[680px] w-full text-start text-sm">
          <thead className="border-b border-line-subtle text-xs uppercase tracking-wide text-muted-ink">
            <tr>
              <th className="px-3 py-2">{translate("Cashier")}</th>
              <th className="px-3 py-2">{translate("Status")}</th>
              <th className="px-3 py-2">{translate("Float")}</th>
              <th className="px-3 py-2">{translate("Cash")}</th>
              <th className="px-3 py-2">{translate("Expected")}</th>
              <th className="px-3 py-2">{translate("Counted")}</th>
              <th className="px-3 py-2">{translate("Foreign drawers")}</th>
              <th className="px-3 py-2">{translate("Variance")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.name} className="border-t border-line-subtle">
                <td className="whitespace-nowrap px-3 py-2">
                  <Link
                    to={`/cashier/shift-report?session=${r.name}`}
                    className="font-medium text-accent-ink hover:underline"
                  >
                    {r.cashier_id || r.user}
                  </Link>
                  <div className="text-xs text-muted-ink">{r.user}</div>
                </td>
                <td className="whitespace-nowrap px-3 py-2">
                  <Badge tone={r.status === "Open" ? "green" : "zinc"}>
                    {translate(r.status)}
                  </Badge>
                </td>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtAmount(r.opening_float)}</td>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtAmount(r.system_cash)}</td>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtAmount(r.expected_cash)}</td>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtAmount(r.counted_cash)}</td>
                <td className="whitespace-nowrap px-3 py-2 text-xs tabular-nums text-secondary-ink">
                  {r.foreign_drawers?.length
                    ? r.foreign_drawers.map((fd) => `${fd.currency} ${fmtAmount(fd.expected_fx, fd.currency)}`).join(" · ")
                    : "-"}
                </td>
                <td className="whitespace-nowrap px-3 py-2 tabular-nums">{fmtAmount(r.variance)}</td>
              </tr>
            ))}
            {!rows.length ? (
              <tr>
                <td colSpan={8} className="px-3 py-10 text-center text-muted-ink">{translate("No sessions for this date.")}{" "}
                  <Link to="/cashier" className="text-accent-ink hover:underline">{translate("Open a till")}</Link>{" "}{translate("to start.")}</td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  )
}
