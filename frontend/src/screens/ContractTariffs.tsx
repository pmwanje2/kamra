import { Fragment, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react"
import { ArrowRight, Check, ChevronDown, Clock3, Info, Plus, Search, X } from "lucide-react"
import { useSearchParams } from "react-router-dom"
import { currencyText, formatFxRate, tariffDate } from "../components/ContractTariff"
import { Button } from "../components/ui/button"
import { call, getCurrentProperty, serverError } from "../lib/api"
import { uiDateLocale, useT } from "../lib/i18n"
import { useLocale } from "../lib/money"
import { addDays, todayPlus } from "../lib/stay"

interface TariffVersion {
  name: string
  rate: number
  local_equiv?: number | null
  valid_from: string
  valid_upto?: string | null
  workflow_state: string
  owner?: string | null
  can_approve?: boolean
}

interface PriceList {
  name: string
  currency: string
  fx_rate?: number | null
  fx_date?: string | null
  rate_plans: string[]
}

interface ContractTariffItem {
  item_code: string
  item_name: string
  category: "Room" | "Supplement" | "Venue" | "Other"
  room_type?: string | null
  occupancy?: string | null
  price_list: string
  currency: string
  fx_rate?: number | null
  fx_date?: string | null
  active?: TariffVersion | null
  pending: TariffVersion[]
  history: TariffVersion[]
}

interface ContractTariffsResponse {
  supported: true
  property: string
  property_currency: string
  business_date: string
  can_propose: boolean
  can_approve: boolean
  price_lists: PriceList[]
  items: ContractTariffItem[]
}

type TariffsPayload = ContractTariffsResponse | {
  supported: false
  property: string
  can_propose: false
  can_approve: false
  price_lists: []
  items: []
}
type Category = ContractTariffItem["category"]
type Filter = "all" | "pending" | Category
type Decision = "approve" | "discard"
type TariffAction = { kind: "propose"; item: ContractTariffItem }
  | { kind: Decision; item: ContractTariffItem; draft: TariffVersion }

const categories: Category[] = ["Room", "Supplement", "Venue", "Other"]
const field = "min-h-10 rounded-lg border border-zinc-300 bg-white px-3 text-sm text-zinc-900 focus:border-brand-600 focus:outline-2 focus:outline-brand-600"
const cell = "flex items-baseline justify-between gap-2 px-3 py-1.5 xl:table-cell xl:py-3"
const pill = "inline-block rounded-full px-2.5 py-1 text-xs font-medium"
const decisionTone = {
  approve: "border-emerald-200 bg-emerald-50 text-emerald-800 hover:bg-emerald-100",
  discard: "border-rose-200 bg-rose-50 text-rose-800 hover:bg-rose-100",
}
const editMethods = {
  propose: "kamra.rates.propose_contract_tariff",
  approve: "kamra.rates.approve_contract_tariff",
  discard: "kamra.rates.discard_contract_tariff",
}
const drafts = (item: ContractTariffItem) => item.pending.filter((version) => version.workflow_state === "Draft")
const fxRate = (item: ContractTariffItem, list?: PriceList) => item.fx_rate ?? list?.fx_rate

export default function ContractTariffs() {
  const property = getCurrentProperty()
  return <TariffDirectory key={property} property={property} />
}

function TariffDirectory({ property }: { property: string }) {
  const { t, tn } = useT()
  const { locale } = useLocale()
  const [params, setParams] = useSearchParams()
  const query = params.get("q") ?? ""
  const [filter, setFilter] = useState<Filter>("all")
  const [listName, setListName] = useState<string | null>(() => query ? "" : null)
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<{ revision: number; data?: TariffsPayload }>()
  const [action, setAction] = useState<TariffAction | null>(null)
  const [notice, setNotice] = useState<TariffAction["kind"] | null>(null)

  useEffect(() => {
    if (!property) return
    let current = true
    call<TariffsPayload>("kamra.rates.list_contract_tariffs", { property })
      .then((data) => { if (current) setResult({ revision, data }) })
      .catch(() => { if (current) setResult({ revision }) })
    return () => { current = false }
  }, [property, revision])

  const data = result?.data
  const loading = !!property && result?.revision !== revision
  const reload = () => setRevision((value) => value + 1)
  const open = (next: TariffAction) => {
    setNotice(null)
    setAction(next)
  }
  const heading = <header className="space-y-1 border-b border-zinc-200 pb-4">
    <p className="text-sm font-medium text-brand-700">{data?.property || property}</p>
    <h1 className="text-2xl font-semibold tracking-tight text-zinc-950">{t("Contract tariffs")}</h1>
    <p className="text-sm text-zinc-600">{t("Manage effective-dated prices for rooms, supplements and venues.")}</p>
  </header>
  const statusToast = notice && <div role="status" className="fixed inset-x-4 bottom-4 z-40 flex items-center gap-3 rounded-xl border border-emerald-200 bg-white p-3 text-sm font-medium text-emerald-900 shadow-lg sm:start-auto sm:end-6 sm:bottom-6">
    <Check className="size-5 shrink-0" aria-hidden />
    {notice === "propose" ? t("Tariff version submitted for approval")
      : notice === "approve" ? t("Tariff version approved") : t("Tariff draft discarded")}
    <Button type="button" variant="ghost" onClick={() => setNotice(null)} aria-label={t("Dismiss")}><X className="size-4" /></Button>
  </div>
  if (!data?.supported) return <div className="space-y-5">
    {heading}
    {!property ? <p>{t("Choose a property to view its contract tariffs.")}</p>
      : loading ? <p role="status">{t("Loading contract tariffs…")}</p>
      : data ? <p>{t("Contract tariffs are not available for this property.")}</p>
      : <div className="flex flex-wrap items-center gap-3">
        <p role="alert">{t("Could not load contract tariffs.")}</p>
        <Button type="button" variant="outline" onClick={reload}>{t("Retry")}</Button>
      </div>}
    {statusToast}
  </div>

  const uiLocale = uiDateLocale(locale)
  const dateLocale = typeof uiLocale === "string" ? uiLocale : locale
  const lists = new Map(data.price_lists.map((list) => [list.name, list]))
  const selectedName = data.price_lists.length === 1 ? data.price_lists[0].name : listName ?? data.price_lists[0]?.name ?? ""
  const selected = lists.get(selectedName)
  const pendingCount = data.items.reduce((total, item) => total + drafts(item).length, 0)
  const needle = query.trim().toLowerCase()
  const groups = categories.map((category) => ({
    category,
    items: data.items.filter((item) => item.category === category
      && (filter === "pending" || !selected || item.price_list === selected.name)
      && (filter === "all" || (filter === "pending" ? drafts(item).length > 0 : item.category === filter))
      && [item.item_name, item.item_code, item.room_type, item.occupancy, item.price_list, item.currency]
        .some((value) => value?.toLowerCase().includes(needle))),
  })).filter((group) => group.items.length > 0)
  const labels = {
    Item: t("Item"), Category: t("Category"), Rate: t("Current rate"), Local: t("Local equiv."),
    Dates: t("Effective from"), Status: t("Status"), Actions: t("Actions"),
  }
  const groupNames: Record<Category, string> = {
    Room: t("Rooms"), Supplement: t("Supplements"), Venue: t("Venues / Conference"), Other: t("Other"),
  }
  const filterNames = { all: t("All"), ...groupNames, pending: t("Pending approval") }
  const showPending = () => {
    setFilter("pending")
    if (query) setParams((current) => { current.delete("q"); return current }, { replace: true })
  }

  return <div className="space-y-5">
    {heading}
    <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
      <div className="flex flex-wrap items-end justify-between gap-4 p-4">
        <label className="flex min-w-0 flex-1 basis-52 flex-col gap-1.5 text-sm font-medium">
          {t("Price list")}
          {data.price_lists.length > 1 ? <select className={field} value={filter === "pending" ? "" : selected?.name ?? ""}
            disabled={filter === "pending"} onChange={(event) => setListName(event.target.value)}>
            <option value="">{t("All price lists")}</option>
            {data.price_lists.map((list) => <option key={list.name} value={list.name}>
              {t("{list} · {currency}", { list: list.name, currency: list.currency })}
            </option>)}
          </select> : <span className="text-zinc-700">{selected?.name ?? "—"}</span>}
        </label>
        <label className="flex min-w-0 flex-1 basis-52 flex-col gap-1.5 text-sm font-medium">
          {t("Search tariffs")}
          <span className="relative">
            <Search className="pointer-events-none absolute start-3 top-3 size-4 text-zinc-500" aria-hidden />
            <input type="search" className={field + " w-full ps-9"} value={query}
              onChange={(event) => {
                const value = event.target.value
                setParams((current) => { if (value) current.set("q", value); else current.delete("q"); return current }, { replace: true })
              }} />
          </span>
        </label>
      </div>
      <div className="space-y-1.5 border-t border-zinc-200 bg-zinc-50 px-4 py-3 text-sm text-zinc-700">
        {filter !== "pending" && selected && selected.currency !== data.property_currency
          && <p className="font-medium text-brand-800 tabular-nums">
            {selected.fx_rate && selected.fx_date
              ? t("Published book rate: 1 {foreign} = {rate} {home} · {date}", {
                foreign: selected.currency, rate: formatFxRate(selected.fx_rate, locale), home: data.property_currency,
                date: tariffDate(selected.fx_date, dateLocale),
              })
              : t("No published book rate is available for this price list.")}
          </p>}
        {filter !== "pending" && !selected && data.price_lists.length > 1 &&
          <p>{t("Select a price list to see its published book rate.")}</p>}
        <p className="flex items-start gap-2">
          <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-brand-600" />
          {t("Released tariffs are never overwritten — new versions take effect from their effective date once approved.")}
        </p>
      </div>
    </div>

    {pendingCount > 0 && <button type="button" onClick={showPending}
      className="flex w-full flex-wrap items-center gap-2 rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-start text-sm font-semibold text-amber-900 hover:bg-amber-100 focus-visible:outline-2 focus-visible:outline-offset-2"
      aria-pressed={filter === "pending"}>
      <Clock3 aria-hidden className="size-4" />
      {tn(pendingCount, "{n} pending approval", "{n} pending approvals")}
      <ArrowRight aria-hidden className="ms-auto size-4 rtl:rotate-180" />
    </button>}

    <div className="flex flex-wrap gap-2" role="group" aria-label={t("Filter tariffs")}>
      {(["all", "Room", "Supplement", "Venue", "pending"] as const).map((value) =>
        <Button type="button" key={value} variant={filter === value ? "primary" : "outline"}
          aria-pressed={filter === value} onClick={() => setFilter(value)}>{filterNames[value]}</Button>)}
    </div>
    {loading && <p role="status" className="text-sm text-zinc-600">{t("Refreshing contract tariffs…")}</p>}
    {groups.length ? <div aria-busy={loading} className="xl:overflow-x-auto xl:rounded-xl xl:border xl:border-zinc-200 xl:bg-white">
      <table role="table" aria-label={t("Contract tariffs")} className="block w-full text-start text-sm xl:table">
        <thead className="hidden bg-zinc-50 xl:table-header-group">
          <tr>{Object.values(labels).map((label) => <th key={label} scope="col" className="px-3 py-3 text-start font-semibold">{label}</th>)}</tr>
        </thead>
        {groups.map(({ category, items }) => <tbody role="rowgroup" key={category} className="mb-4 block space-y-2 xl:table-row-group">
          <tr role="row" className="block xl:table-row">
            <th role="rowheader" scope="rowgroup" colSpan={7} className="block border-b border-zinc-200 bg-zinc-50 px-3 py-2 text-start text-sm font-semibold text-zinc-800 xl:table-cell">
              {groupNames[category]}
            </th>
          </tr>
          {items.map((item) => <TariffRow key={JSON.stringify([item.price_list, item.item_code])}
            {...{ item, data, labels, locale, dateLocale, loading, open }} list={lists.get(item.price_list)} />)}
        </tbody>)}
      </table>
    </div> : <p className="rounded-xl border border-zinc-200 bg-white p-5 text-sm text-zinc-600">
      {filter === "pending" && !needle ? t("No tariff versions are pending approval.") : t("No tariffs match these filters.")}
    </p>}
    {statusToast}
    {action && <ActionDialog action={action} data={data} locale={locale} dateLocale={dateLocale}
      list={lists.get(action.item.price_list)} onClose={() => setAction(null)} onSuccess={() => {
        setNotice(action.kind)
        setAction(null)
        reload()
      }} />}
  </div>
}

type Labels = Record<"Item" | "Category" | "Rate" | "Local" | "Dates" | "Status" | "Actions", string>

function TariffRow({ item, data, labels, locale, dateLocale, list, loading, open }: {
  item: ContractTariffItem; data: ContractTariffsResponse; labels: Labels; locale: string; dateLocale: string
  list?: PriceList; loading: boolean; open: (action: TariffAction) => void
}) {
  const { t } = useT()
  const [historyOpen, setHistoryOpen] = useState(false)
  const historyId = useId()
  const active = item.active
  const rate = fxRate(item, list)
  const local = active?.local_equiv ?? (active && rate && rate > 0 ? active.rate * rate : null)
  const subtitles = [item.item_code, item.room_type, item.occupancy].filter(Boolean)
  const validity = (version: TariffVersion) => version.valid_upto
    ? t("{from} – {until}", { from: tariffDate(version.valid_from, dateLocale), until: tariffDate(version.valid_upto, dateLocale) })
    : tariffDate(version.valid_from, dateLocale)

  return <Fragment>
    <tr role="row" data-item={item.item_code} className="block rounded-xl border border-zinc-200 bg-white py-1 xl:table-row xl:rounded-none xl:border-0 xl:border-t xl:align-top">
      <th role="rowheader" scope="row" className="block px-3 py-2 text-start xl:table-cell xl:py-3">
        <span className="block font-semibold text-zinc-950">{item.item_name}</span>
        <span className="block break-words text-xs font-normal text-zinc-600">{subtitles.join(" · ")}</span>
        <span className="mt-1 block break-words text-xs font-normal text-zinc-600">{item.price_list}</span>
      </th>
      <Cell label={labels.Category}><span className={pill + " bg-zinc-100 text-zinc-700"}>{t(item.category)}</span></Cell>
      <Cell label={labels.Rate}><span className="whitespace-nowrap font-semibold text-zinc-950 tabular-nums">
        {active ? currencyText(item.currency, active.rate, locale) : "—"}
      </span></Cell>
      <Cell label={labels.Local}><span className="whitespace-nowrap tabular-nums">
        {local == null || item.currency === data.property_currency ? "—" : t("≈ {amount}", { amount: currencyText(data.property_currency, local, locale) })}
      </span></Cell>
      <Cell label={labels.Dates}>{active ? validity(active) : "—"}</Cell>
      <Cell label={labels.Status}>
        <div className="flex flex-col items-end gap-2 xl:items-start">
          {active ? <span className={pill + " bg-emerald-50 text-emerald-800"}>{t("Approved")}</span>
            : <span className={pill + " bg-zinc-100 text-zinc-700"}>{t("No active tariff")}</span>}
          {drafts(item).map((draft) => <div key={draft.name} className="space-y-1.5">
            <span className={pill + " bg-amber-50 text-amber-900 tabular-nums"}>
              {t("Pending: {rate} from {date}", {
                rate: currencyText(item.currency, draft.rate, locale), date: validity(draft),
              })}
            </span>
            {data.can_approve && draft.can_approve !== false && <div className="flex flex-wrap justify-end gap-1.5 xl:justify-start">
              <Button type="button" variant="outline" className={decisionTone.approve} disabled={loading}
                onClick={() => open({ kind: "approve", item, draft })}>{t("Approve")}</Button>
              <Button type="button" variant="outline" className={decisionTone.discard} disabled={loading}
                onClick={() => open({ kind: "discard", item, draft })}>{t("Discard")}</Button>
            </div>}
          </div>)}
        </div>
      </Cell>
      <td role="cell" className="block px-3 py-2 xl:table-cell xl:py-3">
        <div className="flex flex-wrap gap-1.5">
          {data.can_propose && <Button type="button" variant="outline" className="whitespace-nowrap text-brand-700" disabled={loading}
            onClick={() => open({ kind: "propose", item })}><Plus aria-hidden className="size-4" />{t("New version")}</Button>}
          {item.history.length > 0 && <Button type="button" variant="ghost" aria-expanded={historyOpen}
            aria-controls={historyOpen ? historyId : undefined} onClick={() => setHistoryOpen(!historyOpen)}>
            <ChevronDown aria-hidden className={historyOpen ? "size-4 rotate-180" : "size-4"} />{t("History")}
          </Button>}
        </div>
      </td>
    </tr>
    {historyOpen && <tr role="row" id={historyId} className="block rounded-lg bg-zinc-50 xl:table-row">
      <td role="cell" colSpan={7} className="block px-4 py-3 xl:table-cell">
        <p className="mb-1 font-semibold">{t("Version history")}</p>
        <ul className="divide-y divide-zinc-200">
          {item.history.map((version) => <li key={version.name} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-1.5 text-zinc-700">
            <span className="font-medium tabular-nums">{currencyText(item.currency, version.rate, locale)}</span>
            <span>{validity(version)}</span>
            <span>{t(version.workflow_state)}</span>
          </li>)}
        </ul>
      </td>
    </tr>}
  </Fragment>
}

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return <td role="cell" className={cell}>
    <span className="shrink-0 text-xs font-medium text-zinc-600 xl:hidden">{label}</span>
    <div className="min-w-0 text-end xl:text-start">{children}</div>
  </td>
}

