import { useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { Lock, Pencil, Plus, Route as RouteIcon, X } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { folioLabel, windowBadge, windowBalance, type SiblingFolio } from "../lib/folioLabel"
import { useT } from "../lib/i18n"
import { fmtMoney, type MoneyContext, useLocale } from "../lib/money"
import { Button } from "./ui/button"

export interface FolioWindowTabItem extends SiblingFolio {
  folio?: string
}

interface CompanyOption {
  name: string
  company_name: string
}

interface AgentOption {
  name: string
  agent_name: string
}

const fieldCls =
  "w-full rounded-lg border border-control bg-surface px-2.5 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

const isEmptyWindow = (w: SiblingFolio) => !w.grand_total && !w.payments_total

export default function FolioWindowTabs({
  currentFolio,
  property,
  windows,
  currency,
  money,
  reservation,
  groupBooking,
  windowCap,
  canAddWindow,
  busy,
  onDeleteEmpty,
  onOpenRouting,
  onWindowChanged,
  act,
}: {
  currentFolio: string
  property?: string
  windows: FolioWindowTabItem[]
  currency: string
  money?: MoneyContext
  reservation: string | null
  groupBooking: string | null
  windowCap: number
  canAddWindow: boolean
  busy: boolean
  onDeleteEmpty: (target: SiblingFolio) => void
  onOpenRouting?: () => void
  onWindowChanged: (navigateToFolio?: string) => void
  act: (fn: () => Promise<unknown>, onError?: (msg: string) => void) => Promise<void>
}) {
  const { t } = useT()
  const moneyContext = money ?? { currency }
  const loc = useLocale()
  const [popover, setPopover] = useState<"add" | "edit" | null>(null)
  const [companies, setCompanies] = useState<CompanyOption[]>([])
  const [agents, setAgents] = useState<AgentOption[]>([])
  const [optionsLoaded, setOptionsLoaded] = useState(false)

  const activeWindow = windows.find((w) => w.name === currentFolio) ?? null
  const activeEditable =
    !!activeWindow &&
    activeWindow.status === "Open" &&
    !activeWindow.invoice_number &&
    !activeWindow.sales_invoice

  const [payeeType, setPayeeType] = useState<"Guest" | "Company" | "Travel Agent">("Guest")
  const [payeeCompany, setPayeeCompany] = useState("")
  const [payeeAgent, setPayeeAgent] = useState("")
  const [paymentMethod, setPaymentMethod] = useState("")
  const [billToName, setBillToName] = useState("")

  useEffect(() => {
    if (!popover || optionsLoaded) return
    call<{ companies?: CompanyOption[]; travel_agents?: AgentOption[] }>("kamra.api.booking_options", {
      property: property || getCurrentProperty(),
    })
      .then((res) => {
        setCompanies(res?.companies ?? [])
        setAgents(res?.travel_agents ?? [])
        setOptionsLoaded(true)
      })
      .catch(() => setOptionsLoaded(true))
  }, [popover, optionsLoaded])

  function openAdd() {
    if (popover === "add") {
      setPopover(null)
      return
    }
    setPayeeType("Guest")
    setPayeeCompany("")
    setPayeeAgent("")
    setPaymentMethod("")
    setBillToName("")
    setPopover("add")
  }

  function openEdit() {
    if (!activeWindow || !activeEditable) return
    if (popover === "edit") {
      setPopover(null)
      return
    }
    const pt =
      activeWindow.payee_type === "Company" || activeWindow.payee_type === "Travel Agent"
        ? activeWindow.payee_type
        : "Guest"
    setPayeeType(pt)
    setPayeeCompany(activeWindow.payee_company || "")
    setPayeeAgent(activeWindow.payee_agent || "")
    setPaymentMethod(activeWindow.payment_method || "")
    setBillToName(activeWindow.bill_to_name || "")
    setPopover("edit")
  }

  const totalRoutingRules = windows.reduce((acc, w) => acc + (w.routed_rule_count ?? 0), 0)
  const paymentMethods = loc.payment_modes.length
    ? Array.from(new Set([...loc.payment_modes, "Company Credit"]))
    : ["Cash", "Card", "Bank Transfer", "Company Credit"]

  const payeeInvalid =
    (payeeType === "Company" && !payeeCompany) ||
    (payeeType === "Travel Agent" && !payeeAgent)

  return (
    <div className="mb-4 space-y-2 print:hidden">
      <div className="flex flex-wrap items-center gap-2">
        <div
          data-testid="folio-window-tabs"
          className="flex min-w-0 max-w-full flex-1 snap-x items-center gap-1.5 overflow-x-auto rounded-xl border border-line bg-canvas p-1.5"
          role="tablist"
          aria-label={t("Billing windows")}
        >
          {windows.map((w) => {
            const balance = windowBalance(w)
            const active = w.name === currentFolio
            const locked = w.status === "Closed" || Boolean(w.invoice_number) || Boolean(w.sales_invoice)
            const canDelete =
              !locked &&
              isEmptyWindow(w) &&
              w.folio_type !== "Guest" &&
              w.folio_type !== "Group"
            const wKey = w.window_no || w.name
            const label = folioLabel(windows, w, t)
            return (
              <div
                key={w.name}
                className={
                  "group flex shrink-0 snap-start items-center rounded-lg border transition-colors " +
                  (active
                    ? "border-accent-control/40 bg-surface shadow-sm"
                    : "border-transparent hover:border-line hover:bg-surface/60")
                }
              >
                <Link
                  role="tab"
                  aria-selected={active}
                  to={`/billing/${encodeURIComponent(w.name)}`}
                  className={
                    "flex min-h-11 items-center gap-1.5 px-2.5 py-1.5 text-xs " +
                    (active ? "font-semibold text-ink" : "text-secondary-ink hover:text-ink")
                  }
                >
                  <span className="rounded bg-subtle px-1.5 py-0.5 font-mono text-[11px] font-semibold text-secondary-ink">
                    {windowBadge(w)}
                  </span>
                  <span className="min-w-0 max-w-32 truncate" title={label}>{label}</span>
                  <span
                    className={
                      "shrink-0 tabular-nums " +
                      (balance > 0 ? "text-warning-text" : balance < 0 ? "text-info-ink" : "text-muted-ink")
                    }
                  >
                    {balance < 0 ? `${t("Advance")} ` : ""}
                    <span dir="ltr" className="inline-block tabular-nums">{fmtMoney(Math.abs(balance), moneyContext)}</span>
                  </span>
                  {locked && (
                    <Lock
                      className="size-3.5 shrink-0 text-muted-ink"
                      data-testid={`window-locked-${wKey}`}
                      aria-label={t("Closed or invoiced")}
                    />
                  )}
                  {(w.routed_rule_count ?? 0) > 0 && (
                    <span
                      data-testid={`routing-active-dot-${wKey}`}
                      className="size-2 shrink-0 rounded-full bg-accent-fill"
                      title={t("{n} active routing rule(s)", { n: w.routed_rule_count ?? 0 })}
                    />
                  )}
                </Link>
                {canDelete && (
                  <button
                    type="button"
                    aria-label={t("Delete empty {label} folio", { label })}
                    title={t("Delete this empty folio")}
                    disabled={busy}
                    onClick={() => onDeleteEmpty(w)}
                    className="min-h-11 min-w-11 justify-center px-2 text-muted-ink hover:text-danger-text"
                  >
                    <X className="size-3.5" aria-hidden />
                  </button>
                )}
              </div>
            )
          })}
        </div>

        <div className="flex flex-wrap items-center gap-1.5">
          {(reservation || groupBooking) && (
            <Button
              type="button"
              variant="outline"
              data-testid="add-window-btn"
              className="min-h-11 px-2.5 text-xs sm:min-h-9"
              disabled={busy || !canAddWindow}
              title={
                canAddWindow
                  ? t("Open a new billing window (up to {cap})", { cap: windowCap })
                  : t("Maximum of {cap} windows reached for this bill.", { cap: windowCap })
              }
              onClick={openAdd}
            >
              <Plus className="size-4" aria-hidden />
              {t("Window")}
            </Button>
          )}
          {activeEditable && (
            <Button
              type="button"
              variant="outline"
              data-testid="edit-window-payee-btn"
              className="min-h-11 px-2.5 text-xs sm:min-h-9"
              disabled={busy}
              onClick={openEdit}
            >
              <Pencil className="size-3.5" aria-hidden />
              {t("Edit payee")}
            </Button>
          )}
          {onOpenRouting && (reservation || groupBooking) && (
            <Button
              type="button"
              variant="outline"
              data-testid="open-routing-drawer-btn"
              className="min-h-11 px-2.5 text-xs sm:min-h-9"
              disabled={busy}
              onClick={onOpenRouting}
            >
              <RouteIcon className="size-3.5" aria-hidden />
              {t("Routing")}
              {totalRoutingRules > 0 && (
                <span className="ms-1 rounded-full bg-accent-muted px-1.5 py-0.5 text-[10px] font-semibold text-accent-ink">
                  {totalRoutingRules}
                </span>
              )}
            </Button>
          )}
        </div>
      </div>

      {popover && (
        <div
          data-testid={popover === "add" ? "open-window-popover" : "edit-window-popover"}
          className="rounded-xl border border-line bg-surface p-4 text-sm shadow-sm"
        >
          <div className="mb-3 flex items-center justify-between gap-2">
            <h3 className="font-semibold text-ink">
              {popover === "add"
                ? t("Open billing window")
                : t("Edit payee and payment method ({window})", {
                    window: activeWindow ? windowBadge(activeWindow) : "",
                  })}
            </h3>
            <button
              type="button"
              aria-label={t("Close")}
              onClick={() => setPopover(null)}
              className="grid min-h-11 min-w-11 place-items-center rounded p-1 text-muted-ink hover:text-secondary-ink"
            >
              <X className="size-4" aria-hidden />
            </button>
          </div>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <label className="space-y-1 text-xs text-secondary-ink">
              <span>{t("Payee type")}</span>
              <select
                aria-label={t("Payee type")}
                className={fieldCls}
                value={payeeType}
                onChange={(e) => {
                  const next = e.target.value as "Guest" | "Company" | "Travel Agent"
                  setPayeeType(next)
                  if (next !== "Company") setPayeeCompany("")
                  if (next !== "Travel Agent") setPayeeAgent("")
                }}
              >
                <option value="Guest">{t("Guest")}</option>
                <option value="Company">{t("Company")}</option>
                <option value="Travel Agent">{t("Travel Agent")}</option>
              </select>
            </label>

            {payeeType === "Company" && (
              <label className="space-y-1 text-xs text-secondary-ink">
                <span>{t("Company")}</span>
                <select
                  aria-label={t("Payee company")}
                  className={fieldCls}
                  value={payeeCompany}
                  onChange={(e) => setPayeeCompany(e.target.value)}
                >
                  <option value="">{t("Select company")}</option>
                  {companies.map((c) => (
                    <option key={c.name} value={c.name}>
                      {c.company_name}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {payeeType === "Travel Agent" && (
              <label className="space-y-1 text-xs text-secondary-ink">
                <span>{t("Travel Agent")}</span>
                <select
                  aria-label={t("Payee travel agent")}
                  className={fieldCls}
                  value={payeeAgent}
                  onChange={(e) => setPayeeAgent(e.target.value)}
                >
                  <option value="">{t("Select travel agent")}</option>
                  {agents.map((a) => (
                    <option key={a.name} value={a.name}>
                      {a.agent_name}
                    </option>
                  ))}
                </select>
              </label>
            )}

            {payeeType === "Guest" && (
              <label className="space-y-1 text-xs text-secondary-ink">
                <span>{t("Bill-to name (optional)")}</span>
                <input
                  aria-label={t("Bill-to name (optional)")}
                  className={fieldCls}
                  maxLength={140}
                  placeholder={t("Guest name on bill")}
                  value={billToName}
                  onChange={(e) => setBillToName(e.target.value)}
                />
              </label>
            )}

            <label className="space-y-1 text-xs text-secondary-ink">
              <span>{t("Payment method")}</span>
              <select
                aria-label={t("Window payment method")}
                className={fieldCls}
                value={paymentMethod}
                onChange={(e) => setPaymentMethod(e.target.value)}
              >
                <option value="">{t("Default")}</option>
                {paymentMethods.map((m) => (
                  <option key={m} value={m}>
                    {t(m)}
                  </option>
                ))}
              </select>
            </label>
          </div>

          <div className="mt-3 flex flex-wrap justify-end gap-2">
            <Button className="min-h-11 sm:min-h-0" type="button" variant="ghost" onClick={() => setPopover(null)}>
              {t("Cancel")}
            </Button>
            {popover === "add" ? (
              <Button className="min-h-11 sm:min-h-0"
                type="button"
                data-testid="confirm-open-window"
                disabled={busy || payeeInvalid}
                onClick={() =>
                  act(async () => {
                    const created = await call<{ folio: string; window_no: number }>(
                      "kamra.api.open_folio_window",
                      {
                        reservation: reservation || undefined,
                        group_booking: !reservation ? groupBooking || undefined : undefined,
                        payee_type: payeeType,
                        payee_company: payeeType === "Company" ? payeeCompany : undefined,
                        payee_agent: payeeType === "Travel Agent" ? payeeAgent : undefined,
                        payment_method: paymentMethod || undefined,
                        bill_to_name: payeeType === "Guest" && billToName.trim() ? billToName.trim() : undefined,
                      },
                    )
                    setPopover(null)
                    onWindowChanged(created?.folio)
                  })
                }
              >
                {t("Open window")}
              </Button>
            ) : (
              <Button className="min-h-11 sm:min-h-0"
                type="button"
                data-testid="save-window-payee-btn"
                disabled={busy || payeeInvalid || !activeWindow}
                onClick={() =>
                  act(async () => {
                    await call("kamra.payment_transfers.update_folio_window", {
                      folio: activeWindow!.name,
                      payee_type: payeeType,
                      payee_company: payeeType === "Company" ? payeeCompany : null,
                      payee_agent: payeeType === "Travel Agent" ? payeeAgent : null,
                      payment_method: paymentMethod || null,
                      bill_to_name: payeeType === "Guest" ? billToName.trim() || null : null,
                    })
                    setPopover(null)
                    onWindowChanged()
                  })
                }
              >
                {t("Save window")}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
