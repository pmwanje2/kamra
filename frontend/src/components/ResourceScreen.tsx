import {
  useCallback,
  useEffect,
  useState,
  Suspense,
  type ComponentType,
  type ReactNode,
} from "react"
import { useSearchParams } from "react-router-dom"
import { ArrowDown, ArrowUp, Columns3, Download, Plus, Search, Trash2 } from "lucide-react"
import { Sheet } from "./ui/sheet"
import { call, getCurrentProperty } from "../lib/api"
import { providerLabel } from "../lib/brand"
import {
  createResource,
  deleteResource,
  listResource,
  serverError,
  updateResource,
  type Row,
} from "../lib/resource"
import { frappeFetch } from "../lib/api"
import { Badge } from "./ui/badge"
import { Button } from "./ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "./ui/card"
import ImageField from "./ImageField"
import { fmtAmount } from "../lib/money"
import { useT, t as translate, useUILanguage } from "../lib/i18n"
import { useAuth } from "../lib/auth"
import { downloadCsv } from "../lib/csv"

const BACKDATE_ROLES = ["Finance", "Hotel Admin", "System Manager", "Administrator"]

function isoToday() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}


/** Keys from kamra.settings.menu_masters - property-editable master lists. */
export type MenuMasterKey =
  | "allergens"
  | "prep_stations"
  | "courses"
  | "categories"

/** Options loaded from a doctype: the value stored, the label shown. */
export interface OptionSource {
  linkDoctype: string
  /** field stored/filtered on (default: the record name) */
  valueField?: string
  /** field shown (default: the value) */
  labelField?: string
  propertyScoped?: boolean
  orderBy?: string
}

/** The Floor master as an option source: floor names in the master's order. */
export const floorSource: OptionSource = {
  linkDoctype: "Floor",
  valueField: "floor_name",
  propertyScoped: true,
  orderBy: "sort_order asc, floor_name asc",
}

export type FilterSpec = {
  field: string
  label: string
  /** static options */
  options?: string[]
} & Partial<Omit<OptionSource, "linkDoctype">> & { linkDoctype?: string; optionsFrom?: "Floor" }

type Opt = { value: string; label: string }

function sourceOf(f: FilterSpec): OptionSource | null {
  if (f.optionsFrom === "Floor") return floorSource
  return f.linkDoctype ? (f as OptionSource) : null
}

function loadOptions(src: OptionSource): Promise<Opt[]> {
  const vf = src.valueField ?? "name"
  const lf = src.labelField ?? vf
  return listResource(src.linkDoctype, {
    fields: Array.from(new Set([vf, lf])),
    filters: src.propertyScoped ? [["property", "=", getCurrentProperty()]] : undefined,
    limit: 500,
    orderBy: src.orderBy ?? `${lf} asc`,
  }).then((r) =>
    r.map((x) => ({ value: String(x[vf] ?? ""), label: String(x[lf] ?? x[vf] ?? "") })),
  )
}

export interface FieldSpec {
  field: string
  label: string
  type:
    | "data"
    | "int"
    | "float"
    | "currency"
    | "select"
    | "check"
    | "date"
    | "link"
    | "readonly"
    | "password"
    | "image"
    | "tags"
    | "table"
  options?: string[] // for select / tags (static fallback)
  /** table: a child table's columns, in order */
  columns?: TableColumn[]
  /** table: rows are tried in order - they can be moved up and down */
  orderable?: boolean
  /** Load options from Settings → F&B → Menu master lists */
  optionsFrom?: MenuMasterKey
  /** select options loaded from a doctype (e.g. the property's floors) */
  source?: OptionSource
  /** with `source`: offer "Add a floor" to type a value not in the list yet */
  allowNew?: string
  linkDoctype?: string // for link
  /** link options only from the current property (the target has a `property` field) */
  linkPropertyScoped?: boolean
  /** extra Frappe filters for a link field's option query (e.g. enabled currencies only) */
  linkFilters?: (string | number)[][]
  /** max options loaded for a link field (default 500) */
  linkLimit?: number
  /** extra values that must always stay selectable in a link dropdown */
  ensureOptions?: (draft: Record<string, unknown>) => string[]
  /** override the value rendered in the input (e.g. inherited currency on derived plans) */
  displayValue?: (draft: Record<string, unknown>) => unknown
  required?: boolean
  hint?: string // for image: recommended size/format
  /** dynamic hint text (overrides static `hint` when returned) */
  hintWhen?: (draft: Record<string, unknown>) => string | undefined
  dependsOn?: (draft: Record<string, unknown>) => boolean
  /** Lock the field on existing records (e.g. code primary keys). */
  readOnlyOnEdit?: boolean
  /** Disable a field dynamically (e.g. disabling a system master code). */
  disabledWhen?: (draft: Record<string, unknown>) => boolean
  /** Initial value when creating a new record. */
  default?: unknown
  /** Block dates before today unless the user may backdate (Finance / Hotel Admin). */
  notPast?: boolean
}

/** One column of a child table edited in the form (FieldSpec type "table"). */
export interface TableColumn {
  field: string
  label: string
  type: "data" | "select"
  options?: string[]
  /** a new row's value */
  default?: string
}

