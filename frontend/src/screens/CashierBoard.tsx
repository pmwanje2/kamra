/** All tills: who holds which, what is on its way to the cash office, tills
 *  that closed over or short, and the terminals themselves (name, outlet,
 *  which printer prints its bills). */
import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Landmark, Lock, Plus, Unlock } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { useAuth } from "../lib/auth"
import { useCashierAuth } from "../lib/cashierAuth"
import { Button } from "../components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "../components/ui/card"
import { fmtAmount, moneyDigits, useLocale } from "../lib/money"
import { useT, t as translate } from "../lib/i18n"

type ForeignDrawer = {
  currency: string
  expected_fx: number
  counted_fx: number | null
  variance_fx: number | null
  home_equivalent: number
}

const WRITE_ROLES = ["Finance", "Hotel Admin", "System Manager", "Administrator"]
const KINDS = ["Front Desk", "Restaurant", "Bar", "Spa", "Cash Office", "Other"]

type BoardSession = {
  name: string
  user: string
  cashier_id?: string
  opened_at: string
  expected_cash: number
  foreign_drawers?: ForeignDrawer[]
}
type Till = {
  name: string | null
  terminal_name: string
  kind: string
  outlet: string | null
  bill_printer: string | null
  is_default: boolean
  sessions: BoardSession[]
  safe?: { balance: number; in_transit: number }
}
type Board = {
  business_date: string
  tills: Till[]
  pending_transfers: {
    name: string
    amount: number
    from_terminal: string | null
    to_terminal: string
    requested_by: string
  }[]
  variance_review: {
    name: string
    user: string
    variance: number
    business_date: string
    foreign_drawers?: ForeignDrawer[]
  }[]
}
type TerminalRow = {
  name: string
  terminal_name: string
  kind: string
  outlet: string | null
  bill_printer: string | null
  default_float: number
  disabled: 0 | 1
}
type Draft = Omit<TerminalRow, "name" | "disabled" | "default_float"> & { default_float: string; name?: string; disabled?: 0 | 1 }
type Printer = { name: string; printer_name: string; prints_bills: 0 | 1 }
type Outlet = { name: string; outlet_name: string }

const blank = (): Draft => ({
  terminal_name: "",
  kind: "Front Desk",
  outlet: null,
  bill_printer: null,
  default_float: "",
})

