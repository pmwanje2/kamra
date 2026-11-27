import { Fragment, useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react"
import { Check, ChevronDown, Download, Info, Layers, Plus, Search, Upload, X } from "lucide-react"
import { Link, useSearchParams } from "react-router-dom"
import { AddTariffDrawer, GuardrailPreview, type Guardrails, occupancyLimit, type TariffTarget, type TariffItemChoice, type TariffRoomChoice } from "../components/AddTariffDrawer"
import { currencyText, formatFxRate, tariffDate, tariffDateTime } from "../components/ContractTariff"
import {
  DiscardReason, type ReasonChoice, decisionTone, field, itemCurrency, TariffMassDialog, TariffModal, tariffReason, type DraftChoice, type MassAction,
} from "../components/TariffMassActions"
import { Button } from "../components/ui/button"
import { call, frappeFetch, getCurrentProperty } from "../lib/api"
import { downloadCsv } from "../lib/csv"
import { uiDateLocale, useT } from "../lib/i18n"
import { moneyStep, useLocale } from "../lib/money"
import { addDays, todayPlus } from "../lib/stay"
import { notifyTariffsChanged, onTariffsChanged } from "../lib/tariffSync"
import {
  priceListRounding, roomMatrix, roundRate, type ContractTariffItem, type MatrixRow, type PriceList,
  type RatePlanPolicy, type RoundingMode, type TariffVersion,
} from "../lib/tariffMatrix"

interface UnpricedRoomType extends TariffRoomChoice {
  room_count: number
  price_lists: string[]
}

interface ContractTariffsResponse {
  supported: true
  property: string
  property_currency: string
  business_date: string
  can_propose: boolean
  can_approve: boolean
  /** a Hotel Admin with tariff access may discard any draft, their own included (kamra.rates) */
  can_discard?: boolean
  price_lists: PriceList[]
  items: ContractTariffItem[]
  rate_plan_policies?: Record<string, RatePlanPolicy>
  unpriced_room_types?: UnpricedRoomType[]
  reason_codes?: ReasonChoice[]
  guardrails?: Guardrails
  room_types?: TariffRoomChoice[]
  available_items?: TariffItemChoice[]
}

type TariffsPayload = ContractTariffsResponse | {
  supported: false
  property: string
  can_propose: false
  can_approve: false
  can_discard?: false
  price_lists: []
  items: []
}
type Category = ContractTariffItem["category"]
type Filter = Category | "pending"
type Decision = "approve" | "discard"
type TariffAction = { kind: "propose"; item: ContractTariffItem }
  | { kind: Decision; item: ContractTariffItem; draft: TariffVersion }
type Rounding = { step: number; mode: RoundingMode } | null
interface CsvExport { supported: boolean; business_date?: string | null; columns: string[]; rows: (string | number)[][] }

const PUBLIC = "__public__"
const cell = "flex items-baseline justify-between gap-2 px-3 py-1.5 xl:table-cell xl:py-3 xl:align-top"
const pill = "inline-block rounded-full px-2.5 py-1 text-xs font-medium"
const editMethods = {
  propose: "kamra.rates.propose_contract_tariff",
  approve: "kamra.rates.approve_contract_tariff",
  discard: "kamra.rates.discard_contract_tariff",
}
const drafts = (item: ContractTariffItem) => item.pending.filter((version) => version.workflow_state === "Draft")
const itemKey = (item: ContractTariffItem) => [item.price_list, item.item_code, item.customer ?? ""].join("|")
const fxRate = (item: ContractTariffItem, list?: PriceList) => item.fx_rate ?? list?.fx_rate
const scheduled = (item: ContractTariffItem, today: string) =>
  item.history.filter((version) => version.workflow_state === "Approved" && version.valid_from > today)

/** What the rows of the screen share: the directory, formatting and the selection. */
interface TariffView {
  data: ContractTariffsResponse
  lists: Map<string, PriceList>
  locale: string
  dateLocale: string
  loading: boolean
  /** the effective date the directory is read at: a version starting after it is Scheduled */
  asOf: string
  selectable: boolean
  selected: Set<string>
  toggle: (keys: string[], on: boolean) => void
  open: (action: TariffAction) => void
  add: (target: TariffTarget) => void
  rooms: Map<string, TariffRoomChoice>
  rounding: (priceList: string) => Rounding
  validity: (version: TariffVersion) => string
}

/** The home-currency figure a version books at: the published equivalent, rounded as the price list's plans round it. */
function localAmount(view: TariffView, item: ContractTariffItem, version?: TariffVersion | null) {
  const list = view.lists.get(item.price_list)
  if (!version || itemCurrency(item, list) === view.data.property_currency) return null
  const rate = fxRate(item, list)
  const raw = version.local_equiv ?? (rate && rate > 0 ? version.rate * rate : null)
  const rule = view.rounding(item.price_list)
  return raw == null ? null : rule ? roundRate(raw, rule.step, rule.mode, view.data.property_currency) : raw
}

export default function ContractTariffs() {
  const property = getCurrentProperty()
  return <TariffDirectory key={property} property={property} />
}

function TariffDirectory({ property }: { property: string }) {
  const { t, tn } = useT()
  const { locale } = useLocale()
  const [params, setParams] = useSearchParams()
  const query = params.get("q") ?? ""
  const [tab, setTab] = useState<{ value: Filter; query?: string }>({ value: "Room" })
  const listName = params.get("price_list")
  const customer = params.get("customer") ?? ""
  const changeFilter = (key: string, value: string) => {
    const next = new URLSearchParams(params)
    if (key === "customer" && !next.has("price_list")) next.set("price_list", selectedName)
    next.set(key, value)
    setParams(next, { replace: true })
  }
  const [asOf, setAsOf] = useState("")
  const [revision, setRevision] = useState(0)
  const [result, setResult] = useState<{ key: string; data?: TariffsPayload }>()
  const [action, setAction] = useState<TariffAction | null>(null)
  const [proposal, setProposal] = useState<TariffTarget | null>(null)
  const [mass, setMass] = useState<MassAction | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(() => new Set())
  const [exporting, setExporting] = useState(false)
  const [exportFailed, setExportFailed] = useState(false)
  const requestKey = `${revision}|${asOf}`

  useEffect(() => {
    if (!property) return
    let current = true
    const key = `${revision}|${asOf}`
    call<TariffsPayload>("kamra.rates.list_contract_tariffs", asOf ? { property, as_of_date: asOf } : { property })
      .then((data) => { if (current) setResult({ key, data }) })
      .catch(() => { if (current) setResult({ key }) })
    return () => { current = false }
  }, [property, revision, asOf])

  const data = result?.data
  const loading = !!property && result?.key !== requestKey
  // corporate / customer accounts a new customer-specific tariff may be for (SAP A305), as native DocPerm lets the user read them
  const [customerNames, setCustomerNames] = useState<string[]>([])
  const canPropose = !!data?.supported && data.can_propose
  useEffect(() => {
    if (!canPropose) return
    let current = true
    const query = new URLSearchParams({ fields: JSON.stringify(["name"]), filters: JSON.stringify([["disabled", "=", 0]]),
      limit_page_length: "500", order_by: "name asc" })
    frappeFetch<{ data?: { name?: string }[] }>(`/api/resource/Customer?${query}`)
      .then((out) => {
        if (current) setCustomerNames((out?.data ?? []).map((row) => row.name ?? "").filter(Boolean))
      })
      .catch(() => { /* no read access: the customers the directory already names are offered */ })
    return () => { current = false }
  }, [property, canPropose])
  const reload = () => setRevision((value) => value + 1)
  useEffect(() => onTariffsChanged(reload), [])
  const open = (next: TariffAction) => {
    setNotice(null)
    setAction(next)
  }
  const add = (target: TariffTarget) => {
    setNotice(null)
    setProposal(target)
  }
  const finish = (message: string) => {
    setNotice(message)
    setAction(null)
    setProposal(null)
    setMass(null)
    setSelected(new Set())
    notifyTariffsChanged(property)
  }
  const heading = <header className="space-y-1 border-b border-line pb-4">
    <p className="text-sm font-medium text-accent-ink">{data?.property || property}</p>
    <h1 className="text-2xl font-semibold tracking-tight text-ink">{t("Contract tariffs")}</h1>
    <p className="text-sm text-secondary-ink">{t("Manage effective-dated prices for rooms, supplements and venues.")}</p>
  </header>
  const statusToast = notice && <div role="status" className="fixed inset-x-4 bottom-4 z-40 flex items-center gap-3 rounded-xl border border-success-line bg-surface p-3 text-sm font-medium text-success-ink shadow-lg sm:start-auto sm:end-6 sm:bottom-6">
    <Check className="size-5 shrink-0" aria-hidden />
    {notice}
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

  const directory = data
  const uiLocale = uiDateLocale(locale)
  const dateLocale = typeof uiLocale === "string" ? uiLocale : locale
  const lists = new Map(directory.price_lists.map((list) => [list.name, list]))
  const policies = directory.rate_plan_policies ?? {}
  const selectedName = directory.price_lists.length === 1 ? directory.price_lists[0].name
    : listName ?? (query || customer ? "" : directory.price_lists[0]?.name ?? "")
  const selectedList = lists.get(selectedName)
  const customers = [...new Set(directory.items.map((item) => item.customer).filter((name): name is string => !!name))].sort()
  const customerOptions = [...new Set([...customers, ...customerNames])].sort()
  const needle = searchTokens(query)
  const linkedId = params.get("room_type")
  const linkedCode = params.get("room_type_code")
  const searchKey = params.toString()
  const normalizedQuery = needle.join(" ")
  const isLinkedRoom = (id?: string | null, code?: string | null) =>
    Boolean((linkedId && linkedId === id) || (linkedCode && linkedCode === code))
  const isExactRoom = (id?: string | null, code?: string | null, label?: string | null) =>
    isLinkedRoom(id, code) || (!(linkedId || linkedCode) && !!normalizedQuery
      && [id, code, label].some((value) => searchTokens(value ?? "").join(" ") === normalizedQuery))
  const searchMatches = (values: (string | null | undefined)[], id?: string | null, code?: string | null) => {
    if (!needle.length || isLinkedRoom(id, code)) return true
    const words = searchTokens(values.filter(Boolean).join(" "))
    return needle.every((token) => words.some((word) => word.includes(token)))
  }
  const matches = directory.items.filter((item) =>
    (!customer || (customer === PUBLIC ? !item.customer : item.customer === customer))
    && searchMatches([item.item_name, item.item_code, item.room_type, item.room_type_id, item.room_type_code,
      item.occupancy, item.price_list, item.customer, itemCurrency(item, lists.get(item.price_list))],
    item.room_type_id, item.room_type_code))
  const inList = matches.filter((item) => !selectedList || item.price_list === selectedList.name)
  const matrix = roomMatrix(inList.filter((item) => item.category === "Room"),
    directory.items.filter((item) => item.category === "Supplement"))
  const unpricedRooms = (directory.unpriced_room_types ?? []).filter((rt) =>
    (!selectedList || rt.price_lists.includes(selectedList.name))
    && searchMatches([rt.room_type_name, rt.room_type_code, rt.name], rt.name, rt.room_type_code),
  )
  const groups = {
    Supplement: inList.filter((item) => item.category === "Supplement"),
    Venue: inList.filter((item) => item.category === "Venue"),
    Other: inList.filter((item) => item.category === "Other"),
    pending: matches.filter((item) => drafts(item).length > 0),
  }
  const counts: Record<Filter, number> = {
    Room: matrix.length + unpricedRooms.length, Supplement: groups.Supplement.length, Venue: groups.Venue.length, Other: groups.Other.length,
    pending: groups.pending.reduce((sum, item) => sum + drafts(item).length, 0),
  }
  const tabs: [Filter, string][] = [["Room", t("Room matrix ({n})", { n: counts.Room })],
    ["Supplement", t("Supplements ({n})", { n: counts.Supplement })], ["Venue", t("Venues ({n})", { n: counts.Venue })],
    ["Other", t("Other ({n})", { n: counts.Other })], ["pending", t("Pending approval ({n})", { n: counts.pending })]]
  const exactItem = (item: ContractTariffItem) =>
    isExactRoom(item.room_type_id, item.room_type_code, item.room_type)
  const exactCategories = new Set<Filter>(inList.filter(exactItem).map((item) => item.category))
  if (!exactCategories.size) {
    if (unpricedRooms.some((room) => isExactRoom(room.name, room.room_type_code, room.room_type_name))) {
      exactCategories.add("Room")
    }
    if (groups.pending.some(exactItem)) exactCategories.add("pending")
  }
  const firstExact = tabs.find(([value]) => exactCategories.has(value))?.[0]
  const firstMatch = tabs.find(([value]) => counts[value] > 0)?.[0]
  let filter = tab.value
  if (needle.length && tab.query !== searchKey) {
    if (firstExact && !exactCategories.has(filter)) filter = firstExact
    else if (!counts[filter] && firstMatch) filter = firstMatch
  }
  const flat = filter === "Room" ? [] : groups[filter]
  const rooms = new Map((directory.room_types ?? []).map((type) => [type.name, type]))
  for (const type of directory.unpriced_room_types ?? []) if (!rooms.has(type.name)) rooms.set(type.name, type)
  for (const item of directory.items) {
    if (item.room_type_id && !rooms.has(item.room_type_id)) rooms.set(item.room_type_id, {
      name: item.room_type_id, room_type_name: item.room_type || item.room_type_id, room_type_code: item.room_type_code,
    })
  }
  const items = new Map<string, TariffItemChoice>(directory.items.map((item) => [item.item_code, item]))
  for (const item of directory.available_items ?? []) items.set(item.item_code, item)
  const visible = filter === "Room" ? matrix.flatMap((row) => [...row.tiers.values(), ...row.others]) : flat
  const chosen = visible.filter((item) => selected.has(itemKey(item)))
  const chosenDrafts: DraftChoice[] = chosen.flatMap((item) => drafts(item).map((draft) => ({ item, draft })))
  const allChosen = visible.length > 0 && chosen.length === visible.length
  const view: TariffView = {
    data: directory, lists, locale, dateLocale, loading, open, add, rooms, asOf: asOf || directory.business_date,
    selectable: directory.can_propose || directory.can_approve || !!directory.can_discard,
    selected,
    toggle: (keys, on) => setSelected((current) => {
      const next = new Set(current)
      for (const key of keys) {
        if (on) next.add(key)
        else next.delete(key)
      }
      return next
    }),
    rounding: (name) => priceListRounding(lists.get(name), policies),
    validity: (version) => version.valid_upto
      ? t("{from} – {until}", { from: tariffDate(version.valid_from, dateLocale), until: tariffDate(version.valid_upto, dateLocale) })
      : tariffDate(version.valid_from, dateLocale),
  }
  // a bulk version edits the selection's one tariff currency; a plan's rounding step rounds the posting
  // currency, so the plans' shared rule prefills only tariffs priced in it (never a foreign tariff)
  const bulkVersion = (items: ContractTariffItem[]): MassAction => {
    const currencies = new Set(items.map((item) => itemCurrency(item, lists.get(item.price_list))))
    const currency = currencies.size === 1 ? [...currencies][0] : null
    const rules = [...new Set(items.map((item) => item.price_list))].map((name) => view.rounding(name))
    const [rule] = rules
    const prefill = currency === directory.property_currency && !!rule && rule.step > 0
      && rules.every((other) => other?.step === rule.step && other.mode === rule.mode)
    return { kind: "version", items, currency, rounding: prefill ? rule : null }
  }
  const roundingText = (rule: Rounding) => {
    if (!rule) return ""
    if (!rule.step) return t("standard currency rounding")
    const step = rule.step.toLocaleString(locale)
    return rule.mode === "Up" ? t("rounded up to {step}", { step })
      : rule.mode === "Down" ? t("rounded down to {step}", { step }) : t("rounded to nearest {step}", { step })
  }
  // a plan's currency is its own, else its nearest base plan's, else the property's
  const planCurrency = (start: string) => {
    const seen = new Set<string>()
    for (let name = start; name && !seen.has(name);) {
      const plan = policies[name]
      if (!plan) break
      if (plan.currency) return plan.currency
      seen.add(name)
      name = plan.base_rate_plan ?? ""
    }
    return directory.property_currency
  }
  const planBadges = (selectedList?.rate_plans ?? []).map((name) => {
    const plan = policies[name]
    if (!plan) return { name, text: name }
    const base = plan.base_rate_plan ? policies[plan.base_rate_plan]?.label ?? plan.base_rate_plan : null
    return { name, text: [plan.label, planCurrency(name), t(plan.fx_lock_policy),
      base && t("Derived from {base}", { base })].filter(Boolean).join(" · ") }
  })
  const listRule = selectedList ? view.rounding(selectedList.name) : null
  const fxText = selectedList?.fx_rate && selectedList.fx_date ? [
    t("Governed posting FX: 1 {foreign} = {rate} {home} · {date}", {
      foreign: selectedList.currency, rate: formatFxRate(selectedList.fx_rate, locale), home: directory.property_currency,
      date: tariffDate(selectedList.fx_date, dateLocale),
    }),
    selectedList.workflow_state && t(selectedList.workflow_state),
    selectedList.stale_days != null && `${t("Age in days")}: ${selectedList.stale_days}`,
    listRule ? roundingText(listRule) : planBadges.length > 1 ? t("rounding set per rate plan") : "",
  ].filter(Boolean).join(" · ") : null
  const labels: Labels = {
    Select: t("Select"), Item: t("Item"), Category: t("Category"), Rate: t("Current rate"), Local: t("Local equiv."),
    Dates: t("Effective from"), Status: t("Status"), Actions: t("Actions"),
  }
  async function exportCsv() {
    setExporting(true)
    setExportFailed(false)
    try {
      // the file holds what the screen shows: its price list, customer and effective date
      const out = await call<CsvExport | null>("kamra.rates.export_contract_tariffs_csv", {
        property, ...(selectedList && filter !== "pending" ? { price_list: selectedList.name } : {}),
        ...(customer ? { customer } : {}), ...(asOf ? { as_of_date: asOf } : {}),
      })
      if (out?.supported) downloadCsv(`contract-tariffs-${out.business_date ?? directory.business_date}`, out.columns, out.rows)
      else setExportFailed(true)
    } catch {
      setExportFailed(true)
    } finally {
      setExporting(false)
    }
  }

  return <div className="space-y-5">
    {heading}
    <div className="overflow-hidden rounded-xl border border-line bg-surface">
      <div className="flex flex-wrap items-end gap-4 p-4">
        <label className="flex min-w-0 flex-1 basis-48 flex-col gap-1.5 text-sm font-medium">
          {t("Price list")}
          {directory.price_lists.length > 1 ? <select name="price_list" className={field} value={filter === "pending" ? "" : selectedList?.name ?? ""}
            disabled={filter === "pending"} onChange={(event) => changeFilter("price_list", event.target.value)}>
            <option value="">{t("All price lists")}</option>
            {directory.price_lists.map((list) => <option key={list.name} value={list.name}>
              {t("{list} · {currency}", { list: list.name, currency: list.currency })}
            </option>)}
          </select> : <span className="text-secondary-ink">{selectedList?.name ?? "—"}</span>}
        </label>
        <label className="flex min-w-0 flex-1 basis-40 flex-col gap-1.5 text-sm font-medium">
          {t("Customer")}
          <select name="customer" className={field} value={customer} onChange={(event) => changeFilter("customer", event.target.value)}>
            <option value="">{t("All customers")}</option>
            <option value={PUBLIC}>{t("Public (standard)")}</option>
            {customers.map((name) => <option key={name} value={name}>{name}</option>)}
          </select>
        </label>
        <label className="flex min-w-0 flex-1 basis-36 flex-col gap-1.5 text-sm font-medium">
          {t("Effective date")}
          <input name="as_of_date" type="date" className={field} value={asOf || directory.business_date}
            onChange={(event) => setAsOf(event.target.value === directory.business_date ? "" : event.target.value)} />
        </label>
        <label className="flex min-w-0 flex-1 basis-48 flex-col gap-1.5 text-sm font-medium">
          {t("Search tariffs")}
          <span className="relative">
            <Search className="pointer-events-none absolute start-3 top-3 size-4 text-muted-ink" aria-hidden />
            <input type="search" className={field + " w-full ps-9"} value={query}
              onChange={(event) => {
                const value = event.target.value
                setTab({ value: filter })
                const next = new URLSearchParams(params)
                if (value) next.set("q", value)
                else next.delete("q")
                next.delete("room_type")
                next.delete("room_type_code")
                setParams(next, { replace: true })
              }} />
          </span>
        </label>
      </div>
      <div className="space-y-2 border-t border-line bg-canvas px-4 py-3 text-sm text-secondary-ink">
        {filter !== "pending" && selectedList && selectedList.currency !== directory.property_currency
          && <p className="font-medium text-accent-ink tabular-nums">
            <Link to="/cashier/fx" className="underline">{fxText ?? t("No governed posting rate is available for this price list.")}{!fxText && selectedList.fx_status && ` · ${t(selectedList.fx_status)}`}</Link>
          </p>}
        {filter !== "pending" && planBadges.length > 0 && <ul className="flex flex-wrap gap-1.5" aria-label={t("Rate plans")}>
          {planBadges.map((badge) => <li key={badge.name} className={pill + " border border-accent-line bg-surface text-accent-ink"}><Link to={`/rate-plans?open=${encodeURIComponent(badge.name)}`}>{badge.text}</Link></li>)}
        </ul>}
        {filter !== "pending" && !selectedList && directory.price_lists.length > 1 &&
          <p>{t("Select a price list to see its governed posting rate.")}</p>}
        <p className="flex items-start gap-2">
          <Info aria-hidden className="mt-0.5 size-4 shrink-0 text-accent-text" />
          {t("Released tariffs are never overwritten — new versions take effect from their effective date once approved.")}
        </p>
      </div>
    </div>

    <div className="flex flex-wrap gap-2" role="group" aria-label={t("Filter tariffs")}>
      {tabs.map(([value, label]) =>
        <Button type="button" key={value} variant={filter === value ? "primary" : "outline"}
          aria-pressed={filter === value} onClick={() => setTab({ value, query: searchKey })}>{label}</Button>)}
    </div>

    <div role="toolbar" aria-label={t("Tariff actions")} className="flex flex-wrap items-center gap-2">
      {view.selectable && visible.length > 0 && <label className="flex items-center gap-2 text-sm font-medium">
        <input type="checkbox" className="size-4" aria-label={t("Select all")} checked={allChosen}
          onChange={(event) => view.toggle(visible.map(itemKey), event.target.checked)} />
        {chosen.length ? tn(chosen.length, "{n} tariff selected", "{n} tariffs selected") : t("Select all")}
      </label>}
      {directory.can_propose && chosen.length > 0 && <Button type="button" variant="outline" disabled={loading}
        onClick={() => setMass(bulkVersion(chosen))}><Layers aria-hidden className="size-4" />{t("Create new version in bulk")}</Button>}
      {directory.can_approve && chosenDrafts.some(({ draft }) => draft.can_approve !== false) &&
        <Button type="button" variant="outline" className={decisionTone.approve} disabled={loading}
          onClick={() => setMass({ kind: "approve", drafts: chosenDrafts })}>{t("Approve selected")}</Button>}
      {directory.can_discard && chosenDrafts.length > 0 &&
        <Button type="button" variant="outline" className={decisionTone.discard} disabled={loading}
          onClick={() => setMass({ kind: "discard", drafts: chosenDrafts })}>{t("Discard selected")}</Button>}
      <span className="ms-auto flex flex-wrap gap-2">
        <Button type="button" variant="outline" onClick={exportCsv} disabled={exporting}>
          <Download aria-hidden className="size-4" />{t("Export CSV")}
        </Button>
        {directory.can_propose && <Button type="button" variant="outline" onClick={() => { setNotice(null); setMass({ kind: "import" }) }}>
          <Upload aria-hidden className="size-4" />{t("Import CSV")}
        </Button>}
        {directory.can_propose && <Button type="button" disabled={loading || !directory.price_lists.length}
          onClick={() => add({ priceList: selectedList?.name, customer: customer === PUBLIC ? "" : customer })}>
          <Plus aria-hidden className="size-4" />{t("Add tariff")}
        </Button>}
      </span>
    </div>
    {exportFailed && <p role="alert" className="text-sm text-danger-ink">{t("The contract tariffs could not be exported.")}</p>}
    {loading && <p role="status" className="text-sm text-secondary-ink">{t("Refreshing contract tariffs…")}</p>}

    {filter === "Room" && matrix.length > 0 ? <RoomMatrix rows={matrix} view={view} />
      : filter !== "Room" && flat.length > 0 ? <TariffList items={flat} view={view} labels={labels} />
      : (filter !== "Room" || !unpricedRooms.length) && <p className="rounded-xl border border-line bg-surface p-5 text-sm text-secondary-ink">
        {filter === "pending" && !needle.length ? t("No tariff versions are pending approval.") : t("No tariffs match these filters.")}
      </p>}
    {filter === "Room" && unpricedRooms.length > 0 && (
      <section aria-label={t("Room types without a tariff")} className="space-y-3 rounded-xl border border-warning-line bg-warning-subtle/40 p-4">
        <div>
          <h2 className="text-sm font-semibold text-ink">{t("Room types without a tariff")}</h2>
          <p className="text-xs text-secondary-ink">{t("These room types have no contract tariff on this price list yet.")}</p>
        </div>
        <ul className="divide-y divide-warning-line/70 rounded-lg border border-warning-line/70 bg-surface">
          {unpricedRooms.map((rt) => (
            <li key={rt.name} className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{rt.room_type_name}</span>
                {rt.room_type_code && (
                  <span className="rounded bg-subtle px-1.5 py-0.5 text-xs font-medium text-secondary-ink">{rt.room_type_code}</span>
                )}
                {rt.room_count > 0 ? (
                  <span className="text-xs tabular-nums text-muted-ink">{tn(rt.room_count, "{n} room", "{n} rooms")}</span>
                ) : (
                  <span className="rounded-full bg-warning-muted px-2 py-0.5 text-xs font-medium text-warning-ink">{t("No rooms")}</span>
                )}
              </div>
              <div className="flex flex-wrap items-center gap-3">
                <Link to={`/room-types?open=${encodeURIComponent(rt.name)}`}
                  className="text-xs font-medium text-accent-ink hover:underline">{t("Open room type")}</Link>
                {directory.can_propose && <Button type="button" variant="outline" disabled={loading}
                  onClick={() => add({ roomType: rt.name, priceList: selectedList?.name || rt.price_lists[0],
                    customer: customer === PUBLIC ? "" : customer })}>{t("Set up tariff")}</Button>}
              </div>
            </li>
          ))}
        </ul>
      </section>
    )}
    {statusToast}
    {proposal && <AddTariffDrawer guardrails={directory.guardrails} target={proposal} lists={directory.price_lists} items={[...items.values()]}
      rooms={[...rooms.values()]} customers={customerOptions} property={directory.property}
      propertyCurrency={directory.property_currency} businessDate={directory.business_date}
      locale={locale} rounding={view.rounding} onClose={() => setProposal(null)}
      onDone={() => finish(t("Tariff version submitted for approval"))} />}
    {action && <ActionDialog action={action} data={directory} locale={locale} dateLocale={dateLocale}
      list={lists.get(action.item.price_list)} rounding={view.rounding(action.item.price_list)} customers={customerOptions}
      onClose={() => setAction(null)} onSuccess={() => finish(action.kind === "propose" ? t("Tariff version submitted for approval")
        : action.kind === "approve" ? t("Tariff version approved") : t("Tariff draft discarded"))} />}
    {mass && <TariffMassDialog reasonCodes={directory.reason_codes} action={mass} property={directory.property} propertyCurrency={directory.property_currency}
      businessDate={directory.business_date} customers={customerOptions}
      lists={lists} locale={locale} dateLocale={dateLocale} onClose={() => setMass(null)} onDone={finish} />}
  </div>
}

type Labels = Record<"Select" | "Item" | "Category" | "Rate" | "Local" | "Dates" | "Status" | "Actions", string>

function searchTokens(value: string) {
  return value.normalize("NFKD").toLowerCase().replace(/\p{M}/gu, "").match(/[\p{L}\p{N}]+/gu) ?? []
}

function RoomMatrix({ rows, view }: { rows: MatrixRow<ContractTariffItem>[]; view: TariffView }) {
  const { t, tn } = useT()
  const tiers = [...new Set([
    ...(view.data.can_propose ? [1, 2] : []), ...rows.flatMap((row) => [...row.tiers.keys()]),
  ])].sort((a, b) => a - b)
  const others = rows.some((row) => row.others.length > 0)
  const columns = 3 + tiers.length + (others ? 1 : 0) + (view.selectable ? 1 : 0)
  return <div aria-busy={view.loading} className="xl:overflow-x-auto xl:rounded-xl xl:border xl:border-line xl:bg-surface">
    <table role="table" aria-label={t("Room pricing matrix")} className="block w-full text-start text-sm xl:table">
      <thead className="hidden bg-canvas xl:table-header-group">
        <tr>
          {view.selectable && <th scope="col" className="w-10 px-3 py-3"><span className="sr-only">{t("Select")}</span></th>}
          <th scope="col" className="px-3 py-3 text-start font-semibold">{t("Room type")}</th>
          {tiers.map((n) => <th key={n} scope="col" className="px-3 py-3 text-start font-semibold">{tn(n, "{n} Adult", "{n} Adults")}</th>)}
          {others && <th scope="col" className="px-3 py-3 text-start font-semibold">{t("Other occupancy")}</th>}
          <th scope="col" className="px-3 py-3 text-start font-semibold">{t("Extras")}</th>
          <th scope="col" className="px-3 py-3 text-start font-semibold">{t("Actions")}</th>
        </tr>
      </thead>
      <tbody className="block space-y-2 xl:table-row-group">
        {rows.map((row) => <MatrixRowView key={row.key} row={row} tiers={tiers} others={others} columns={columns} view={view} />)}
      </tbody>
    </table>
  </div>
}

function MatrixRowView({ row, tiers, others, columns, view }: {
  row: MatrixRow<ContractTariffItem>; tiers: number[]; others: boolean; columns: number; view: TariffView
}) {
  const { t, tn } = useT()
  const [historyOpen, setHistoryOpen] = useState(false)
  const historyId = useId()
  const items = [...row.tiers.values(), ...row.others]
  const keys = items.map(itemKey)
  const roomType = items.find((item) => item.room_type_id)?.room_type_id
    || [...(view.data.room_types ?? [])].find((type) => type.room_type_name === row.roomType)?.name
  const name = [row.roomType, row.priceList, row.customer].filter(Boolean).join(" · ")
  const withHistory = items.filter((item) => item.history.length > 0)
  // a customer's negotiated price (SAP customer-specific condition) reads apart from the public one
  const scope = row.customer
    ? "border-s-2 border-s-indigo-500 bg-indigo-subtle/25 xl:border-s-2"
    : "bg-surface"
  return <Fragment>
    <tr role="row" data-room={row.key} data-scope={row.customer ? "corporate" : "public"}
      className={"block rounded-xl border border-line py-1 xl:table-row xl:rounded-none xl:border-0 xl:border-t " + scope}>
      {view.selectable && <td role="cell" className="block px-3 pt-2 xl:table-cell xl:py-3 xl:align-top">
        <input type="checkbox" className="size-4" aria-label={t("Select {name}", { name })}
          checked={keys.every((key) => view.selected.has(key))} onChange={(event) => view.toggle(keys, event.target.checked)} />
      </td>}
      <th role="rowheader" scope="row" className="block px-3 py-2 text-start xl:table-cell xl:py-3 xl:align-top">
        <Link className="block font-semibold text-accent-ink" to={`/room-types?open=${encodeURIComponent(roomType || row.roomType)}`}>{row.roomType}</Link>
        <span className="block break-words text-xs font-normal text-secondary-ink">{row.priceList}</span>
        {row.customer
          ? <span data-scope-badge className={pill + " mt-1 inline-flex max-w-full items-center gap-1 border border-indigo-line bg-indigo-subtle text-indigo-ink"}>
            <span className="font-semibold">{t("Corporate")}</span>
            <span aria-hidden>·</span>
            <Link className="break-words" to={`/companies?q=${encodeURIComponent(row.customer)}`}>{row.customer}</Link>
          </span>
          : <span data-scope-badge className={pill + " mt-1 bg-subtle text-secondary-ink"}>{t("Public")}</span>}
      </th>
      {tiers.map((n) => {
        const item = row.tiers.get(n)
        return <Cell key={n} label={tn(n, "{n} Adult", "{n} Adults")}>
          {item ? <TierCell item={item} view={view} /> : roomType && view.rooms.has(roomType)
            && view.data.can_propose && n <= occupancyLimit(view.rooms.get(roomType))
            ? <Button type="button" variant="outline" disabled={view.loading}
                onClick={() => view.add({ roomType, occupancy: String(n), priceList: row.priceList, customer: row.customer })}>
                <Plus aria-hidden className="size-4" />{t("Set rate")}
              </Button> : "—"}
        </Cell>
      })}
      {others && <Cell label={t("Other occupancy")}>{row.others.length ? <div className="space-y-3">
        {row.others.map((item) => <div key={item.item_code}>
          <span className="block text-xs font-medium text-secondary-ink">{item.occupancy || item.item_name}</span>
          <TierCell item={item} view={view} />
        </div>)}
      </div> : "—"}</Cell>}
      <Cell label={t("Extras")}><Extras row={row} view={view} /></Cell>
      <td role="cell" className="block px-3 py-2 xl:table-cell xl:py-3 xl:align-top">
        <Button disabled={!withHistory.length} type="button" variant="ghost" aria-expanded={historyOpen}
          aria-controls={historyOpen ? historyId : undefined} onClick={() => setHistoryOpen(!historyOpen)}>
          <ChevronDown aria-hidden className={historyOpen ? "size-4 rotate-180" : "size-4"} />{t("History")}{!withHistory.length && " (0)"}
        </Button>
        {view.data.can_propose && <Button variant="ghost" onClick={() => view.add({ roomType: roomType || row.roomType,
          priceList: row.priceList, customer: row.customer, corporate: true })}>+ {t("Corporate")}</Button>}
        <Link className="block text-accent-ink" to={`/room-types?open=${encodeURIComponent(roomType || row.roomType)}`}>{t("Open room type")}</Link>
        {view.lists.get(row.priceList)?.rate_plans?.map(plan => <Link key={plan} className="block text-accent-ink"
          to={`/rate-calendar?rate_plan=${encodeURIComponent(plan)}`}>{t("Rate calendar")} · {plan}</Link>)}
      </td>
    </tr>
    {historyOpen && <tr role="row" id={historyId} className="block rounded-lg bg-canvas xl:table-row">
      <td role="cell" colSpan={columns} className="block space-y-2 px-4 py-3 xl:table-cell">
        <p className="font-semibold">{t("Version history")}</p>
        {withHistory.map((item) => <div key={item.item_code}>
          <p className="text-xs font-medium text-secondary-ink">{item.item_name}</p>
          <History item={item} view={view} />
        </div>)}
      </td>
    </tr>}
  </Fragment>
}

function TierCell({ item, view }: { item: ContractTariffItem; view: TariffView }) {
  const { t } = useT()
  const active = item.active
  const local = localAmount(view, item, active)
  return <div data-item={item.item_code} className="flex flex-col items-end gap-1 xl:items-start">
    <span className="whitespace-nowrap font-semibold text-ink tabular-nums">
      {active ? currencyText(itemCurrency(item, view.lists.get(item.price_list)), active.rate, view.locale) : "—"}
    </span>
    {local != null && <span className="whitespace-nowrap text-xs text-secondary-ink tabular-nums">
      {t("≈ {amount}", { amount: currencyText(view.data.property_currency, local, view.locale) })}
    </span>}
    {active && <span className="text-xs text-secondary-ink">{view.validity(active)}</span>}
    <Versions item={item} view={view} />
    {view.data.can_propose && <Button type="button" variant="outline" className="whitespace-nowrap text-accent-ink"
      disabled={view.loading} onClick={() => active ? view.open({ kind: "propose", item })
        : view.add({ itemCode: item.item_code, priceList: item.price_list, customer: item.customer })}>
      <Plus aria-hidden className="size-4" />{active ? t("New version") : t("Set rate")}
    </Button>}
  </div>
}

/** Approved, scheduled and in-review versions of one tariff, with its draft decisions. */
function Versions({ item, view }: { item: ContractTariffItem; view: TariffView }) {
  const { t } = useT()
  const currency = itemCurrency(item, view.lists.get(item.price_list))
  const upcoming = scheduled(item, view.asOf)
  const pending = drafts(item)
  return <div className="flex flex-col items-end gap-1.5 xl:items-start">
    {item.active ? <span className={pill + " bg-success-subtle text-success-ink"}>{t("Approved")}</span>
      : !upcoming.length && !pending.length && <span className={pill + " bg-subtle text-secondary-ink"}>{t("No active tariff")}</span>}
    {upcoming.map((version) => <span key={version.name} className={pill + " bg-info-subtle text-info-ink tabular-nums"}>
      {t("Scheduled: {rate} from {date}", { rate: currencyText(currency, version.rate, view.locale), date: view.validity(version) })}
    </span>)}
    {pending.map((draft) => <div key={draft.name} className="space-y-1.5">
      <span className={pill + " bg-warning-subtle text-warning-ink tabular-nums"}>
        {t("In review: {rate} from {date}", { rate: currencyText(currency, draft.rate, view.locale), date: view.validity(draft) })}
      </span>
      <VersionAudit version={draft} view={view} />
      {/* a maker never approves their own draft (can_approve false), but may discard it */}
      {((view.data.can_approve && draft.can_approve !== false) || view.data.can_discard) &&
        <div className="flex flex-wrap justify-end gap-1.5 xl:justify-start">
          {view.data.can_approve && draft.can_approve !== false &&
            <Button type="button" variant="outline" className={decisionTone.approve} disabled={view.loading}
              onClick={() => view.open({ kind: "approve", item, draft })}>{t("Approve")}</Button>}
          {view.data.can_discard &&
            <Button type="button" variant="outline" className={decisionTone.discard} disabled={view.loading}
              onClick={() => view.open({ kind: "discard", item, draft })}>{t("Discard")}</Button>}
        </div>}
    </div>)}
  </div>
}

function Extras({ row, view }: { row: MatrixRow<ContractTariffItem>; view: TariffView }) {
  const { t } = useT()
  const lines = (["adult", "child", "bed"] as const).flatMap((kind) => {
    const item = row.extras[kind]
    if (!item) return []
    const rate = item.active ? currencyText(itemCurrency(item, view.lists.get(item.price_list)), item.active.rate, view.locale) : "—"
    const label = kind === "adult" ? t("Extra adult {rate}", { rate }) : kind === "child" ? t("Extra child {rate}", { rate })
      : t("Extra bed {rate}", { rate })
    return [{ item, label }]
  })
  return lines.length ? <ul className="space-y-0.5 text-xs text-secondary-ink tabular-nums">
    {lines.map(({ item, label }) => <li key={item.item_code}>
      {view.data.can_propose ? <button type="button" className="text-accent-ink underline" onClick={() => view.open({ kind: "propose", item })}>{label}</button> : label}
      {drafts(item).length > 0 && <span className="ms-1 rounded bg-warning-subtle">{t("Draft")} ({drafts(item).length})</span>}
    </li>)}
  </ul> : <span>—</span>
}

function History({ item, view }: { item: ContractTariffItem; view: TariffView }) {
  const { t } = useT()
  const currency = itemCurrency(item, view.lists.get(item.price_list))
  return <ul className="divide-y divide-line">
    {item.history.map((version) => <li key={version.name} className="flex flex-wrap items-baseline gap-x-4 gap-y-1 py-1.5 text-secondary-ink">
      <span className="font-medium tabular-nums">{currencyText(currency, version.rate, view.locale)}</span>
      <span>{view.validity(version)}</span>
      <span>{t(version.workflow_state)}</span>
      <VersionAudit version={version} view={view} />
    </li>)}
  </ul>
}

/** Who proposed a version and who released or rejected it, and when (SAP condition change log, OPERA rate
 *  history). An integration that answers none of it shows nothing. */
function VersionAudit({ version, view }: { version: TariffVersion; view: TariffView }) {
  const { t } = useT()
  const when = (value?: string | null) => value ? <> · {tariffDateTime(value, view.dateLocale)}</> : null
  // an older integration has no approver: the last editor decided, unless that is the proposer
  const decider = version.approver || (version.modified_by && version.modified_by !== version.owner ? version.modified_by : null)
  const lines: ReactNode[] = []
  if (version.owner) lines.push(<span key="proposed">{t("Proposed by {user}", { user: version.owner })}{when(version.creation)}</span>)
  if (decider && version.workflow_state === "Approved")
    lines.push(<span key="approved">{t("Approved by {user}", { user: decider })}{when(version.modified)}</span>)
  if (decider && version.workflow_state === "Rejected")
    lines.push(<span key="rejected">{t("Rejected by {user}", { user: decider })}{when(version.modified)}</span>)
  if (version.rejection_reason)
    lines.push(<span key="reason">{t("Reason: {reason}", { reason: version.rejection_reason })}</span>)
  return lines.length ? <span className="flex basis-full flex-col text-xs text-secondary-ink">{lines}</span> : null
}

function TariffList({ items, view, labels }: { items: ContractTariffItem[]; view: TariffView; labels: Labels }) {
  const { t } = useT()
  const headers = view.selectable ? Object.values(labels) : Object.values(labels).slice(1)
  return <div aria-busy={view.loading} className="xl:overflow-x-auto xl:rounded-xl xl:border xl:border-line xl:bg-surface">
    <table role="table" aria-label={t("Contract tariffs")} className="block w-full text-start text-sm xl:table">
      <thead className="hidden bg-canvas xl:table-header-group">
        <tr>{headers.map((label, index) => <th key={label} scope="col" className="px-3 py-3 text-start font-semibold">
          {view.selectable && index === 0 ? <span className="sr-only">{label}</span> : label}
        </th>)}</tr>
      </thead>
      <tbody className="block space-y-2 xl:table-row-group">
        {items.map((item) => <TariffRow key={itemKey(item)} item={item} view={view} labels={labels} columns={headers.length} />)}
      </tbody>
    </table>
  </div>
}

function TariffRow({ item, view, labels, columns }: { item: ContractTariffItem; view: TariffView; labels: Labels; columns: number }) {
  const { t } = useT()
  const [historyOpen, setHistoryOpen] = useState(false)
  const historyId = useId()
  const active = item.active
  const local = localAmount(view, item, active)
  const subtitles = [item.item_code, item.room_type, item.occupancy].filter(Boolean)
  const key = itemKey(item)
  return <Fragment>
    <tr role="row" data-item={item.item_code} className="block rounded-xl border border-line bg-surface py-1 xl:table-row xl:rounded-none xl:border-0 xl:border-t xl:align-top">
      {view.selectable && <td role="cell" className="block px-3 pt-2 xl:table-cell xl:py-3">
        <input type="checkbox" className="size-4" checked={view.selected.has(key)}
          aria-label={t("Select {name}", { name: [item.item_name, item.price_list, item.customer].filter(Boolean).join(" · ") })}
          onChange={(event) => view.toggle([key], event.target.checked)} />
      </td>}
      <th role="rowheader" scope="row" className="block px-3 py-2 text-start xl:table-cell xl:py-3">
        <span className="block font-semibold text-ink">{item.item_name}</span>
        <span className="block break-words text-xs font-normal text-secondary-ink">{subtitles.join(" · ")}</span>
        <span className="mt-1 block break-words text-xs font-normal text-secondary-ink">{item.price_list}</span>
        {item.customer && <span className={pill + " mt-1 bg-accent-subtle text-accent-ink"}>{item.customer}</span>}
      </th>
      <Cell label={labels.Category}><span className={pill + " bg-subtle text-secondary-ink"}>{t(item.category)}</span></Cell>
      <Cell label={labels.Rate}><span className="whitespace-nowrap font-semibold text-ink tabular-nums">
        {active ? currencyText(itemCurrency(item, view.lists.get(item.price_list)), active.rate, view.locale) : "—"}
      </span></Cell>
      <Cell label={labels.Local}><span className="whitespace-nowrap tabular-nums">
        {local == null ? "—" : t("≈ {amount}", { amount: currencyText(view.data.property_currency, local, view.locale) })}
      </span></Cell>
      <Cell label={labels.Dates}>{active ? view.validity(active) : "—"}</Cell>
      <Cell label={labels.Status}><Versions item={item} view={view} /></Cell>
      <td role="cell" className="block px-3 py-2 xl:table-cell xl:py-3">
        <div className="flex flex-wrap gap-1.5">
          {view.data.can_propose && <Button type="button" variant="outline" className="whitespace-nowrap text-accent-ink" disabled={view.loading}
            onClick={() => view.open({ kind: "propose", item })}><Plus aria-hidden className="size-4" />{t("New version")}</Button>}
          {item.history.length > 0 && <Button type="button" variant="ghost" aria-expanded={historyOpen}
            aria-controls={historyOpen ? historyId : undefined} onClick={() => setHistoryOpen(!historyOpen)}>
            <ChevronDown aria-hidden className={historyOpen ? "size-4 rotate-180" : "size-4"} />{t("History")}
          </Button>}
        </div>
      </td>
    </tr>
    {historyOpen && <tr role="row" id={historyId} className="block rounded-lg bg-canvas xl:table-row">
      <td role="cell" colSpan={columns} className="block px-4 py-3 xl:table-cell">
        <p className="mb-1 font-semibold">{t("Version history")}</p>
        <History item={item} view={view} />
      </td>
    </tr>}
  </Fragment>
}

function Cell({ label, children }: { label: string; children: ReactNode }) {
  return <td role="cell" className={cell}>
    <span className="shrink-0 text-xs font-medium text-secondary-ink xl:hidden">{label}</span>
    <div className="min-w-0 text-end xl:text-start">{children}</div>
  </td>
}

function ActionDialog({ action, data, locale, dateLocale, list, rounding, customers, onClose, onSuccess }: {
  action: TariffAction; data: ContractTariffsResponse; locale: string; dateLocale: string
  list?: PriceList; rounding: Rounding; customers: string[]; onClose: () => void; onSuccess: () => void
}) {
  const { t } = useT()
  const submitting = useRef(false)
  const customerList = useId()
  const { item, kind } = action
  const [discardReason, setDiscardReason] = useState("")
  const currency = itemCurrency(item, list)
  // a supplement, venue or other line may be complimentary (OPERA 0.00); a room rate never is
  const free = item.category !== "Room"
  const [customer, setCustomer] = useState("")
  // a customer row's version is that customer's own price; a public row's may be a new customer's first (SAP A305)
  const target = item.customer || customer.trim()
  // a customer's first price follows no version of its own (as the bulk dialog has it)
  const fresh = !item.customer && target !== ""
  const minDate = !fresh && item.active?.valid_from === data.business_date ? addDays(data.business_date, 1)
    : data.business_date
  const [amount, setAmount] = useState("")
  const [validFrom, setValidFrom] = useState(() => minDate > todayPlus(1) || item.active?.valid_from === data.business_date ? minDate : todayPlus(1))
  const [validUntil, setValidUntil] = useState("")
  const [busy, setBusy] = useState(false)
  const [failure, setFailure] = useState<"absent" | "failed" | null>(null)
  const [reason, setReason] = useState("")

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (submitting.current) return
    const value = Number(amount)
    if (action.kind === "propose" && (amount.trim() === "" || !Number.isFinite(value) || (free ? value < 0 : value <= 0)
      || !validFrom || validFrom < minDate || (validUntil && validUntil < validFrom))) return
    submitting.current = true
    setBusy(true)
    setFailure(null)
    setReason("")
    try {
      const result = await call(editMethods[action.kind], action.kind === "propose" ? {
        property: data.property, price_list: item.price_list, item_code: item.item_code,
        price_list_rate: value, valid_from: validFrom, valid_upto: validUntil || null,
        // a customer's version is that customer's own price, never the public rate
        ...(target ? { customer: target } : {}),
      } : { item_price: action.draft.name, ...(kind === "discard" && discardReason.trim() ? { reason: discardReason.trim() } : {}) })
      if (result == null) setFailure("absent")
      else onSuccess()
    } catch (error) {
      setReason(tariffReason(error))
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
  const showPreview = currency !== data.property_currency && rate != null && rate > 0
    && Number.isFinite(Number(amount)) && Number(amount) > 0
  const local = showPreview ? Number(amount) * rate : 0
  const subtitle = <>
    {item.item_name} · {item.price_list}
    {target && <> · <strong className="font-semibold text-accent-ink">
      {t("Customer rate: {customer}", { customer: target })}</strong></>}
  </>
  return <TariffModal title={title} subtitle={subtitle} busy={busy} onClose={onClose} onSubmit={submit}
    footer={<>
      <Button type="button" variant="outline" onClick={onClose} disabled={busy}>{t("Cancel")}</Button>
      <Button type="submit" variant={kind === "propose" ? "primary" : "outline"}
        className={kind === "propose" ? undefined : decisionTone[kind]} disabled={busy}>
        {busy ? t("Saving…") : label}
      </Button>
    </>}>
    {action.kind === "propose" ? <>
      <p className="text-sm text-secondary-ink">{item.active
        ? t("Current rate: {rate}", { rate: currencyText(currency, item.active.rate, locale) })
        : t("No active tariff")}</p>
      {!item.customer && <label className="block space-y-1.5 text-sm font-medium">
        <span>{t("Customer (optional)")}</span>
        <input name="customer" list={customerList} autoComplete="off" className={field + " w-full"} value={customer}
          onChange={(event) => setCustomer(event.target.value)} disabled={busy} />
        <datalist id={customerList}>{customers.map((name) => <option key={name} value={name} />)}</datalist>
        <span className="block text-xs font-normal text-secondary-ink">{t("Leave empty for the public rate.")}</span>
      </label>}
      <label className="block space-y-1.5 text-sm font-medium">
        <span>{t("New rate")}</span>
        <span className="flex items-center gap-2">
          <span className="rounded-lg bg-subtle px-2 py-2 text-secondary-ink">{currency}</span>
          <input type="number" inputMode="decimal" className={field + " min-w-0 flex-1 tabular-nums"} min={free ? "0" : moneyStep(currency)} step={moneyStep(currency)}
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
      {amount.trim() && <GuardrailPreview rails={data.guardrails} room={item.room_type_id} date={validFrom}
        amount={currency === data.property_currency ? Number(amount) : !showPreview ? null :
          roundRate(local, rounding?.step ?? 0, rounding?.mode, data.property_currency)}
        currency={data.property_currency} locale={locale} />}
      {showPreview && rate != null && <div className="rounded-lg border border-accent-line bg-accent-subtle p-3 text-sm" aria-live="polite">
        <p className="font-medium">{t("Local equivalent preview")}</p>
        <p className="tabular-nums">{t("{foreign_amount} ≈ {local_amount} at 1 {foreign} = {rate} {home}", {
          foreign_amount: currencyText(currency, Number(amount), locale),
          local_amount: currencyText(data.property_currency,
            rounding ? roundRate(local, rounding.step, rounding.mode, data.property_currency) : local, locale),
          foreign: currency, rate: formatFxRate(rate, locale), home: data.property_currency,
        })}</p>
      </div>}
    </> : <>
      <p className="font-semibold tabular-nums">{t("{rate} from {date}", {
        rate: currencyText(currency, action.draft.rate, locale),
        date: tariffDate(action.draft.valid_from, dateLocale),
      })}</p>
      {kind === "discard" && <DiscardReason codes={data.reason_codes} value={discardReason} onChange={setDiscardReason} />}
      {action.draft.valid_upto && <p className="text-sm text-secondary-ink">
        {t("Valid until {date}", { date: tariffDate(action.draft.valid_upto, dateLocale) })}
      </p>}
      <p className="text-sm text-secondary-ink">{kind === "approve"
        ? t("This version will take effect on its effective date once approved.")
        : t("Discarding this draft cannot be undone. The released tariff will not change.")}</p>
    </>}
    {failure && <p role="alert" className="rounded-lg bg-danger-subtle p-3 text-sm text-danger-ink">
      {failure === "absent" ? t("Tariff editing is not available for this property.")
        : reason || t("The tariff change could not be saved. Check the latest versions before retrying.")}
    </p>}
  </TariffModal>
}