export interface ScreenConfig {
  doctype: string
  title: string
  description?: string
  columns: {
    field: string
    label: string
    badge?: boolean
    /** Map a Check field (0/1) to Active / Disabled badges. */
    statusCheck?: boolean
    /** Map a Check field (0/1) to explicit badge labels (e.g. System / Custom). */
    booleanBadge?: { truthy: string; falsy?: string }
  }[]
  form: FieldSpec[]
  formNote?: string
  propertyScoped?: boolean
  /** Show records belonging to the current property plus shared (blank property) records;
   *  newly created records are scoped to the current property. */
  propertyOrShared?: boolean
  allowCreate?: boolean
  allowDelete?: boolean
  /** Row-level delete guard (e.g. hide Delete for system master rows). */
  canDelete?: (row: Record<string, unknown>) => boolean
  orderBy?: string
  /** Fields searched (LIKE) by the search box. Adds a search input when set. */
  searchFields?: string[]
  /** Dropdown filters shown in the toolbar (e.g. status). */
  filters?: FilterSpec[]
  /** Rows per page (adds pagination when set). */
  pageSize?: number
  /** Date-range filter on this date field (adds From/To pickers). */
  dateFilter?: { field: string; label: string }
  /** Custom section rendered in the drawer below the form (existing rows only). */
  extra?: ComponentType<{ row: Row; reload: () => void }>
  /** Replace the generic edit form with a bespoke detail panel (existing rows).
   *  When set, the drawer opens wide and the panel owns its own actions. */
  detailPanel?: ComponentType<{
    row: Row
    reload: () => void
    onClose: () => void
  }>
}

const inputCls =
  "w-full rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600 " +
  "disabled:bg-zinc-50 disabled:text-zinc-400"

function parseTags(value: unknown): string[] {
  if (Array.isArray(value))
    return value.map((x) => String(x).trim()).filter(Boolean)
  return String(value ?? "")
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean)
}

