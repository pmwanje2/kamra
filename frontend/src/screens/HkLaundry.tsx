import { useMemo, useState } from "react"
import { Shirt, Zap, Plus, Minus, ChevronRight } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { Badge } from "../components/ui/badge"
import { cn } from "../lib/utils"
import { cur } from "../lib/money"
import {
  ExpressToggle,
  inr,
  STATUS_TONE,
  useLaundryBoard,
  type LaundryOrder,
  type Room,
} from "./laundry/shared"
import { t as translate, useUILanguage } from "../lib/i18n"

/** The laundry side of the housekeeping phone app: pickup queue, counting
 * the bag with the guest, tracking what's out, and returning it piece by
 * piece. Prices always come from the property's rate card. Types, the rate
 * card / board data hook and helpers are shared with the desktop Laundry
 * module (see ./laundry/shared). */

export default function HkLaundry({ rooms }: { rooms: Room[] }) {
  useUILanguage()
  const property = getCurrentProperty()
  const { board, rates, error, busy, act } = useLaundryBoard(property)
  // sheets
  const [pickup, setPickup] = useState<{ room: string; notes: string; express: boolean } | null>(null)
  const [counting, setCounting] = useState<{ order: string | null; room: string; express: boolean; qty: Record<string, number> } | null>(null)
  const [returning, setReturning] = useState<{ order: LaundryOrder; back: Record<string, number>; note: string } | null>(null)

  const occupied = useMemo(
    () => rooms.filter((r) => r.occupancy_status === "Occupied"),
    [rooms])

  const countTotal = counting
    ? rates.reduce((s, r) => s + (counting.qty[r.name] || 0) *
        (counting.express ? r.express_rate : r.rate), 0)
    : 0
  const countPieces = counting
    ? Object.values(counting.qty).reduce((s, q) => s + q, 0) : 0

  async function submitCollect() {
    if (!counting || countPieces === 0) return
    const items = rates
      .filter((r) => (counting.qty[r.name] || 0) > 0)
      .map((r) => ({ item_name: r.item_name, service_type: r.service_type, qty: counting.qty[r.name] }))
    await act(async () => {
      await call("kamra.laundry.collect_laundry", {
        property, room: counting.room, items,
        order: counting.order, express: counting.express ? 1 : 0,
      })
      setCounting(null)
    })
  }

  const returnPending = returning
    ? returning.order.items.reduce(
        (s, it) => s + it.qty - (returning.back[it.name] ?? it.returned_qty), 0)
    : 0

  async function submitDeliver() {
    if (!returning) return
    const r = returning
    await act(async () => {
      await call("kamra.laundry.return_items", {
        order: r.order.name,
        rows: Object.fromEntries(r.order.items.map((it) => [it.name, r.back[it.name] ?? it.returned_qty])),
      })
      await call("kamra.laundry.deliver_laundry", {
        order: r.order.name,
        shortage_note: returnPending > 0 ? r.note : null,
      })
      setReturning(null)
    })
  }

  const OrderCard = ({ o }: { o: LaundryOrder }) => (
    <li className="rounded-2xl border border-line bg-surface p-4 shadow-sm">
      <div className="flex items-center gap-3">
        <span className="text-2xl font-bold tabular-nums">{o.room_no}</span>
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium">{o.guest_name || o.name}</p>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5">
            <Badge tone={STATUS_TONE[o.status] || "zinc"}>{translate(o.status)}</Badge>
            {!!o.express && <Badge tone="amber"><Zap className="mr-0.5 size-3" />{translate("express")}</Badge>}
            {o.pieces > 0 && <Badge tone="zinc">{o.pieces}{translate(" pc")}</Badge>}
            {o.pending > 0 && o.status !== "Requested" && (
              <Badge tone="rose">{o.pending}{translate(" pending")}</Badge>
            )}
          </div>
        </div>
        {o.total > 0 && <span className="text-sm font-semibold tabular-nums">{cur()}{inr(o.total)}</span>}
      </div>
      {o.notes && <p className="mt-2 text-sm text-muted-ink">{o.notes}</p>}
      {o.shortage_note && (
        <p className="mt-2 rounded-lg bg-danger-subtle px-2.5 py-1.5 text-sm text-danger-ink">{translate("Shortage: ")}{o.shortage_note}
        </p>
      )}
      <div className="mt-3 flex gap-2">
        {o.status === "Requested" && (
          <button
            className="flex-1 rounded-xl bg-accent-fill py-2.5 text-sm font-semibold text-on-solid"
            disabled={busy}
            onClick={() => setCounting({ order: o.name, room: o.room, express: !!o.express, qty: {} })}>{translate("Collect & count")}</button>
        )}
        {o.status === "Collected" && (
          <button
            className="flex-1 rounded-xl bg-info-fill py-2.5 text-sm font-semibold text-on-solid"
            disabled={busy}
            onClick={() => act(() => call("kamra.laundry.laundry_status", { order: o.name, status: "In Process" }))}>{translate("Send to laundry")}</button>
        )}
        {o.status === "In Process" && (
          <button
            className="flex-1 rounded-xl bg-success-fill py-2.5 text-sm font-semibold text-on-solid"
            disabled={busy}
            onClick={() => act(() => call("kamra.laundry.laundry_status", { order: o.name, status: "Ready" }))}>{translate("Mark ready")}</button>
        )}
        {["Collected", "In Process", "Ready"].includes(o.status) && (
          <button
            className={cn("flex-1 rounded-xl border py-2.5 text-sm font-semibold",
              o.status === "Ready" ? "border-accent-control text-accent-ink" : "border-control text-secondary-ink")}
            disabled={busy}
            onClick={() => setReturning({ order: o, back: {}, note: "" })}>{translate("Return & deliver")}</button>
        )}
      </div>
    </li>
  )

  const open = board?.open ?? []
  const requested = open.filter((o) => o.status === "Requested")
  const inHand = open.filter((o) => o.status !== "Requested")

  return (
    <div className="space-y-4">
      {error && <p className="rounded-xl bg-danger-subtle px-3 py-2 text-sm text-danger-ink">{translate(error)}</p>}

      <div className="grid grid-cols-2 gap-2">
        <button
          className="rounded-xl border border-control bg-surface py-3 text-sm font-semibold text-secondary-ink"
          onClick={() => setPickup({ room: "", notes: "", express: false })}>{translate("Log pickup request")}</button>
        <button
          className="rounded-xl bg-accent-fill py-3 text-sm font-semibold text-on-solid"
          onClick={() => setCounting({ order: null, room: "", express: false, qty: {} })}>
          <Shirt className="mr-1 inline size-4" />{translate("Collect now")}</button>
      </div>

      {requested.length > 0 && (
        <>
          <p className="px-1 text-sm font-medium text-muted-ink">{translate("Pickup requests")}</p>
          <ul className="space-y-3">{requested.map((o) => <OrderCard key={o.name} o={o} />)}</ul>
        </>
      )}

      <p className="px-1 text-sm font-medium text-muted-ink">{translate("In hand (")}{inHand.length})
      </p>
      <ul className="space-y-3">
        {inHand.map((o) => <OrderCard key={o.name} o={o} />)}
        {inHand.length === 0 && (
          <li className="rounded-2xl border border-dashed border-control p-8 text-center text-muted-ink">{translate("No laundry out right now.")}</li>
        )}
      </ul>

      {(board?.recent ?? []).length > 0 && (
        <>
          <p className="px-1 text-sm font-medium text-muted-ink">{translate("Recent")}</p>
          <ul className="space-y-1">
            {(board?.recent ?? []).map((o) => (
              <li key={o.name} className="flex items-center justify-between rounded-xl bg-surface px-3 py-2 text-sm">
                <span className="text-secondary-ink">
                  {o.room_no} · {o.guest_name || o.name}
                  {o.shortage_note && <span className="ml-1 text-danger-text">!</span>}
                </span>
                <span className="flex items-center gap-2">
                  <Badge tone={STATUS_TONE[o.status] || "zinc"}>{translate(o.status)}</Badge>
                  {o.total > 0 && <span className="tabular-nums font-medium">{cur()}{inr(o.total)}</span>}
                </span>
              </li>
            ))}
          </ul>
        </>
      )}

      {/* pickup request sheet */}
      {pickup && (
        <Sheet onClose={() => setPickup(null)} title={translate("Laundry pickup request")}>
          <select
            className="w-full rounded-xl border border-control px-3 py-3 text-base"
            value={pickup.room}
            onChange={(e) => setPickup({ ...pickup, room: e.target.value })}>
            <option value="">{translate("Room…")}</option>
            {occupied.map((r) => <option key={r.name} value={r.name}>{translate("Room ")}{r.room_number}</option>)}
          </select>
          <input
            className="w-full rounded-xl border border-control px-3 py-3 text-base"
            placeholder={translate("Notes (bag on door, after 3pm…)")}
            value={pickup.notes}
            onChange={(e) => setPickup({ ...pickup, notes: e.target.value })} />
          <ExpressToggle value={pickup.express} onChange={(v) => setPickup({ ...pickup, express: v })} />
          <button
            className="w-full rounded-xl bg-accent-fill py-3 text-base font-semibold text-on-solid disabled:opacity-50"
            disabled={busy || !pickup.room}
            onClick={() => act(async () => {
              await call("kamra.laundry.request_pickup", {
                property, room: pickup.room, notes: pickup.notes || null,
                express: pickup.express ? 1 : 0,
              })
              setPickup(null)
            })}>{translate("Add to pickup queue")}</button>
        </Sheet>
      )}

      {/* count sheet */}
      {counting && (
        <Sheet onClose={() => setCounting(null)} title={translate("Count the bag")}>
          {!counting.order && (
            <select
              className="w-full rounded-xl border border-control px-3 py-3 text-base"
              value={counting.room}
              onChange={(e) => setCounting({ ...counting, room: e.target.value })}>
              <option value="">{translate("Room…")}</option>
              {occupied.map((r) => <option key={r.name} value={r.name}>{translate("Room ")}{r.room_number}</option>)}
            </select>
          )}
          <ExpressToggle value={counting.express} onChange={(v) => setCounting({ ...counting, express: v })} />
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {rates.map((r) => {
              const q = counting.qty[r.name] || 0
              const price = counting.express ? r.express_rate : r.rate
              return (
                <div key={r.name}
                  className={cn("flex items-center gap-2 rounded-xl border px-3 py-2",
                    q > 0 ? "border-accent-line-strong bg-accent-subtle" : "border-line")}>
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{r.item_name}</p>
                    <p className="text-xs text-muted-ink">{translate(r.service_type)} · {cur()}{inr(price)}</p>
                  </div>
                  <button className="rounded-lg border border-control p-1.5" aria-label={translate("less")}
                    onClick={() => setCounting({ ...counting, qty: { ...counting.qty, [r.name]: Math.max(0, q - 1) } })}>
                    <Minus className="size-4" />
                  </button>
                  <span className="w-6 text-center text-base font-bold tabular-nums">{q}</span>
                  <button className="rounded-lg border border-control p-1.5" aria-label={translate("more")}
                    onClick={() => setCounting({ ...counting, qty: { ...counting.qty, [r.name]: q + 1 } })}>
                    <Plus className="size-4" />
                  </button>
                </div>
              )
            })}
            {rates.length === 0 && (
              <p className="py-4 text-center text-sm text-muted-ink">{translate("No rate card yet - add laundry rates in Settings.")}</p>
            )}
          </div>
          <button
            className="w-full rounded-xl bg-accent-fill py-3 text-base font-semibold text-on-solid disabled:opacity-50"
            disabled={busy || countPieces === 0 || (!counting.order && !counting.room)}
            onClick={submitCollect}>{translate("Collect ")}{countPieces}{translate(" piece")}{countPieces === 1 ? "" : translate("s")} · {cur()}{inr(countTotal)}
          </button>
        </Sheet>
      )}

      {/* return & deliver sheet */}
      {returning && (
        <Sheet onClose={() => setReturning(null)} title={translate("Return to room {v0}", { v0: String(returning.order.room_no) })}>
          <button
            className="w-full rounded-xl border border-success-line-strong bg-success-subtle py-2.5 text-sm font-semibold text-success-ink"
            onClick={() => setReturning({
              ...returning,
              back: Object.fromEntries(returning.order.items.map((it) => [it.name, it.qty])),
            })}>{translate("Everything came back")}</button>
          <div className="max-h-72 space-y-1 overflow-y-auto">
            {returning.order.items.map((it) => {
              const back = returning.back[it.name] ?? it.returned_qty
              return (
                <div key={it.name} className="flex items-center gap-2 rounded-xl border border-line px-3 py-2">
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-sm font-medium">{it.item_name}</p>
                    <p className="text-xs text-muted-ink">{translate(it.service_type)}{translate(" · collected ")}{it.qty}</p>
                  </div>
                  <button className="rounded-lg border border-control p-1.5" aria-label={translate("less")}
                    onClick={() => setReturning({ ...returning, back: { ...returning.back, [it.name]: Math.max(0, back - 1) } })}>
                    <Minus className="size-4" />
                  </button>
                  <span className={cn("w-10 text-center text-base font-bold tabular-nums",
                    back < it.qty ? "text-danger-text" : "text-success-ink")}>
                    {back}/{it.qty}
                  </span>
                  <button className="rounded-lg border border-control p-1.5" aria-label={translate("more")}
                    onClick={() => setReturning({ ...returning, back: { ...returning.back, [it.name]: Math.min(it.qty, back + 1) } })}>
                    <Plus className="size-4" />
                  </button>
                </div>
              )
            })}
          </div>
          {returnPending > 0 && (
            <input
              className="w-full rounded-xl border border-danger-line-strong bg-danger-subtle px-3 py-3 text-base"
              placeholder={translate("{v0} piece(s) short - why? (required)", { v0: String(returnPending) })}
              value={returning.note}
              onChange={(e) => setReturning({ ...returning, note: e.target.value })} />
          )}
          <button
            className="w-full rounded-xl bg-accent-fill py-3 text-base font-semibold text-on-solid disabled:opacity-50"
            disabled={busy || (returnPending > 0 && !returning.note.trim())}
            onClick={submitDeliver}>
            <ChevronRight className="mr-1 inline size-4" />{translate("Deliver & bill ")}{cur()}{inr(returning.order.total)}
            {returnPending > 0 ? translate(" ({v0} short)", { v0: String(returnPending) }) : ""}
          </button>
        </Sheet>
      )}
    </div>
  )
}

function Sheet({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  useUILanguage()
  return (
    <div className="fixed inset-0 z-50 flex items-end bg-black/40" onClick={onClose}>
      <div className="max-h-[90vh] w-full space-y-3 overflow-y-auto rounded-t-2xl bg-surface p-4"
        onClick={(e) => e.stopPropagation()}>
        <h2 className="text-lg font-semibold">{translate(title)}</h2>
        {children}
      </div>
    </div>
  )
}