export default function CashierBoard() {
  const { t } = useT()
  const { currency } = useLocale()
  const { roles } = useAuth()
  const { withPin } = useCashierAuth()
  const canEdit = roles.some((r) => WRITE_ROLES.includes(r))
  const property = getCurrentProperty()
  const [board, setBoard] = useState<Board | null>(null)
  const [terminals, setTerminals] = useState<TerminalRow[]>([])
  const [printers, setPrinters] = useState<Printer[]>([])
  const [outlets, setOutlets] = useState<Outlet[]>([])
  const [form, setForm] = useState<Draft | null>(null)
  const [bankAmt, setBankAmt] = useState<Record<string, string>>({})
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const termName = (id: string | null) =>
    id ? (terminals.find((x) => x.name === id)?.terminal_name ?? id) : t("Default")

  const load = useCallback(async () => {
    try {
      const [b, tl, pr, ol] = await Promise.all([
        call<Board>("kamra.cashier.tills_board", { property }),
        call<TerminalRow[]>("kamra.cashier.terminals", { property, include_disabled: 1 }),
        call<Printer[]>("kamra.printing.printers", { property }),
        call<Outlet[]>("kamra.pos.outlets", { property }),
      ])
      setBoard(b)
      setTerminals(tl)
      setPrinters(pr)
      setOutlets(ol)
    } catch (e) {
      setErr(serverError(e))
    }
  }, [property])

  useEffect(() => {
    void load()
  }, [load])

  const act = async (fn: () => Promise<void>) => {
    setBusy(true)
    setErr(null)
    try {
      await fn()
      await load()
    } catch (e) {
      setErr(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  const receive = (transfer: string) =>
    act(() =>
      withPin(async (pin) => {
        await call("kamra.cashier.receive_transfer", { transfer, pin })
      }),
    )
  const approve = (session: string) =>
    act(async () => {
      await call("kamra.cashier.approve_variance", { session })
    })
  const bank = (office: string) =>
    act(() =>
      withPin(async (pin) => {
        await call("kamra.cashier.bank_deposit", {
          cash_office: office,
          amount: Number(bankAmt[office] || 0),
          pin,
        })
        setBankAmt({ ...bankAmt, [office]: "" })
      }),
    )
  const floatText = form?.default_float ?? ""
  const digits = moneyDigits(currency)
  const floatValue = Number(floatText)
  const validFloat = (floatText === "" || /^(?:\d+\.?\d*|\.\d+)$/.test(floatText))
    && (floatText.split(".")[1]?.length ?? 0) <= digits
    && floatValue <= Number.MAX_SAFE_INTEGER / 10 ** digits
  const save = () =>
    act(async () => {
      if (!form || !validFloat) return
      await call("kamra.cashier.save_terminal", {
        property,
        name: form.name || undefined,
        terminal_name: form.terminal_name,
        kind: form.kind,
        outlet: form.outlet || undefined,
        bill_printer: form.bill_printer || undefined,
        default_float: floatValue,
        disabled: form.disabled ?? 0,
      })
      setForm(null)
    })

  const billPrinters = printers.filter((p) => p.prints_bills)

  return (
    <div className="w-full max-w-none space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-[10px] font-semibold uppercase tracking-[0.14em] text-muted-ink">
            {t("Cashier")}
          </p>
          <h1 className="text-2xl font-semibold tracking-tight text-ink">{t("All tills")}</h1>
          <p className="mt-1 text-sm text-muted-ink">
            {t("Business date")}{" "}
            <span className="font-medium text-secondary-ink">{board?.business_date ?? "-"}</span>
          </p>
        </div>
        <Link
          to="/cashier"
          className="rounded-lg border border-line px-3 py-1.5 text-sm text-secondary-ink hover:bg-canvas"
        >
          {t("My till")}
        </Link>
      </div>

      {err ? (
        <div className="rounded-lg border border-danger-line bg-danger-subtle px-4 py-2 text-sm text-danger-ink">
          {translate(err)}
        </div>
      ) : null}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {(board?.tills ?? []).map((tl) => (
          <Card key={tl.name ?? "default"}>
            <CardHeader className="pb-2">
              <CardTitle className="flex items-center justify-between gap-2 text-base">
                <span className="flex items-center gap-2">
                  {tl.kind === "Cash Office" ? <Landmark className="size-4" /> : null}
                  {tl.terminal_name}
                </span>
                <span
                  className={
                    "inline-flex items-center gap-1 text-xs font-normal " +
                    (tl.sessions.length ? "text-success-ink" : "text-muted-ink")
                  }
                >
                  {tl.sessions.length ? <Unlock className="size-3" /> : <Lock className="size-3" />}
                  {tl.sessions.length ? t("Open") : t("Closed")}
                </span>
              </CardTitle>
              <p className="text-xs text-muted-ink">
                {translate(tl.kind)}
                {tl.outlet ? ` · ${outlets.find((o) => o.name === tl.outlet)?.outlet_name ?? tl.outlet}` : ""}
              </p>
            </CardHeader>
            <CardContent className="space-y-2 pt-0 text-sm">
              {tl.sessions.map((s) => (
                <div key={s.name} className="space-y-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="truncate text-secondary-ink">{s.cashier_id || s.user}</span>
                    <span className="tabular-nums text-ink">
                      {fmtAmount(s.expected_cash)}
                      <span className="ms-1 text-xs text-muted-ink">
                        {String(s.opened_at).replace(" ", "T").slice(11, 16)}
                      </span>
                    </span>
                  </div>
                  {(s.foreign_drawers?.length ?? 0) > 0 ? (
                    <div className="flex flex-wrap gap-1.5 text-xs text-muted-ink">
                      {s.foreign_drawers!.map((fd) => (
                        <span
                          key={fd.currency}
                          className="rounded bg-subtle px-1.5 py-0.5 tabular-nums"
                        >
                          {fd.currency} {fmtAmount(fd.expected_fx, fd.currency)}
                        </span>
                      ))}
                    </div>
                  ) : null}
                </div>
              ))}
              {tl.is_default && tl.sessions.length ? (
                <p className="text-xs text-muted-ink">
                  {t("Sessions opened without choosing a till.")}
                </p>
              ) : null}
              {tl.safe ? (
                <div className="space-y-2 border-t border-line-subtle pt-2">
                  <div className="flex justify-between">
                    <span className="text-muted-ink">{t("In the safe")}</span>
                    <span className="font-semibold tabular-nums">{fmtAmount(tl.safe.balance)}</span>
                  </div>
                  {tl.safe.in_transit ? (
                    <div className="flex justify-between text-xs text-warning-ink">
                      <span>{t("On its way")}</span>
                      <span className="tabular-nums">{fmtAmount(tl.safe.in_transit)}</span>
                    </div>
                  ) : null}
                  {canEdit && tl.name ? (
                    <div className="flex gap-2">
                      <input
                        className="w-full rounded-lg border border-control px-3 py-1.5 text-sm"
                        placeholder={t("Amount to bank")}
                        inputMode="decimal"
                        value={bankAmt[tl.name] ?? ""}
                        onChange={(e) => setBankAmt({ ...bankAmt, [tl.name!]: e.target.value })}
                      />
                      <Button
                        variant="outline"
                        disabled={busy || !bankAmt[tl.name]}
                        onClick={() => bank(tl.name!)}
                      >
                        {t("Bank it")}
                      </Button>
                    </div>
                  ) : null}
                </div>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>

      {board?.pending_transfers.length ? (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t("Cash on its way to the cash office")}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {board.pending_transfers.map((x) => (
              <div key={x.name} className="flex items-center justify-between gap-3 text-sm">
                <span className="text-secondary-ink">
                  {termName(x.from_terminal)} → {termName(x.to_terminal)} · {x.requested_by}
                </span>
                <span className="flex items-center gap-3">
                  <span className="font-semibold tabular-nums">{fmtAmount(x.amount)}</span>
                  <Button variant="outline" disabled={busy} onClick={() => receive(x.name)}>
                    {t("Received")}
                  </Button>
                </span>
              </div>
            ))}
          </CardContent>
        </Card>
      ) : null}

      {board?.variance_review.length ? (
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-base">{t("Tills to review")}</CardTitle>
            <p className="text-sm font-normal text-muted-ink">
              {t("Closed over or short by more than the allowed variance.")}
            </p>
          </CardHeader>
          <CardContent className="space-y-2 pt-0">
            {board.variance_review.map((v) => {
              const fxWithVar = (v.foreign_drawers || []).filter(
                (fd) =>
                  fd.variance_fx !== null &&
                  fd.variance_fx !== undefined &&
                  Math.abs(fd.variance_fx) >= 0.5 * 10 ** -moneyDigits(fd.currency),
              )
              return (
                <div key={v.name} className="flex flex-wrap items-center justify-between gap-3 text-sm">
                  <div className="min-w-0">
                    <span className="text-secondary-ink">
                      {v.user} · {v.business_date}
                    </span>
                    {fxWithVar.length ? (
                      <div className="mt-1 flex flex-wrap gap-1.5 text-xs">
                        {fxWithVar.map((fd) => (
                          <span
                            key={fd.currency}
                            className="rounded bg-danger-subtle px-1.5 py-0.5 font-medium tabular-nums text-danger-ink"
                          >
                            {fd.currency} {fmtAmount(fd.variance_fx, fd.currency)}
                          </span>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <span className="flex flex-wrap items-center gap-3">
                    <span className="font-semibold tabular-nums text-danger-ink">{fmtAmount(v.variance)}</span>
                    <Link
                      to={`/cashier/shift-report?session=${v.name}`}
                      className="text-xs text-accent-ink hover:underline"
                    >
                      {t("View report")}
                    </Link>
                    {canEdit ? (
                      <Button variant="outline" disabled={busy} onClick={() => approve(v.name)}>
                        {t("Approve")}
                      </Button>
                    ) : null}
                  </span>
                </div>
              )
            })}
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between gap-3 pb-2">
          <div>
            <CardTitle className="text-base">{t("Terminals")}</CardTitle>
            <p className="text-sm font-normal text-muted-ink">
              {t("Name each till, tie it to an outlet, and choose the printer that prints its bills. Without terminals everyone uses the Default till.")}
            </p>
          </div>
          {canEdit ? (
            <Button onClick={() => setForm(blank())}>
              <Plus className="me-1.5 size-4" />
              {t("Add terminal")}
            </Button>
          ) : null}
        </CardHeader>
        <CardContent className="overflow-x-auto pt-0">
          {terminals.length ? (
            <table className="min-w-[520px] w-full text-start text-sm">
              <thead className="text-xs uppercase tracking-wide text-muted-ink">
                <tr>
                  <th className="py-2 pe-3">{t("Name")}</th>
                  <th className="py-2 pe-3">{t("Kind")}</th>
                  <th className="py-2 pe-3">{t("Outlet")}</th>
                  <th className="py-2 pe-3">{t("Bill printer")}</th>
                  <th className="py-2" />
                </tr>
              </thead>
              <tbody>
                {terminals.map((x) => (
                  <tr key={x.name} className="border-t border-line-subtle">
                    <td className="py-2 pe-3">
                      {x.terminal_name}
                      {x.disabled ? (
                        <span className="ms-2 text-xs text-muted-ink">{t("Disabled")}</span>
                      ) : null}
                    </td>
                    <td className="py-2 pe-3">{translate(x.kind)}</td>
                    <td className="py-2 pe-3 text-muted-ink">
                      {outlets.find((o) => o.name === x.outlet)?.outlet_name ?? "-"}
                    </td>
                    <td className="py-2 pe-3 text-muted-ink">
                      {printers.find((p) => p.name === x.bill_printer)?.printer_name ?? t("This device")}
                    </td>
                    <td className="py-2 text-end">
                      {canEdit ? (
                        <Button variant="ghost" onClick={() => setForm({ ...x, default_float: String(x.default_float ?? "") })}>
                          {t("Edit")}
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="rounded-lg border border-dashed border-line px-4 py-6 text-center text-sm text-muted-ink">
              {t("No terminals yet. Everyone works from the Default till, as before.")}
            </p>
          )}

          {form ? (
            <div className="mt-4 space-y-3 rounded-xl border border-line bg-canvas/60 p-4">
              <div className="grid gap-3 sm:grid-cols-2">
                <label className="block text-sm">
                  <span className="mb-1 block text-secondary-ink">{t("Name")}</span>
                  <input
                    className="w-full rounded-lg border border-control px-3 py-2 text-sm"
                    value={form.terminal_name}
                    onChange={(e) => setForm({ ...form, terminal_name: e.target.value })}
                  />
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block text-secondary-ink">{t("Kind")}</span>
                  <select
                    className="w-full rounded-lg border border-control px-3 py-2 text-sm"
                    value={form.kind}
                    onChange={(e) => setForm({ ...form, kind: e.target.value })}
                  >
                    {KINDS.map((k) => (
                      <option key={k} value={k}>{translate(k)}</option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block text-secondary-ink">{t("Outlet")}</span>
                  <select
                    className="w-full rounded-lg border border-control px-3 py-2 text-sm"
                    value={form.outlet || ""}
                    onChange={(e) => setForm({ ...form, outlet: e.target.value || null })}
                  >
                    <option value="">{t("None")}</option>
                    {outlets.map((o) => (
                      <option key={o.name} value={o.name}>
                        {o.outlet_name}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block text-secondary-ink">{t("Bill printer")}</span>
                  <select
                    className="w-full rounded-lg border border-control px-3 py-2 text-sm"
                    value={form.bill_printer || ""}
                    onChange={(e) => setForm({ ...form, bill_printer: e.target.value || null })}
                  >
                    <option value="">{t("This device prints it")}</option>
                    {billPrinters.map((p) => (
                      <option key={p.name} value={p.name}>
                        {p.printer_name}
                      </option>
                    ))}
                  </select>
                  <span className="mt-1 block text-xs text-muted-ink">
                    {t("Only printers set to print bills are listed. KOTs are set on each printer's routes, not here.")}{" "}
                    <Link to="/printers" className="text-accent-ink hover:underline">
                      {t("Printers")}
                    </Link>
                  </span>
                </label>
                <label className="block text-sm">
                  <span className="mb-1 block text-secondary-ink">{t("Default opening float")} ({currency})</span>
                  <input
                    className="w-full rounded-lg border border-control px-3 py-2 text-sm"
                    inputMode="decimal"
                    value={form.default_float}
                    aria-invalid={!validFloat}
                    onChange={(e) => {
                      const value = e.target.value
                      if (/^\d*\.?\d*$/.test(value)) setForm({ ...form, default_float: value })
                    }}
                  />
                </label>
                {form.name ? (
                  <label className="flex items-center gap-2 self-end pb-2 text-sm">
                    <input
                      type="checkbox"
                      checked={!!form.disabled}
                      onChange={(e) => setForm({ ...form, disabled: e.target.checked ? 1 : 0 })}
                    />
                    {t("Disabled")}
                  </label>
                ) : null}
              </div>
              <div className="flex gap-2">
                <Button disabled={busy || !validFloat || !form.terminal_name.trim()} onClick={save}>
                  {t("Save")}
                </Button>
                <Button variant="ghost" onClick={() => setForm(null)}>
                  {t("Cancel")}
                </Button>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