function TagsInput(props: {
  value: unknown
  onChange: (v: string) => void
  options: string[]
  t: (s: string) => string
}) {
  const selected = parseTags(props.value)
  const available = props.options.filter(
    (o) => !selected.some((s) => s.toLowerCase() === o.toLowerCase()),
  )
  const toggle = (tag: string) => {
    const next = selected.some((s) => s.toLowerCase() === tag.toLowerCase())
      ? selected.filter((s) => s.toLowerCase() !== tag.toLowerCase())
      : [...selected, tag]
    props.onChange(next.join(", "))
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {selected.length === 0 && (
          <span className="text-xs text-zinc-400">{props.t("None selected")}</span>
        )}
        {selected.map((tag) => (
          <button
            key={tag}
            type="button"
            onClick={() => toggle(tag)}
            className="inline-flex items-center gap-1 rounded-full border border-brand-200 bg-brand-50 px-2.5 py-0.5 text-xs font-medium text-brand-800 hover:bg-brand-100"
          >
            {props.t(tag)}
            <span aria-hidden className="text-brand-500">×</span>
          </button>
        ))}
      </div>
      {available.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {available.map((tag) => (
            <button
              key={tag}
              type="button"
              onClick={() => toggle(tag)}
              className="rounded-full border border-zinc-200 bg-white px-2.5 py-0.5 text-xs text-zinc-600 hover:border-zinc-300 hover:bg-zinc-50"
            >
              + {props.t(tag)}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/** A select whose options come from a doctype; allowNew adds a typed value. */
function SourceSelect(props: {
  spec: FieldSpec
  value: unknown
  onChange: (v: unknown) => void
  t: (s: string) => string
}) {
  const { spec, value, onChange, t } = props
  const [opts, setOpts] = useState<Opt[]>([])
  const [adding, setAdding] = useState(false)
  useEffect(() => {
    loadOptions(spec.source!).then(setOpts).catch(() => setOpts([]))
  }, [spec.source])
  const cur = String(value ?? "")
  if (adding)
    return (
      <div className="flex gap-2">
        <input
          autoFocus
          className={inputCls}
          value={cur}
          placeholder={t(spec.allowNew ?? "")}
          onChange={(e) => onChange(e.target.value)}
        />
        <button
          type="button"
          className="text-xs text-zinc-500 hover:text-zinc-700"
          onClick={() => setAdding(false)}
        >
          {t("Choose from list")}
        </button>
      </div>
    )
  return (
    <select
      className={inputCls}
      value={cur}
      onChange={(e) => {
        if (e.target.value === NEW) {
          onChange("")
          setAdding(true)
        } else onChange(e.target.value)
      }}
    >
      <option value="">-</option>
      {cur && !opts.some((o) => o.value === cur) && <option value={cur}>{cur}</option>}
      {opts.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
      {spec.allowNew && <option value={NEW}>{t(spec.allowNew)}</option>}
    </select>
  )
}
const NEW = "__new__"

type TableRow = Record<string, unknown>

/** A child table edited inside the form: a row per record, its columns as
 *  inputs, rows added, removed and (when orderable) moved. The form's Save
 *  sends the whole table back with the record. */
function TableField(props: {
  spec: FieldSpec
  value: unknown
  onChange: (v: TableRow[]) => void
  t: (s: string) => string
}) {
  const { spec, onChange, t } = props
  const rows = Array.isArray(props.value) ? (props.value as TableRow[]) : []
  const columns = spec.columns ?? []
  const cell = "w-full rounded-md border border-zinc-300 bg-white px-2 py-1 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"
  const icon = "rounded-md p-1 text-zinc-500 hover:bg-zinc-100 hover:text-zinc-700 disabled:opacity-30"
  const update = (i: number, field: string, v: string) =>
    onChange(rows.map((row, j) => (j === i ? { ...row, [field]: v } : row)))
  const move = (i: number, by: number) => {
    const next = [...rows]
    const [row] = next.splice(i, 1)
    next.splice(i + by, 0, row)
    onChange(next)
  }
  return (
    <fieldset className="space-y-2 sm:col-span-2">
      <legend className="mb-1.5 text-sm font-medium text-zinc-600">{t(spec.label)}</legend>
      {spec.hint && <p className="text-xs leading-relaxed text-zinc-400">{t(spec.hint)}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
              {columns.map((c) => <th key={c.field} className="py-1 pr-2">{t(c.label)}</th>)}
              <th className="py-1" />
            </tr>
          </thead>
          <tbody className="divide-y divide-zinc-100">
            {rows.map((row, i) => (
              <tr key={String(row.name ?? `new-${i}`)}>
                {columns.map((c) => (
                  <td key={c.field} className="py-1 pr-2">
                    {c.type === "select" ? (
                      <select className={cell} aria-label={t(c.label)} value={String(row[c.field] ?? "")}
                        onChange={(e) => update(i, c.field, e.target.value)}>
                        <option value="">-</option>
                        {(c.options ?? []).map((o) => <option key={o} value={o}>{t(o)}</option>)}
                      </select>
                    ) : (
                      <input className={cell} aria-label={t(c.label)} value={String(row[c.field] ?? "")}
                        onChange={(e) => update(i, c.field, e.target.value)} />
                    )}
                  </td>
                ))}
                <td className="whitespace-nowrap py-1 text-right">
                  {spec.orderable && (
                    <>
                      <button type="button" className={icon} aria-label={t("Move up")} title={t("Move up")}
                        disabled={i === 0} onClick={() => move(i, -1)}>
                        <ArrowUp className="size-4" aria-hidden />
                      </button>
                      <button type="button" className={icon} aria-label={t("Move down")} title={t("Move down")}
                        disabled={i === rows.length - 1} onClick={() => move(i, 1)}>
                        <ArrowDown className="size-4" aria-hidden />
                      </button>
                    </>
                  )}
                  <button type="button" className={icon} aria-label={t("Remove")} title={t("Remove")}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}>
                    <Trash2 className="size-4" aria-hidden />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {rows.length === 0 && <p className="text-xs text-zinc-400">{t("Nothing here yet.")}</p>}
      <Button type="button" variant="outline"
        onClick={() => onChange([...rows, Object.fromEntries(columns.map((c) => [c.field, c.default ?? ""]))])}>
        <Plus className="size-4" aria-hidden />
        {t("Add row")}
      </Button>
    </fieldset>
  )
}

/** A child table as the record's save sends it: each row's columns, its
 *  name when it is a stored row, and its place (idx) as shown. */
function tableRows(spec: FieldSpec, value: unknown): TableRow[] {
  return (Array.isArray(value) ? (value as TableRow[]) : []).map((row, i) => ({
    ...(row.name ? { name: row.name } : {}),
    idx: i + 1,
    ...Object.fromEntries((spec.columns ?? []).map((c) => [c.field, row[c.field] ?? ""])),
  }))
}

function LinkSelect(props: {
  spec: FieldSpec
  value: unknown
  onChange: (v: unknown) => void
  baseOpts: string[]
  extraOpts: string[]
  capped?: boolean
  disabled?: boolean
  t: (s: string) => string
}) {
  const { spec, value, onChange, baseOpts, extraOpts, capped, disabled, t } = props
  const [query, setQuery] = useState("")
  const [searched, setSearched] = useState<string[]>([])

  useEffect(() => {
    const trimmed = query.trim()
    if (!capped || !spec.linkDoctype || !trimmed) {
      setSearched([])
      return
    }
    let active = true
    const timer = setTimeout(() => {
      const filters: (string | number)[][] = []
      if (spec.linkPropertyScoped) filters.push(["property", "=", getCurrentProperty()])
      if (spec.linkFilters?.length) filters.push(...spec.linkFilters)
      filters.push(["name", "like", `%${trimmed}%`])
      listResource(spec.linkDoctype!, {
        fields: ["name"],
        filters,
        limit: 50,
        orderBy: "name asc",
      })
        .then((r) => {
          if (active) setSearched(r.map((x) => x.name))
        })
        .catch(() => {})
    }, 120)
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [capped, query, spec.linkDoctype, spec.linkPropertyScoped, spec.linkFilters])

  const current = String(value ?? "")
  const opts = Array.from(
    new Set([...baseOpts, ...searched, ...extraOpts, ...(current ? [current] : [])].filter(Boolean)),
  ).sort()

  return (
    <div className={capped && !disabled ? "space-y-1.5" : undefined}>
      {capped && !disabled && (
        <input
          type="search"
          className={inputCls}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("Search…")}
        />
      )}
      <select
        className={inputCls}
        value={current}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">-</option>
        {opts.map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </div>
  )
}

function FieldInput(props: {
  spec: FieldSpec
  value: unknown
  onChange: (v: unknown) => void
  linkOptions: Record<string, string[]>
  linkCapped?: Record<string, boolean>
  extraLinkOptions?: string[]
  masterOptions: Partial<Record<MenuMasterKey, string[]>>
  /** Translate display labels; API values stay English. */
  t: (s: string) => string
  canBackdate?: boolean
  disabled?: boolean
}) {
  useUILanguage()
  const { spec, value, onChange, t, canBackdate, disabled } = props
  const masterOpts =
    (spec.optionsFrom && props.masterOptions[spec.optionsFrom]) ||
    spec.options ||
    []
  switch (spec.type) {
    case "tags":
      return (
        <TagsInput
          value={value}
          onChange={(v) => onChange(v)}
          options={masterOpts}
          t={t}
        />
      )
    case "select":
      if (spec.source)
        return (
          <SourceSelect spec={spec} value={value} onChange={onChange} t={t} />
        )
      return (
        <select
          className={inputCls}
          value={String(value ?? "")}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">-</option>
          {masterOpts.map((o) => (
            <option key={o} value={o}>
              {t(spec.field === "provider" ? providerLabel(o) : o)}
            </option>
          ))}
        </select>
      )
    case "link":
      return (
        <LinkSelect
          spec={spec}
          value={value}
          onChange={onChange}
          baseOpts={props.linkOptions[spec.field] ?? []}
          extraOpts={props.extraLinkOptions ?? []}
          capped={Boolean(props.linkCapped?.[spec.field])}
          disabled={disabled}
          t={t}
        />
      )
    case "check":
      return (
        <input
          type="checkbox"
          className="size-4 accent-brand-600 disabled:cursor-not-allowed disabled:opacity-50"
          checked={Boolean(Number(value ?? 0))}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked ? 1 : 0)}
        />
      )
    case "date":
      return (
        <input
          type="date"
          className={inputCls}
          value={String(value ?? "")}
          disabled={disabled}
          min={spec.notPast && !canBackdate ? isoToday() : undefined}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    case "int":
    case "float":
    case "currency":
      return (
        <input
          type="number"
          className={inputCls}
          value={value === null || value === undefined ? "" : Number(value)}
          disabled={disabled}
          onChange={(e) =>
            onChange(e.target.value === "" ? null : Number(e.target.value))
          }
        />
      )
    case "readonly":
      return (
        <input className={inputCls} disabled value={String(value ?? "")} />
      )
    case "password":
      // a stored secret is never shown nor sent back: always empty on open
      return (
        <input
          type="password"
          autoComplete="new-password"
          className={inputCls}
          value={String(value ?? "")}
          disabled={disabled}
          onChange={(e) => onChange(e.target.value)}
        />
      )
    case "image":
      return (
        <ImageField
          hint={t(spec.hint || "JPG/PNG/WebP · under 1 MB")}
          value={String(value ?? "")}
          onChange={(url) => onChange(url)}
        />
      )
    default: {
      const optionLabels = masterOpts.map((option) => ({ value: option, label: t(option) }))
      const labelCounts = new Map<string, number>()
      for (const { label } of optionLabels) {
        labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1)
      }
      // Keep distinct original values selectable when their translations coincide.
      const suggestions = optionLabels.map((option) => ({
        ...option,
        label: (labelCounts.get(option.label) ?? 0) > 1 && option.label !== option.value
          ? `${option.label} (${option.value})`
          : option.label,
      }))
      const rawValue = String(value ?? "")
      const selected = suggestions.find((option) => option.value === rawValue)
      return (
        <>
          <input
            className={inputCls}
            list={spec.optionsFrom ? `masters-${spec.field}` : undefined}
            value={selected?.label ?? rawValue}
            disabled={disabled}
            onChange={(e) => {
              const entered = e.target.value
              const suggestion = suggestions.find((option) => option.label === entered)
              // Store the master value; free text remains available for new categories.
              onChange(suggestion?.value ?? entered)
            }}
          />
          {spec.optionsFrom && suggestions.length > 0 && (
            <datalist id={`masters-${spec.field}`}>
              {suggestions.map((option) => (
                <option key={option.value} value={option.label} />
              ))}
            </datalist>
          )}
        </>
      )
    }
  }
}

const BADGE_TONES: Record<string, "green" | "sky" | "amber" | "rose" | "zinc"> = {
  Confirmed: "green", "Checked In": "sky", "Checked Out": "zinc",
  Cancelled: "rose", "No Show": "rose", Waitlist: "amber",
  Requested: "amber", Held: "amber", "Pending Payment": "amber",
  Open: "amber", Closed: "zinc", Enquiry: "amber", Completed: "zinc",
  Clean: "green", Dirty: "amber", Inspected: "sky", "Out of Order": "rose",
  "In Progress": "sky", Done: "green",
}

const cellValue = (v: unknown, t: (s: string) => string = translate) =>
  typeof v === "number"
    ? fmtAmount(v)
    : v == null || v === ""
      ? "-"
      : typeof v === "string"
        ? t(v)
        : String(v)

export function ResourceScreen({
  config,
  headerAction,
  onSaved,
}: {
  config: ScreenConfig
  /** Extra control rendered in the header next to New (e.g. a bulk import). */
  headerAction?: ReactNode
  /** Called after a record is created, updated, or deleted. */
  onSaved?: () => void
}) {
  const { t, lang } = useT()
  const { roles } = useAuth()
  const canBackdate = roles.some((r) => BACKDATE_ROLES.includes(r))
  const [rows, setRows] = useState<Row[]>([])
  const [editing, setEditing] = useState<Row | "new" | null>(null)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [linkOptions, setLinkOptions] = useState<Record<string, string[]>>({})
  const [linkCapped, setLinkCapped] = useState<Record<string, boolean>>({})
  const [linkReloadTick, setLinkReloadTick] = useState(0)
  const [masterOptions, setMasterOptions] = useState<
    Partial<Record<MenuMasterKey, string[]>>
  >({})
  const [search, setSearch] = useState("")
  // Frappe-style list settings: choose which columns this table shows,
  // remembered per user per doctype.
  const colsKey = `kamra:cols:${config.doctype}`
  const [hiddenCols, setHiddenCols] = useState<Set<string>>(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(colsKey) || "[]"))
    } catch {
      return new Set()
    }
  })
  const [colsOpen, setColsOpen] = useState(false)
  const toggleCol = (field: string) => {
    setHiddenCols((prev) => {
      const next = new Set(prev)
      if (next.has(field)) next.delete(field)
      else if (next.size < config.columns.length - 1) next.add(field)
      localStorage.setItem(colsKey, JSON.stringify([...next]))
      return next
    })
  }
  const visibleCols = config.columns.filter((c) => !hiddenCols.has(c.field))
  const [debounced, setDebounced] = useState("")
  const [filterVals, setFilterVals] = useState<Record<string, string>>({})
  const [filterOpts, setFilterOpts] = useState<Record<string, Opt[]>>({})
  useEffect(() => {
    for (const f of config.filters ?? []) {
      const src = sourceOf(f)
      if (src)
        loadOptions(src)
          .then((o) => setFilterOpts((p) => ({ ...p, [f.field]: o })))
          .catch(() => {})
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.doctype])
  const [dateFrom, setDateFrom] = useState("")
  const [dateTo, setDateTo] = useState("")
  const [page, setPage] = useState(0)

  const pageSize = config.pageSize ?? 0

  const fields = Array.from(
    new Set(["name", ...config.columns.map((c) => c.field)]),
  )

  // debounce the search box
  useEffect(() => {
    const t = setTimeout(() => setDebounced(search.trim()), 300)
    return () => clearTimeout(t)
  }, [search])

  // any search/filter change resets to the first page
  useEffect(() => setPage(0), [debounced, filterVals, dateFrom, dateTo])

  const matchLocalSearch = useCallback(
    (items: Row[]) => {
      if (!config.propertyOrShared || !debounced || !config.searchFields?.length) return items
      const needle = debounced.toLowerCase()
      return items.filter((row) =>
        config.searchFields!.some((f) => String(row[f] ?? "").toLowerCase().includes(needle)),
      )
    },
    [config.propertyOrShared, config.searchFields, debounced],
  )

  const load = useCallback(() => {
    const filters: (string | number)[][] = []
    if (config.propertyScoped)
      filters.push(["property", "=", getCurrentProperty()])
    for (const [field, val] of Object.entries(filterVals))
      if (val) filters.push([field, "=", val])
    if (config.dateFilter?.field) {
      if (dateFrom) filters.push([config.dateFilter.field, ">=", dateFrom])
      if (dateTo) filters.push([config.dateFilter.field, "<=", dateTo])
    }
    const orFilters = config.propertyOrShared
      ? [
          ["property", "=", getCurrentProperty()],
          ["property", "is", "not set"],
        ]
      : debounced && config.searchFields?.length
        ? config.searchFields.map((f) => [f, "like", `%${debounced}%`])
        : undefined
    listResource(config.doctype, {
      fields,
      filters: filters.length ? filters : undefined,
      orFilters,
      orderBy: config.orderBy,
      limit: pageSize || 100,
      start: pageSize ? page * pageSize : 0,
    })
      .then((r) => {
        setRows(matchLocalSearch(r))
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.doctype, config.propertyOrShared, debounced, filterVals, page, dateFrom, dateTo, matchLocalSearch])

  useEffect(load, [load])

  // ?open=<name> - a link from another record opens that row's panel
  const [searchParams, setSearchParams] = useSearchParams()
  const openParam = searchParams.get("open")
  useEffect(() => {
    if (!openParam) return
    openEdit({ name: openParam })
    setSearchParams(
      (p) => {
        p.delete("open")
        return p
      },
      { replace: true },
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openParam])

  async function exportCsv() {
    const filters: (string | number)[][] = []
    if (config.propertyScoped)
      filters.push(["property", "=", getCurrentProperty()])
    for (const [field, val] of Object.entries(filterVals))
      if (val) filters.push([field, "=", val])
    if (config.dateFilter?.field) {
      if (dateFrom) filters.push([config.dateFilter.field, ">=", dateFrom])
      if (dateTo) filters.push([config.dateFilter.field, "<=", dateTo])
    }
    const orFilters = config.propertyOrShared
      ? [
          ["property", "=", getCurrentProperty()],
          ["property", "is", "not set"],
        ]
      : debounced && config.searchFields?.length
        ? config.searchFields.map((f) => [f, "like", `%${debounced}%`])
        : undefined
    const all = matchLocalSearch(await listResource(config.doctype, {
      fields,
      filters: filters.length ? filters : undefined,
      orFilters,
      orderBy: config.orderBy,
      limit: 2000,
    }))
    const cols = visibleCols
    downloadCsv(
      `${config.doctype.toLowerCase().replace(/ /g, "-")}-${new Date().toISOString().slice(0, 10)}`,
      cols.map((c) => c.label),
      all.map((r) => cols.map((c) => r[c.field] as string | number | null)),
    )
  }

  // load link options and menu master lists once per screen
  useEffect(() => {
    const links = config.form.filter((f) => f.type === "link" && f.linkDoctype)
    links.forEach((f) => {
      const filters: (string | number)[][] = []
      if (f.linkPropertyScoped) filters.push(["property", "=", getCurrentProperty()])
      if (f.linkFilters?.length) filters.push(...f.linkFilters)
      const limit = f.linkLimit ?? 100
      listResource(f.linkDoctype!, {
        fields: ["name"],
        filters: filters.length ? filters : undefined,
        limit,
        orderBy: "name asc",
      }).then((r) => {
        setLinkOptions((prev) => ({
          ...prev,
          [f.field]: r.map((x) => x.name),
        }))
        setLinkCapped((prev) => ({
          ...prev,
          [f.field]: r.length >= limit,
        }))
      })
    })
    if (config.form.some((f) => f.optionsFrom)) {
      call<Record<MenuMasterKey, string[]>>("kamra.settings.menu_masters", {
        property: getCurrentProperty(),
      })
        .then(setMasterOptions)
        .catch(() => setMasterOptions({}))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.doctype, linkReloadTick])

  function openEdit(row: Row | "new") {
    setEditing(row)
    setError(null)
    if (row === "new") {
      // ponytail: transaction dates default to today when notPast is set
      const defaults: Record<string, unknown> = {}
      for (const spec of config.form) {
        if (spec.default !== undefined) defaults[spec.field] = spec.default
        if (spec.type === "date" && spec.notPast) defaults[spec.field] = isoToday()
      }
      setDraft(defaults)
      return
    }
    // List rows only carry column fields - fetch the full document so
    // form extras (location, slugs, etc.) aren't blank and can't wipe DB values.
    const secrets = config.form.filter((f) => f.type === "password").map((f) => f.field)
    const withoutSecrets = (doc: Row) => {
      const copy: Record<string, unknown> = { ...doc }
      for (const field of secrets) delete copy[field]
      return copy
    }
    setDraft(withoutSecrets(row))
    frappeFetch<{ data: Row }>(
      `/api/resource/${encodeURIComponent(config.doctype)}/${encodeURIComponent(row.name)}`,
    )
      .then((r) => setDraft(withoutSecrets(r.data)))
      .catch((e) => setError(serverError(e)))
  }

  async function save() {
    setBusy(true)
    try {
      // Only persist form fields - never the raw list-row or full-doc dump.
      const payload: Record<string, unknown> = {}
      for (const f of config.form) {
        if (f.type === "readonly") continue
        if (f.readOnlyOnEdit && editing !== "new") continue
        if (f.disabledWhen?.(draft)) continue
        // an empty secret keeps the stored one
        if (f.type === "password" && !draft[f.field]) continue
        if (f.type === "table") {
          if (f.field in draft) payload[f.field] = tableRows(f, draft[f.field])
          continue
        }
        if (f.field in draft) payload[f.field] = draft[f.field]
      }
      if (config.propertyScoped || (config.propertyOrShared && editing === "new"))
        payload.property = getCurrentProperty()
      if (editing === "new") {
        await createResource(config.doctype, payload)
      } else if (editing) {
        await updateResource(config.doctype, editing.name, payload)
      }
      setEditing(null)
      load()
      if (config.form.some((f) => f.type === "link" && f.linkDoctype === config.doctype)) {
        setLinkReloadTick((n) => n + 1)
      }
      onSaved?.()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function remove() {
    if (editing === "new" || !editing) return
    setBusy(true)
    try {
      await deleteResource(config.doctype, editing.name)
      setEditing(null)
      load()
      if (config.form.some((f) => f.type === "link" && f.linkDoctype === config.doctype)) {
        setLinkReloadTick((n) => n + 1)
      }
      onSaved?.()
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
          <CardTitle>{t(config.title)}</CardTitle>
          {config.description && (
            <p className="mt-0.5 text-xs text-zinc-400">
              {t(config.description)}
            </p>
          )}
        </div>
        <div className="flex items-center gap-2">
          {headerAction}
          {config.allowCreate !== false && (
            <Button onClick={() => openEdit("new")}>
              <Plus className="size-4" aria-hidden />
              {t("New")}
            </Button>
          )}
        </div>
      </CardHeader>
      <CardContent>
        {error && !editing && (
          <div className="mb-3 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
            {translate(error)}
          </div>
        )}
        <div className="mb-4 flex flex-wrap items-center gap-2">
            {config.searchFields?.length ? (
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 size-4 text-zinc-400" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t("Search…")}
                  className="w-56 rounded-lg border border-zinc-300 bg-white py-1.5 pl-8 pr-3 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"
                />
              </div>
            ) : null}
            {config.filters?.map((f) => (
              <select
                key={f.field}
                value={filterVals[f.field] ?? ""}
                onChange={(e) =>
                  setFilterVals((v) => ({ ...v, [f.field]: e.target.value }))
                }
                className="rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"
              >
                <option value="">{t("{label}: all", { label: t(f.label) })}</option>
                {f.options
                  ? f.options.map((o) => (
                      <option key={o} value={o}>
                        {t(o)}
                      </option>
                    ))
                  : (filterOpts[f.field] ?? []).map((o) => (
                      <option key={o.value} value={o.value}>
                        {o.label}
                      </option>
                    ))}
              </select>
            ))}
            {config.dateFilter && (
              <div className="flex items-center gap-1.5 text-sm text-zinc-500">
                <span className="text-xs">{t(config.dateFilter.label)}</span>
                <input
                  type="date"
                  aria-label={(`${t(config.dateFilter.label)} ${t("from")}`)}
                  className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm"
                  value={dateFrom}
                  onChange={(e) => setDateFrom(e.target.value)}
                />
                <span className="text-xs">{t("to")}</span>
                <input
                  type="date"
                  aria-label={(`${t(config.dateFilter.label)} ${t("to")}`)}
                  className="rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm"
                  value={dateTo}
                  onChange={(e) => setDateTo(e.target.value)}
                />
                {(dateFrom || dateTo) && (
                  <button
                    className="text-xs text-zinc-400 hover:text-zinc-600"
                    onClick={() => {
                      setDateFrom("")
                      setDateTo("")
                    }}
                  >
                    {t("Clear")}
                  </button>
                )}
              </div>
            )}
            <div className="relative ml-auto flex items-center gap-2">
              <button
                onClick={exportCsv}
                title={t("Download the current view as CSV (Excel-ready)")}
                aria-label={t("Export as CSV")}
                className="flex items-center gap-1.5 rounded-lg border border-zinc-200 px-2.5 py-1.5 text-sm text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700"
              >
                <Download className="size-4" aria-hidden />
                {t("Export")}
              </button>
              <button
                onClick={() => setColsOpen((o) => !o)}
                title={t("Choose which columns this table shows")}
                aria-label={t("Configure table columns")}
                className="flex items-center gap-1.5 rounded-lg border border-zinc-200 px-2.5 py-1.5 text-sm text-zinc-500 hover:bg-zinc-50 hover:text-zinc-700"
              >
                <Columns3 className="size-4" aria-hidden />
                {t("Columns")}
              </button>
              {colsOpen && (
                <div className="absolute right-0 z-30 mt-1 w-56 rounded-xl border border-zinc-200 bg-white p-2 shadow-xl">
                  {config.columns.map((c) => (
                    <label
                      key={c.field}
                      className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-zinc-50"
                    >
                      <input
                        type="checkbox"
                        className="size-4 accent-brand-600"
                        checked={!hiddenCols.has(c.field)}
                        onChange={() => toggleCol(c.field)}
                      />
                      {t(c.label)}
                    </label>
                  ))}
                  <button
                    className="mt-1 w-full rounded-lg px-2 py-1 text-left text-xs text-zinc-400 hover:text-zinc-600"
                    onClick={() => setColsOpen(false)}
                  >
                    {t("Done")}
                  </button>
                </div>
              )}
            </div>
          </div>
        <div className="overflow-x-auto">
          <table className={"w-full text-sm " + (visibleCols.length > 7 ? "table-auto" : "table-fixed")}>
            <thead>
              <tr className="border-b border-zinc-200 text-left text-xs font-medium uppercase tracking-wider text-zinc-500">
                {visibleCols.map((c, i) => (
                  <th
                    key={c.field}
                    className={
                      "py-2 pr-4" +
                      (visibleCols.length > 7 ? " whitespace-nowrap" : i === 0 ? " w-[22%]" : "")
                    }
                  >
                    {t(c.label)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-zinc-100">
              {rows.map((row) => (
                <tr
                  key={row.name}
                  className="cursor-pointer hover:bg-zinc-50"
                  onClick={() => openEdit(row)}
                >
                  {visibleCols.map((c) => (
                    <td
                      key={c.field}
                      className={
                        (visibleCols.length > 7
                          ? "whitespace-nowrap py-2.5 pr-4" + (c.badge || c.statusCheck || c.booleanBadge ? "" : " max-w-[240px] truncate")
                          : "truncate py-2.5 pr-4") +
                        (typeof row[c.field] === "number"
                          ? " text-right tabular-nums"
                          : "")
                      }
                    >
                      {c.statusCheck ? (
                        <Badge tone={row[c.field] ? "zinc" : "green"}>
                          {t(row[c.field] ? "Disabled" : "Active")}
                        </Badge>
                      ) : c.booleanBadge ? (
                        Number(row[c.field]) ? (
                          <Badge tone="sky">{t(c.booleanBadge.truthy)}</Badge>
                        ) : c.booleanBadge.falsy ? (
                          <Badge tone="zinc">{t(c.booleanBadge.falsy)}</Badge>
                        ) : (
                          "-"
                        )
                      ) : c.badge && row[c.field] ? (
                        <Badge
                          tone={BADGE_TONES[String(row[c.field])] ?? "zinc"}
                        >
                          {t(String(row[c.field]))}
                        </Badge>
                      ) : (
                        cellValue(c.field === "provider" && typeof row[c.field] === "string"
                          ? providerLabel(String(row[c.field])) : row[c.field], t)
                      )}
                    </td>
                  ))}
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td
                    colSpan={config.columns.length}
                    className="py-6 text-center text-sm text-zinc-400"
                  >
                    {t("Nothing here yet.")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        {pageSize > 0 && (page > 0 || rows.length >= pageSize) && (
          <div className="mt-3 flex items-center justify-between text-sm text-zinc-500">
            <span>{t("Page {n}", { n: page + 1 })}</span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                disabled={page === 0}
                onClick={() => setPage((p) => Math.max(0, p - 1))}
              >
                {t("Prev")}
              </Button>
              <Button
                variant="outline"
                disabled={rows.length < pageSize}
                onClick={() => setPage((p) => p + 1)}
              >
                {t("Next")}
              </Button>
            </div>
          </div>
        )}
      </CardContent>

      {editing && (() => {
        const useDetail = editing !== "new" && !!config.detailPanel
        return (
        <Sheet
          wide
          title={
            editing === "new"
              ? (lang !== "en") ? t("New record in {section}", { section: t(config.title) }) : t("New {thing}", {
                  thing: t(config.title.replace(/s$/, "")),
                })
              : String(editing.name)
          }
          description={(useDetail ? undefined : config.description ? t(config.description) : undefined)}
          onClose={() => setEditing(null)}
          footer={
            useDetail ? undefined : (
            <div className="flex items-center justify-between">
              {editing !== "new" &&
              config.allowDelete !== false &&
              (!config.canDelete || config.canDelete(draft)) ? (
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={remove}
                  aria-label={t("Delete")}
                >
                  <Trash2 className="size-4 text-rose-500" aria-hidden />
                  {t("Delete")}
                </Button>
              ) : (
                <span />
              )}
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditing(null)}>
                  {t("Cancel")}
                </Button>
                <Button disabled={busy} onClick={save}>
                  {busy ? t("Saving…") : t("Save")}
                </Button>
              </div>
            </div>
            )
          }
        >
          {useDetail && config.detailPanel ? (
            <Suspense fallback={<p className="text-sm text-zinc-400">{t("Loading…")}</p>}>
              <config.detailPanel
                row={editing}
                reload={load}
                onClose={() => setEditing(null)}
              />
            </Suspense>
          ) : (
            <div className="space-y-4">
              {config.formNote && <p className="rounded-lg bg-zinc-50 p-3 text-sm text-zinc-700">{config.formNote}</p>}
              <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
                {config.form.filter(spec => spec.dependsOn ? spec.dependsOn(draft) : true).map((spec) => spec.type === "table" ? (
                  <TableField
                    key={spec.field}
                    spec={spec}
                    value={draft[spec.field]}
                    onChange={(v) => setDraft((d) => ({ ...d, [spec.field]: v }))}
                    t={t}
                  />
                ) : (
                  <label
                    key={spec.field}
                    className={
                      "block" +
                      (spec.type === "check" || spec.type === "tags"
                        ? " sm:col-span-2"
                        : "")
                    }
                  >
                    <span className="mb-1.5 block text-sm font-medium text-zinc-600">
                      {t(spec.label)}
                      {spec.required && <span className="text-rose-500"> *</span>}
                    </span>
                    <FieldInput
                      spec={spec}
                      value={spec.displayValue ? spec.displayValue(draft) : draft[spec.field]}
                      onChange={(v) =>
                        setDraft((d) => ({ ...d, [spec.field]: v }))
                      }
                      linkOptions={linkOptions}
                      linkCapped={linkCapped}
                      extraLinkOptions={spec.ensureOptions?.(draft)}
                      masterOptions={masterOptions}
                      t={t}
                      canBackdate={canBackdate}
                      disabled={Boolean(
                        (spec.readOnlyOnEdit && editing !== "new") ||
                        spec.disabledWhen?.(draft),
                      )}
                    />
                    {(() => {
                      const shownHint = spec.hintWhen?.(draft) ?? spec.hint
                      return shownHint && spec.type !== "image" ? (
                        <span className="mt-1 block text-xs leading-relaxed text-zinc-400">
                          {t(shownHint)}
                        </span>
                      ) : null
                    })()}
                  </label>
                ))}
              </div>
              {editing !== "new" && config.extra && (
                <Suspense fallback={null}>
                  <config.extra row={editing} reload={load} />
                </Suspense>
              )}
              {error && (
                <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
                  {translate(error)}
                </div>
              )}
            </div>
          )}
        </Sheet>
        )
      })()}
    </Card>
  )
}
