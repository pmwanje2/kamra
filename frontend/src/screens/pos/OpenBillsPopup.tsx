/**
 * Full-screen-ish popup for open bills: every running bill listed large,
 * selected bill's lines shown with room to read. The skinny till column
 * stays for punch; this is for seeing everything.
 */
import { useEffect } from "react"
import { Clock, Maximize2, X, XCircle } from "lucide-react"
import { Button } from "../../components/ui/button"
import { cn } from "../../lib/utils"
import { cur, fmtAmount } from "../../lib/money"
import { useT, t as translate } from "../../lib/i18n"


const inr = (n: unknown) => fmtAmount(n ?? 0)
const inr2 = (n: unknown) => fmtAmount(n ?? 0)

export interface OpenBillRow {
  name: string
  label: string
  status: string
  order_total: number
  items: number
  pending: number
  kot_fired: number
  kot_no: number | null
  order_type: string | null
  table_no: string | null
  creation: string
  nc?: number
  age_min?: number
  stale?: 0 | 1
}

export interface BillLine {
  row: string
  item_name: string
  qty: number
  amount: number
  instructions: string | null
  kot_status: string
  voided: number
}

function ago(ts?: string) {
  if (!ts) return ""
  const mins = Math.max(
    0,
    Math.round((Date.now() - new Date(ts.replace(" ", "T")).getTime()) / 60000),
  )
  if (mins < 60) return `${mins}m`
  if (mins < 600) return `${Math.floor(mins / 60)}h ${mins % 60}m`
  return `${Math.round(mins / 60)}h`
}

