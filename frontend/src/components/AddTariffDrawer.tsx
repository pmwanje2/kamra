import { useId, useRef, useState, type FormEvent } from "react"
import { call } from "../lib/api"
import { useT } from "../lib/i18n"
import { moneyStep } from "../lib/money"
import { roundRate, type ContractTariffItem, type PriceList, type RoundingMode } from "../lib/tariffMatrix"
import { currencyText, formatFxRate } from "./ContractTariff"
import { field, TariffModal, tariffReason } from "./TariffMassActions"
import { Button } from "./ui/button"

export interface TariffItemChoice {
  item_code: string
  item_name: string
  category?: ContractTariffItem["category"] | null
  room_type_id?: string | null
  can_link_to_room?: boolean
}

export interface TariffRoomChoice {
  name: string
  room_type_name: string
  room_type_code?: string | null
  base_occupancy?: number
  adults_capacity?: number
}

export interface TariffTarget {
  corporate?: boolean
  priceList?: string
  itemCode?: string
  roomType?: string
  occupancy?: string
  customer?: string | null
}

export type Guardrails = Record<string, ([number, number] | null)[]>

export function GuardrailPreview({ rails, room, date, until, amount, currency, locale }: {
  rails?: Guardrails; room?: string | null; date: string; until: string; amount: number | null; currency: string; locale: string
}) {
  const { t } = useT()
  const week = rails?.[room || ""] ?? rails?.[""]
  const start = Date.parse(date + "T00:00:00Z")
  const end = until ? Date.parse(until + "T00:00:00Z") : start + 6 * 86400000
  if (!week || amount == null || !Number.isFinite(start) || !Number.isFinite(end) || end < start) return null
  const first = new Date(start).getUTCDay()
  const days = Math.min(7, 1 + (end - start) / 86400000)
  const limits = week.filter((limit, weekday): limit is [number, number] =>
    limit != null && (weekday - first + 7) % 7 < days)
  if (!limits.length) return null
  const floor = Math.max(...limits.map(([min]) => min))
  const ceiling = Math.min(...limits.map(([, max]) => max))
  const outside = amount < floor || amount > ceiling
  return <p aria-live="polite" className={outside ? "text-warning-ink" : "text-secondary-ink"}>
    {outside && <strong>{t("Outside the guardrail for the proposed dates.")} </strong>}
    {t("Rate Guardrail")}: {currencyText(currency, amount, locale)} · {t("Floor price / night")}: {currencyText(currency, floor, locale)}
    {" · "}{t("Ceiling")}: {currencyText(currency, ceiling, locale)}
  </p>
}

interface Props {
  guardrails?: Guardrails
  target: TariffTarget
  lists: PriceList[]
  items: TariffItemChoice[]
  rooms: TariffRoomChoice[]
  customers: string[]
  property: string
  propertyCurrency: string
  businessDate: string
  locale: string
  rounding: (priceList: string) => { step: number; mode: RoundingMode } | null
  onClose: () => void
  onDone: () => void
}

const control = field + " w-full"
const label = "block space-y-1.5 text-sm font-medium"

const baseOccupancy = (room?: TariffRoomChoice) => Math.max(1, room?.base_occupancy || 2)

export function occupancyLimit(room?: TariffRoomChoice) {
  return Math.max(baseOccupancy(room), room?.adults_capacity || 5)
}