function ActionDialog({ action, data, locale, dateLocale, list, onClose, onSuccess }: {
  action: TariffAction; data: ContractTariffsResponse; locale: string; dateLocale: string
  list?: PriceList; onClose: () => void; onSuccess: () => void
}) {
  const { t } = useT()
  const dialog = useRef<HTMLDialogElement>(null)
  const submitting = useRef(false)
  const titleId = useId()
  const { item, kind } = action
  const minDate = item.active?.valid_from === data.business_date ? addDays(data.business_date, 1) : data.business_date
  const [amount, setAmount] = useState("")
  const [validFrom, setValidFrom] = useState(() => minDate > todayPlus(1) || item.active?.valid_from === data.business_date ? minDate : todayPlus(1))
  const [validUntil, setValidUntil] = useState("")
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<"absent" | "failed" | null>(null)
  const [reason, setReason] = useState("")

  useEffect(() => {
    const node = dialog.current
    const opener = document.activeElement
    node?.showModal()
    return () => {
      if (node?.open) node.close()
      if (opener instanceof HTMLElement && opener.isConnected) opener.focus()
    }
  }, [])

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting.current) return
    const value = Number(amount)
    if (action.kind === "propose" && (!Number.isFinite(value) || value <= 0 || !validFrom ||
      validFrom < minDate || (validUntil && validUntil < validFrom))) return
    submitting.current = true
    setBusy(true)
    setFailure(null)
    setReason("")
    try {
      const result = await call(editMethods[action.kind], action.kind === "propose" ? {
        property: data.property, price_list: item.price_list, item_code: item.item_code,
        price_list_rate: value, valid_from: validFrom, valid_upto: validUntil || null,
      } : { item_price: action.draft.name })
      if (result == null) setFailure("absent")
      else onSuccess()
    } catch (error) {
      const message = serverError(error)
      if (!/\b(?:ERPNext|Frappe|Kamra)\b/i.test(message)) setReason(message)
      setFailure("failed")
    } finally {
      submitting.current = false
      setBusy(false)
    }
  }

  const label = kind === "propose" ? t("Submit for approval") : kind === "approve" ? t("Approve") : t("Discard")
  const title = kind === "propose" ? t("New tariff version")
    : kind === "approve" ? t("Approve tariff version?") : t("Discard tariff draft?")
  const rate = fxRate(item, list)
  const showPreview = item.currency !== data.property_currency && rate != null && rate > 0
    && Number.isFinite(Number(amount)) && Number(amount) > 0
  return <dialog ref={dialog} aria-labelledby={titleId}
    onCancel={(event) => { event.preventDefault(); if (!submitting.current) onClose() }}
    className="m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-lg overflow-y-auto rounded-2xl border border-zinc-200 bg-white p-0 text-zinc-900 shadow-xl backdrop:bg-zinc-950/40">
    <form onSubmit={submit}>
      <header className="space-y-1 border-b border-zinc-200 px-5 py-4">
        <h2 id={titleId} className="text-lg font-semibold">{title}</h2>
        <p className="text-sm text-zinc-700">{item.item_name} · {item.price_list}</p>
      </header>
      <div className="space-y-4 px-5 py-4">
        {action.kind === "propose" ? <>
          <p className="text-sm text-zinc-600">{item.active
            ? t("Current rate: {rate}", { rate: currencyText(item.currency, item.active.rate, locale) })
            : t("No active tariff")}</p>
          <label className="block space-y-1.5 text-sm font-medium">
            <span>{t("New rate")}</span>
            <span className="flex items-center gap-2">
              <span className="rounded-lg bg-zinc-100 px-2 py-2 text-zinc-700">{item.currency}</span>
              <input type="number" inputMode="decimal" className={field + " min-w-0 flex-1 tabular-nums"} min="0.01" step="0.01"
                value={amount} onChange={(event) => setAmount(event.target.value)} required disabled={busy} autoFocus />
            </span>
          </label>
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
              {t("Effective from")}
              <input name="valid_from" type="date" className={field} min={minDate} value={validFrom}
                onChange={(event) => setValidFrom(event.target.value)} required disabled={busy} />
            </label>
            <label className="flex min-w-0 flex-col gap-1.5 text-sm font-medium">
              {t("Valid until (optional)")}
              <input name="valid_upto" type="date" className={field} min={validFrom} value={validUntil}
                onChange={(event) => setValidUntil(event.target.value)} disabled={busy} />
            </label>
          </div>
          {showPreview && <div className="rounded-lg border border-brand-200 bg-brand-50 p-3 text-sm" aria-live="polite">
            <p className="font-medium">{t("Local equivalent preview")}</p>
            <p className="tabular-nums">{t("{foreign_amount} ≈ {local_amount} at 1 {foreign} = {rate} {home}", {
              foreign_amount: currencyText(item.currency, Number(amount), locale),
              local_amount: currencyText(data.property_currency, Number(amount) * rate, locale),
              foreign: item.currency, rate: formatFxRate(rate, locale), home: data.property_currency,
            })}</p>
          </div>}
        </> : <>
          <p className="font-semibold tabular-nums">{t("{rate} from {date}", {
            rate: currencyText(item.currency, action.draft.rate, locale),
            date: tariffDate(action.draft.valid_from, dateLocale),
          })}</p>
          {action.draft.valid_upto && <p className="text-sm text-zinc-600">
            {t("Valid until {date}", { date: tariffDate(action.draft.valid_upto, dateLocale) })}
          </p>}
          <p className="text-sm text-zinc-700">{kind === "approve"
            ? t("This version will take effect on its effective date once approved.")
            : t("Discarding this draft cannot be undone. The released tariff will not change.")}</p>
        </>}
        {failure && <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-800">
          {failure === "absent" ? t("Tariff editing is not available for this property.")
            : reason || t("The tariff change could not be saved. Check the latest versions before retrying.")}
        </p>}
      </div>
      <footer className="flex flex-wrap justify-end gap-2 border-t border-zinc-200 bg-zinc-50 px-5 py-3">
        <Button type="button" variant="outline" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
        <Button type="submit" variant={kind === "propose" ? "primary" : "outline"}
          className={kind === "propose" ? undefined : decisionTone[kind]} disabled={busy}>
          {busy ? t("Saving…") : label}
        </Button>
      </footer>
    </form>
  </dialog>
}