export default function OpenBillsPopup({
  open,
  selected,
  detailLabel,
  detailStatus,
  detailKot,
  detailNc,
  lines,
  grand,
  busy,
  onSelect,
  onSettle,
  onClose,
  onVoid,
  onRemove,
}: {
  open: OpenBillRow[]
  selected: string | null
  detailLabel: string
  detailStatus: string
  detailKot: number | null
  detailNc: boolean
  lines: BillLine[]
  grand: number
  busy: boolean
  onSelect: (name: string) => void
  onSettle: () => void
  onClose: () => void
  onVoid?: (row: string) => void
  onRemove?: (row: string) => void
}) {
  const { t } = useT()

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onClose])

  const selectedRow = open.find((o) => o.name === selected) || null

  return (
    <div
      className="fixed inset-0 z-[55] flex items-stretch justify-center p-2 sm:items-center sm:p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="open-bills-title"
    >
      <div className="absolute inset-0 bg-black/45 animate-fade-in" onClick={onClose} aria-hidden />
      <div className="relative z-10 flex h-[min(92vh,880px)] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl animate-sheet-in">
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-line-subtle px-4 py-3 sm:px-5">
          <div>
            <h2 id="open-bills-title" className="text-lg font-semibold text-ink">
              {t("Open bills")}
              <span className="ms-2 text-base font-normal text-muted-ink">· {open.length}</span>
            </h2>
            <p className="text-sm text-muted-ink">{t("Pick a bill to see every line")}</p>
          </div>
          <button
            type="button"
            className="rounded-xl p-2.5 text-muted-ink hover:bg-subtle hover:text-secondary-ink"
            aria-label={t("Close")}
            onClick={onClose}
          >
            <X className="size-5" />
          </button>
        </div>

        <div className="flex min-h-0 flex-1 flex-col md:flex-row">
          {/* all open bills */}
          <div className="max-h-[38%] shrink-0 overflow-y-auto border-b border-line-subtle p-3 md:max-h-none md:w-[17rem] md:border-b-0 md:border-e md:p-4 xl:w-[19rem]">
            <ul className="grid grid-cols-2 gap-2 md:grid-cols-1">
              {open.map((o) => {
                const tone = !o.kot_fired
                  ? o.stale
                    ? "border-rose-400 bg-danger-subtle"
                    : "border-warning-line-strong bg-warning-subtle"
                  : o.pending > 0
                    ? "border-info-line-strong bg-info-subtle"
                    : "border-success-line-strong bg-success-subtle"
                const tag = !o.kot_fired
                  ? `${t("Not sent")} · ${ago(o.creation)}`
                  : o.pending > 0
                    ? t("In kitchen")
                    : t("Ready / unpaid")
                return (
                  <li key={o.name}>
                    <button
                      type="button"
                      onClick={() => onSelect(o.name)}
                      className={cn(
                        "flex w-full flex-col rounded-xl border-2 px-3 py-2.5 text-left transition",
                        tone,
                        selected === o.name && "ring-2 ring-accent-control ring-offset-1",
                      )}
                    >
                      <span className="truncate text-sm font-bold text-ink">{translate(o.label)}</span>
                      <span className="mt-1 flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-wide text-secondary-ink">
                        <Clock className="size-3.5" />
                        {tag}
                      </span>
                      <span className="mt-1 text-sm font-semibold tabular-nums text-ink">
                        {cur()}
                        {inr(o.order_total)}
                        {o.kot_no ? translate(" · KOT {v0}", { v0: String(o.kot_no) }) : ""}
                        {o.items ? ` · ${o.items}` : ""}
                      </span>
                    </button>
                  </li>
                )
              })}
            </ul>
            {open.length === 0 && (
              <p className="py-8 text-center text-sm text-muted-ink">{t("No open bills.")}</p>
            )}
          </div>

          {/* selected bill lines */}
          <div className="flex min-h-0 min-w-0 flex-1 flex-col">
            {selected && selectedRow ? (
              <>
                <div className="flex shrink-0 items-start justify-between gap-3 border-b border-line-subtle px-4 py-3 sm:px-5">
                  <div>
                    <h3 className="text-xl font-semibold text-ink">{translate(detailLabel)}</h3>
                    <p className="mt-0.5 text-sm text-muted-ink">
                      {detailKot ? translate("KOT #{v0} · ", { v0: String(detailKot) }) : ""}
                      {translate(detailStatus)}
                      {detailNc ? ` · ${t("Complimentary")}` : ""}
                    </p>
                  </div>
                  <div className="text-right">
                    <div className="text-2xl font-semibold tabular-nums text-ink">
                      {cur()}
                      {inr2(grand)}
                    </div>
                  </div>
                </div>
                <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3 sm:px-5">
                  <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-ink">
                    {t("Order items")} · {lines.filter((l) => !l.voided).length}
                  </h4>
                  <ul className="divide-y divide-line-subtle rounded-xl border border-line bg-canvas/50">
                    {lines.map((it) => (
                      <li
                        key={it.row}
                        className={cn(
                          "flex items-center justify-between gap-3 px-3.5 py-3",
                          !!it.voided && "opacity-50",
                        )}
                      >
                        <div className="min-w-0">
                          <div
                            className={cn(
                              "text-[15px] font-medium text-ink",
                              !!it.voided && "line-through",
                            )}
                          >
                            {Math.round(it.qty)}× {it.item_name}
                          </div>
                          {!it.voided && (
                            <div className="mt-0.5 text-xs text-muted-ink">
                              {it.kot_status === "New"
                                ? t("not sent")
                                : it.kot_status === "Prepared"
                                  ? t("Prepared")
                                  : t("In kitchen")}
                              {it.instructions ? ` · ${it.instructions}` : ""}
                            </div>
                          )}
                        </div>
                        <div className="flex shrink-0 items-center gap-2">
                          <span className="text-base font-semibold tabular-nums text-ink">
                            {cur()}
                            {inr(it.amount)}
                          </span>
                          {!it.voided && it.kot_status === "New" && onRemove && (
                            <button
                              type="button"
                              disabled={busy}
                              title={t("Remove (not sent to the kitchen)")}
                              onClick={() => onRemove(it.row)}
                              className="grid size-9 place-items-center rounded-lg text-muted-ink hover:bg-danger-subtle hover:text-danger-text"
                            >
                              <XCircle className="size-4" />
                            </button>
                          )}
                          {!it.voided && it.kot_status !== "New" && onVoid && (
                            <button
                              type="button"
                              title={t("Void (the kitchen has it)")}
                              onClick={() => onVoid(it.row)}
                              className="grid size-9 place-items-center rounded-lg text-muted-ink hover:bg-danger-subtle hover:text-danger-text"
                            >
                              <XCircle className="size-4" />
                            </button>
                          )}
                        </div>
                      </li>
                    ))}
                    {lines.length === 0 && (
                      <li className="px-3.5 py-8 text-center text-sm text-muted-ink">
                        {t("No items on this bill yet.")}
                      </li>
                    )}
                  </ul>
                </div>
                <div className="flex shrink-0 gap-2 border-t border-line-subtle bg-surface px-4 py-3 sm:px-5">
                  <Button variant="outline" className="h-12 flex-1" onClick={onClose}>
                    {t("Back to till")}
                  </Button>
                  <Button className="h-12 flex-1 text-base font-semibold" disabled={busy} onClick={onSettle}>
                    {t("Settle")} · {cur()}
                    {inr2(grand)}
                  </Button>
                </div>
              </>
            ) : (
              <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-muted-ink">
                <Maximize2 className="size-8 opacity-40" />
                <p className="text-sm">{t("Select a bill on the left to see every line.")}</p>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