export function AddTariffDrawer({ guardrails, target, lists, items, rooms, customers, property, propertyCurrency, businessDate,
  locale, rounding, onClose, onDone }: Props) {
  const { t, tn } = useT()
  const customerList = useId()
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const [listName, setListName] = useState(target.priceList || lists[0]?.name || "")
  const [kind, setKind] = useState(target.roomType || !items.length ? "room" : "item")
  const [itemCode, setItemCode] = useState(target.itemCode || "")
  const [linkedItem, setLinkedItem] = useState("")
  const [query, setQuery] = useState("")
  const [roomType, setRoomType] = useState(target.roomType || "")
  const [occupancy, setOccupancy] = useState(target.occupancy ||
    String(baseOccupancy(rooms.find((room) => room.name === target.roomType))))
  const [customer, setCustomer] = useState(target.customer || "")
  const [amount, setAmount] = useState("")
  const [validFrom, setValidFrom] = useState(businessDate)
  const [validUntil, setValidUntil] = useState("")
  const list = lists.find((row) => row.name === listName)
  const item = items.find((row) => row.item_code === itemCode)
  const search = query.trim().toLowerCase()
  const choices = items.filter((row) => row.item_code === itemCode ||
    [row.item_code, row.item_name].some((value) => value.toLowerCase().includes(search)))
  const currency = list?.currency || ""
  const step = moneyStep(currency)
  const free = kind === "room" ? occupancy === "bed" : item?.category !== "Room"
  const value = Number(amount)
  const validAmount = amount.trim() !== "" && Number.isFinite(value) && (free ? value >= 0 : value > 0)
  const fx = list?.fx_rate
  const preview = validAmount && currency !== propertyCurrency && fx && fx > 0 ? value * fx : null
  const rule = rounding(listName)
  const capacity = occupancyLimit(rooms.find((room) => room.name === roomType))
  const tiers = Array.from({ length: capacity }, (_, index) => {
    const count = index + 1
    return [String(count), count === 1 ? t("Single (1 Adult)") : count === 2
      ? t("Double (2 Adults)") : tn(count, "{n} Adult", "{n} Adults")]
  })
  tiers.push(["bed", t("Extra bed")])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting.current || (target.corporate && !customer.trim()) || !list || !validAmount || !validFrom || validFrom < businessDate
      || (validUntil && validUntil < validFrom) || (kind === "room" ? !roomType : !item)) return
    submitting.current = true
    setBusy(true)
    setError("")
    try {
      const result = await call("kamra.rates.propose_contract_tariff", {
        property, price_list: list.name, price_list_rate: value, valid_from: validFrom, valid_upto: validUntil || null,
        ...(customer.trim() ? { customer: customer.trim() } : {}),
        ...(kind === "room" ? { item_code: linkedItem, room_type: roomType, occupancy_tier: occupancy } : { item_code: itemCode }),
      })
      if (result == null) setError(t("Tariff editing is not available for this property."))
      else onDone()
    } catch (cause) {
      setError(tariffReason(cause) || t("The tariff change could not be saved. Check the latest versions before retrying."))
    } finally {
      submitting.current = false
      setBusy(false)
    }
  }

  return <TariffModal drawer title={t("Propose contract tariff")} busy={busy} onClose={onClose} onSubmit={submit}
    footer={<>
      <Button type="button" variant="outline" disabled={busy} onClick={onClose}>{t("Cancel")}</Button>
      <Button type="submit" disabled={busy || !list || (kind === "room" ? !roomType : !item)}>
        {busy ? t("Saving…") : t("Submit for approval")}
      </Button>
    </>}>
    <label className={label}>
      <span>{t("Price list")}</span>
      <select name="price_list" className={control} value={listName} disabled={busy} autoFocus
        onChange={(event) => setListName(event.target.value)}>
        {lists.map((row) => <option key={row.name} value={row.name}>
          {t("{list} · {currency}", { list: row.name, currency: row.currency })}
        </option>)}
      </select>
    </label>
    <label className={label}>
      <span>{t("Tariff target")}</span>
      <select name="target" className={control} value={kind} disabled={busy} onChange={(event) => setKind(event.target.value)}>
        <option value="item">{t("Existing item")}</option>
        <option value="room">{t("Room type and occupancy")}</option>
      </select>
    </label>
    {kind === "item" ? <>
      <label className={label}>
        <span>{t("Search items")}</span>
        <input type="search" className={control} value={query} disabled={busy}
          onChange={(event) => setQuery(event.target.value)} />
      </label>
      <label className={label}>
        <span>{t("Item")}</span>
        <select name="item_code" className={control} value={itemCode} required disabled={busy}
          onChange={(event) => setItemCode(event.target.value)}>
          <option value="">{t("Choose an item")}</option>
          {choices.map((row) => <option key={row.item_code} value={row.item_code}>
            {row.item_name === row.item_code ? row.item_code : `${row.item_name} · ${row.item_code}`}
          </option>)}
        </select>
      </label>
    </> : <div className="grid gap-3 sm:grid-cols-2">
      <label className={label}>
        <span>{t("Room type")}</span>
        <select name="room_type" className={control} value={roomType} required disabled={busy}
          onChange={(event) => {
            const name = event.target.value
            setRoomType(name)
            setLinkedItem("")
            setOccupancy(String(baseOccupancy(rooms.find((room) => room.name === name))))
          }}>
          <option value="">{t("Choose a room type")}</option>
          {rooms.map((row) => <option key={row.name} value={row.name}>{row.room_type_name}</option>)}
        </select>
      </label>
      <label className={label}>
        <span>{t("Occupancy tier")}</span>
        <select name="occupancy_tier" className={control} value={occupancy} disabled={busy}
          onChange={(event) => {
            setOccupancy(event.target.value)
            setLinkedItem("")
          }}>
          {tiers.map(([code, text]) => <option value={code} key={code}>{text}</option>)}
        </select>
      </label>
      {items.some((row) => row.can_link_to_room) && <label className={label + " sm:col-span-2"}>
        <span>{t("Item to link (optional)")}</span>
        <select name="linked_item" className={control} disabled={busy} value={linkedItem}
          onChange={(event) => setLinkedItem(event.target.value)}>
          <option value="">{t("Use the linked item or create one")}</option>
          {items.filter((row) => row.can_link_to_room).map((row) => <option key={row.item_code} value={row.item_code}>
            {`${row.item_name} · ${row.item_code}`}
          </option>)}
        </select>
      </label>}
    </div>}
    <label className={label}>
      <span>{target.corporate ? t("Customer") : t("Corporate customer (optional)")}</span>
      <input name="customer" required={target.corporate} list={customerList} className={control} autoComplete="off" value={customer} disabled={busy}
        onChange={(event) => setCustomer(event.target.value)} />
      <datalist id={customerList}>{customers.map((name) => <option key={name} value={name} />)}</datalist>
      {!target.corporate && <span className="block text-xs font-normal text-secondary-ink">{t("Leave empty for the public rate.")}</span>}
    </label>
    <label className={label}>
      <span>{t("Rate")}</span>
      <span className="flex items-center gap-2">
        <span className="rounded-lg bg-subtle px-2 py-2 text-secondary-ink">{currency}</span>
        <input name="price_list_rate" type="number" inputMode="decimal" className={field + " min-w-0 flex-1 tabular-nums"}
          min={free ? "0" : step} step={step} required value={amount} disabled={busy}
          onChange={(event) => setAmount(event.target.value)} />
      </span>
    </label>
    {validAmount && <GuardrailPreview rails={guardrails} room={kind === "room" ? roomType : item?.room_type_id} date={validFrom} until={validUntil}
      amount={currency === propertyCurrency ? value : preview == null ? null : roundRate(preview, rule?.step ?? 0, rule?.mode, propertyCurrency)}
      currency={propertyCurrency} locale={locale} />}
    {preview != null && fx && <div aria-live="polite" className="rounded-lg border border-accent-line bg-accent-subtle p-3 text-sm">
      <p className="font-medium">{t("Local equivalent preview")}</p>
      <p className="tabular-nums">{t("{foreign_amount} ≈ {local_amount} at 1 {foreign} = {rate} {home}", {
        foreign_amount: currencyText(currency, value, locale),
        local_amount: currencyText(propertyCurrency, roundRate(preview, rule?.step ?? 0, rule?.mode, propertyCurrency), locale),
        foreign: currency, rate: formatFxRate(fx, locale), home: propertyCurrency,
      })}</p>
    </div>}
    <div className="grid gap-3 sm:grid-cols-2">
      <label className={label}>
        <span>{t("Effective from")}</span>
        <input name="valid_from" type="date" className={control} min={businessDate} value={validFrom} disabled={busy}
          required onChange={(event) => setValidFrom(event.target.value)} />
      </label>
      <label className={label}>
        <span>{t("Valid until (optional)")}</span>
        <input name="valid_upto" type="date" className={control} min={validFrom} value={validUntil} disabled={busy}
          onChange={(event) => setValidUntil(event.target.value)} />
      </label>
    </div>
    {error && <p role="alert" className="rounded-lg bg-danger-subtle p-3 text-sm text-danger-ink">{error}</p>}
  </TariffModal>
}
