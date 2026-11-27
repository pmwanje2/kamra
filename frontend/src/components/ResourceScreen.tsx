import {
  useCallback,
  useEffect,
  useId,
  useRef,
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
  /** the property's rate categories (Settings → Revenue) for Rate Plans */
  | "rate_categories"

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
  linkLabelField?: string
  linkFallbackLabel?: (value: string) => string
  /** link options only from the current property (the target has a `property` field) */
  linkPropertyScoped?: boolean
  /** extra server filters for a link field's option query (e.g. enabled currencies only) */
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
  /** Block dates before today unless the user may backdate (Finance / Hotel Admin). */
  notPast?: boolean
  /** Initial value when creating a new record (e.g. the DocType's own default). */
  default?: unknown
  /** Key of a `ScreenConfig.sections` entry: the field renders in that collapsible section. */
  section?: string
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
    /** Text shown when the cell value is blank (e.g. "Shared" for unscoped rows). */
    emptyLabel?: string
    /** Computed client-side column (omitted from listResource fields). */
    virtual?: boolean
    /** Custom cell renderer for display. */
    render?: (row: Row) => ReactNode
    /** Explicit CSV export value for computed columns. */
    csvValue?: (row: Row) => string | number | null
    /** Optional table width class (e.g. "w-[24%]"). */
    width?: string
    /** Allow wrapping instead of truncating in table cells. */
    wrap?: boolean
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
  /** Optional minimum table width class (e.g. "min-w-[820px]") so wide fixed-layout tables scroll cleanly on mobile. */
  tableMinWidth?: string
  /** Fields searched (LIKE) by the search box. Adds a search input when set. */
  searchFields?: string[]
  /** Dropdown filters shown in the toolbar (e.g. status). */
  filters?: FilterSpec[]
  /** Rows per page (adds pagination when set). */
  pageSize?: number
  enrichRows?: (rows: Row[]) => Promise<Row[]>
  /** Date-range filter on this date field (adds From/To pickers). */
  dateFilter?: { field: string; label: string }
  /** Custom section rendered in the drawer below the form (existing rows only). */
  extra?: ComponentType<{ row: Row; reload: () => void }>
  /** Custom section rendered in the drawer above the form (existing rows only). */
  lead?: ComponentType<{ row: Row; reload: () => void }>
  /** Collapsible groups of form fields (see FieldSpec.section), after the other
   *  fields: closed for an existing row, open for a new one. */
  sections?: { key: string; label: string; hint?: string }[]
  /** Replace the generic edit form with a bespoke detail panel (existing rows).
   *  When set, the drawer opens wide and the panel owns its own actions. */
  detailPanel?: ComponentType<{
    row: Row
    reload: () => void
    onClose: () => void
  }>
}

const inputCls =
  "w-full rounded-lg border border-control bg-surface px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus " +
  "disabled:bg-canvas disabled:text-muted-ink"

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
          <span className="text-xs text-muted-ink">{props.t("None selected")}</span>
        )}
        {selected.map((tag) => (
          <button
            key={tag}
            type="button"
            onClick={() => toggle(tag)}
            className="inline-flex items-center gap-1 rounded-full border border-accent-line bg-accent-subtle px-2.5 py-0.5 text-xs font-medium text-accent-ink hover:bg-accent-muted"
          >
            {props.t(tag)}
            <span aria-hidden className="text-accent-text">×</span>
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
              className="rounded-full border border-line bg-surface px-2.5 py-0.5 text-xs text-secondary-ink hover:border-control hover:bg-canvas"
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
          className="text-xs text-muted-ink hover:text-secondary-ink"
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
  const cell = "w-full rounded-md border border-control bg-surface px-2 py-1 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-focus"
  const icon = "rounded-md p-1 text-muted-ink hover:bg-subtle hover:text-secondary-ink disabled:opacity-30"
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
      <legend className="mb-1.5 text-sm font-medium text-secondary-ink">{t(spec.label)}</legend>
      {spec.hint && <p className="text-xs leading-relaxed text-muted-ink">{t(spec.hint)}</p>}
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
              {columns.map((c) => <th key={c.field} className="py-1 pr-2">{t(c.label)}</th>)}
              <th className="py-1" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line-subtle">
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
      {rows.length === 0 && <p className="text-xs text-muted-ink">{t("Nothing here yet.")}</p>}
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

function linkOptionsFor(rows: Row[], spec: FieldSpec): Opt[] {
  return rows.map((row) => ({
    value: row.name,
    label: String((spec.linkLabelField && row[spec.linkLabelField]) || spec.linkFallbackLabel?.(row.name) || row.name),
  }))
}

function LinkSelect(props: {
  spec: FieldSpec
  value: unknown
  onChange: (v: unknown) => void
  baseOpts: Opt[]
  extraOpts: string[]
  capped?: boolean
  disabled?: boolean
  selectId?: string
  fieldLabel?: string
  t: (s: string) => string
}) {
  const { spec, value, onChange, baseOpts, extraOpts, capped, disabled, selectId, fieldLabel, t } = props
  const [query, setQuery] = useState("")
  const [searched, setSearched] = useState<Opt[]>([])

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
      const fields = spec.linkLabelField ? ["name", spec.linkLabelField] : ["name"]
      const searchFilters = fields.map((field) => [field, "like", `%${trimmed}%`])
      listResource(spec.linkDoctype!, {
        fields,
        filters: spec.linkLabelField ? filters : [...filters, ...searchFilters],
        orFilters: spec.linkLabelField ? searchFilters : undefined,
        limit: 50,
        orderBy: "name asc",
      })
        .then((r) => {
          if (active) setSearched(linkOptionsFor(r, spec))
        })
        .catch(() => {})
    }, 120)
    return () => {
      active = false
      clearTimeout(timer)
    }
  }, [capped, query, spec.linkDoctype, spec.linkPropertyScoped, spec.linkFilters, spec.linkLabelField, spec.linkFallbackLabel])

  const current = String(value ?? "")
  const labels = new Map([...baseOpts, ...searched].map(({ value, label }) => [value, label]))
  const opts = [...new Set([...labels.keys(), ...extraOpts, current].filter(Boolean))].sort()

  return (
    <div className={capped && !disabled ? "space-y-1.5" : undefined}>
      {capped && !disabled && (
        <input
          type="search"
          className={inputCls}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("Search…")}
          aria-label={fieldLabel ? `${t("Search…")} ${fieldLabel}` : t("Search…")}
        />
      )}
      <select
        id={selectId}
        className={inputCls}
        value={current}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">-</option>
        {opts.map((o) => (
          <option key={o} value={o}>
            {labels.get(o) || spec.linkFallbackLabel?.(o) || o}
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
  linkOptions: Record<string, Opt[]>
  linkCapped?: Record<string, boolean>
  extraLinkOptions?: string[]
  masterOptions: Partial<Record<MenuMasterKey, string[]>>
  /** Translate display labels; API values stay English. */
  t: (s: string) => string
  canBackdate?: boolean
  disabled?: boolean
  selectId?: string
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
          selectId={props.selectId}
          fieldLabel={t(spec.label)}
          t={t}
        />
      )
    case "check":
      return (
        <input
          type="checkbox"
          className="size-4 accent-focus disabled:cursor-not-allowed disabled:opacity-50"
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

const BADGE_TONES: Record<string, "green" | "sky" | "amber" | "orange" | "indigo" | "rose" | "zinc"> = {
  Confirmed: "green", "Checked In": "sky", "Checked Out": "zinc",
  Cancelled: "rose", "No Show": "rose", Waitlist: "amber",
  Requested: "amber", Held: "amber", "Pending Payment": "amber",
  Open: "amber", Closed: "zinc", Enquiry: "amber", Completed: "zinc",
  Clean: "green", Dirty: "amber", Pickup: "indigo", Inspected: "sky", Ready: "green",
  "Out of Service": "orange", "Out of Order": "rose",
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
  onReload,
  onSaved,
}: {
  config: ScreenConfig
  /** Extra control rendered in the header next to New (e.g. a bulk import). */
  headerAction?: ReactNode
  /** Optional hook fired whenever rows reload after create/update/delete. */
  onReload?: () => void
  /** Called after a record is created, updated, or deleted. */
  onSaved?: () => void
}) {
  const { t, lang } = useT()
  const formId = useId()
  const { roles } = useAuth()
  const canBackdate = roles.some((r) => BACKDATE_ROLES.includes(r))
  const [allRows, setAllRows] = useState<Row[]>([])
  const [editing, setEditing] = useState<Row | "new" | null>(null)
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [canDeleteDoc, setCanDeleteDoc] = useState(false)
  const deletePermReq = useRef(0)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [linkOptions, setLinkOptions] = useState<Record<string, Opt[]>>({})
  const [linkCapped, setLinkCapped] = useState<Record<string, boolean>>({})
  const [linkReloadTick, setLinkReloadTick] = useState(0)
  const [masterOptions, setMasterOptions] = useState<
    Partial<Record<MenuMasterKey, string[]>>
  >({})
  const [searchParams, setSearchParams] = useSearchParams()
  const query = searchParams.get("q") ?? ""
  const [search, setSearch] = useState(query)
  useEffect(() => setSearch(query), [query])
  // Column chooser: choose which columns this table shows,
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
    new Set(["name", ...config.columns.filter((c) => !c.virtual).map((c) => c.field)]),
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
      limit: config.propertyOrShared ? 2000 : (pageSize || 100),
      start: config.propertyOrShared ? 0 : (pageSize ? page * pageSize : 0),
    })
      .then((r) => config.enrichRows?.(r) ?? r)
      .then((r) => {
        setAllRows(r)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.doctype, config.propertyOrShared, config.enrichRows, debounced, filterVals, page, dateFrom, dateTo])

  const filteredRows = matchLocalSearch(allRows)
  const rows =
    config.propertyOrShared && pageSize > 0
      ? filteredRows.slice(page * pageSize, (page + 1) * pageSize)
      : filteredRows
  const hasMorePages =
    config.propertyOrShared && pageSize > 0
      ? (page + 1) * pageSize < filteredRows.length
      : rows.length >= pageSize

  const currentProperty = getCurrentProperty()
  const isSharedEdit = Boolean(
    config.propertyOrShared &&
      editing &&
      editing !== "new" &&
      !editing.property &&
      !draft.property,
  )
  const editingCode =
    editing && editing !== "new"
      ? String(draft.code ?? editing.code ?? editing.name ?? "")
      : ""
  const existingOverride = isSharedEdit
    ? allRows.find(
        (r) =>
          String(r.property || "") === currentProperty &&
          String(r.code ?? r.name ?? "") === editingCode,
      )
    : undefined

  useEffect(load, [load])

  // ?open=<name> - a link from another record opens that row's panel
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
    const exported = await listResource(config.doctype, {
      fields,
      filters: filters.length ? filters : undefined,
      orFilters,
      orderBy: config.orderBy,
      limit: 2000,
    })
    const all = matchLocalSearch(await (config.enrichRows?.(exported) ?? exported))
    const cols = visibleCols
    downloadCsv(
      `${config.doctype.toLowerCase().replace(/ /g, "-")}-${new Date().toISOString().slice(0, 10)}`,
      cols.map((c) => c.label),
      all.map((r) => cols.map((c) => (c.csvValue ? c.csvValue(r) : r[c.field] as string | number | null))),
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
        fields: f.linkLabelField ? ["name", f.linkLabelField] : ["name"],
        filters: filters.length ? filters : undefined,
        limit,
        orderBy: "name asc",
      }).then((r) => {
        setLinkOptions((prev) => ({
          ...prev,
          [f.field]: linkOptionsFor(r, f),
        }))
        setLinkCapped((prev) => ({
          ...prev,
          [f.field]: r.length >= limit,
        }))
      })
    })
    if (config.form.some((f) => f.optionsFrom)) {
      // a failed lookup leaves the static options; it never breaks the screen
      Promise.resolve()
        .then(() => call<Record<MenuMasterKey, string[]>>("kamra.settings.menu_masters", {
          property: getCurrentProperty(),
        }))
        .then((masters) => setMasterOptions(masters ?? {}))
        .catch(() => setMasterOptions({}))
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [config.doctype, linkReloadTick])

  function openEdit(row: Row | "new") {
    setEditing(row)
    setError(null)
    setCanDeleteDoc(false)
    const permReq = ++deletePermReq.current
    if (row === "new") {
      // ponytail: transaction dates default to today when notPast is set
      const defaults: Record<string, unknown> = {}
      for (const spec of config.form) {
        if (spec.type === "date" && spec.notPast) defaults[spec.field] = isoToday()
        else if (spec.default !== undefined) defaults[spec.field] = spec.default
      }
      setDraft(defaults)
      return
    }
    if (config.allowDelete !== false) {
      call<{ has_permission?: boolean }>("frappe.client.has_permission", {
        doctype: config.doctype,
        docname: row.name,
        perm_type: "delete",
      })
        .then((res) => {
          if (deletePermReq.current === permReq)
            setCanDeleteDoc(Boolean(res?.has_permission))
        })
        .catch(() => {
          if (deletePermReq.current === permReq) setCanDeleteDoc(false)
        })
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
      if (isSharedEdit && editing && editing !== "new") {
        if (existingOverride) {
          setError(
            t("This property already has its own override for {code}. Edit that property row instead.", {
              code: editingCode,
            }),
          )
          return
        }
        const overridePayload: Record<string, unknown> = {
          ...payload,
          code: editingCode,
          property: currentProperty,
          system: 0,
        }
        if (!("tender_type" in overridePayload) && (draft.tender_type ?? editing.tender_type)) {
          overridePayload.tender_type = draft.tender_type ?? editing.tender_type
        }
        if (!("disabled" in overridePayload) && "disabled" in draft) {
          overridePayload.disabled = draft.disabled
        }
        await createResource(config.doctype, overridePayload)
      } else {
        if (config.propertyScoped || (config.propertyOrShared && editing === "new"))
          payload.property = currentProperty
        if (editing === "new") {
          await createResource(config.doctype, payload)
        } else if (editing) {
          await updateResource(config.doctype, editing.name, payload)
        }
      }
      setEditing(null)
      load()
      if (config.form.some((f) => f.type === "link" && f.linkDoctype === config.doctype)) {
        setLinkReloadTick((n) => n + 1)
      }
      onReload?.()
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
      onReload?.()
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
            <p className="mt-0.5 text-xs text-muted-ink">
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
          <div className="mb-3 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
            {translate(error)}
          </div>
        )}
        <div className="mb-4 flex flex-wrap items-center gap-2">
            {config.searchFields?.length ? (
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-2 size-4 text-muted-ink" />
                <input
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={t("Search…")}
                  className="w-56 rounded-lg border border-control bg-surface py-1.5 pl-8 pr-3 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-focus"
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
                className="rounded-lg border border-control bg-surface px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-focus"
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
              <div className="flex max-w-full min-w-0 flex-wrap items-center gap-1.5 text-sm text-muted-ink">
                <div className="flex min-w-0 max-w-full items-center gap-1.5">
                  <span className="text-xs">{t(config.dateFilter.label)}</span>
                  <input
                    type="date"
                    aria-label={(`${t(config.dateFilter.label)} ${t("from")}`)}
                    className="min-w-0 max-w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm"
                    value={dateFrom}
                    onChange={(e) => setDateFrom(e.target.value)}
                  />
                </div>
                <div className="flex min-w-0 max-w-full items-center gap-1.5">
                  <span className="text-xs">{t("to")}</span>
                  <input
                    type="date"
                    aria-label={(`${t(config.dateFilter.label)} ${t("to")}`)}
                    className="min-w-0 max-w-full rounded-lg border border-control bg-surface px-2 py-1.5 text-sm"
                    value={dateTo}
                    onChange={(e) => setDateTo(e.target.value)}
                  />
                </div>
                {(dateFrom || dateTo) && (
                  <button
                    className="text-xs text-muted-ink hover:text-secondary-ink"
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
                className="flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-sm text-muted-ink hover:bg-canvas hover:text-secondary-ink"
              >
                <Download className="size-4" aria-hidden />
                {t("Export")}
              </button>
              <button
                onClick={() => setColsOpen((o) => !o)}
                title={t("Choose which columns this table shows")}
                aria-label={t("Configure table columns")}
                className="flex items-center gap-1.5 rounded-lg border border-line px-2.5 py-1.5 text-sm text-muted-ink hover:bg-canvas hover:text-secondary-ink"
              >
                <Columns3 className="size-4" aria-hidden />
                {t("Columns")}
              </button>
              {colsOpen && (
                <div className="absolute right-0 z-30 mt-1 w-56 rounded-xl border border-line bg-surface p-2 shadow-xl">
                  {config.columns.map((c) => (
                    <label
                      key={c.field}
                      className="flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-canvas"
                    >
                      <input
                        type="checkbox"
                        className="size-4 accent-focus"
                        checked={!hiddenCols.has(c.field)}
                        onChange={() => toggleCol(c.field)}
                      />
                      {t(c.label)}
                    </label>
                  ))}
                  <button
                    className="mt-1 w-full rounded-lg px-2 py-1 text-left text-xs text-muted-ink hover:text-secondary-ink"
                    onClick={() => setColsOpen(false)}
                  >
                    {t("Done")}
                  </button>
                </div>
              )}
            </div>
          </div>
        <div className="overflow-x-auto">
          {(() => {
            const useFixedLayout = Boolean(config.tableMinWidth) && visibleCols.length <= 7
            return (
          <table className={`w-full text-sm ${useFixedLayout ? "table-fixed" : "table-auto"}${config.tableMinWidth ? ` ${config.tableMinWidth}` : ""}`}>
            <thead>
              <tr className="border-b border-line text-left text-xs font-medium uppercase tracking-wider text-muted-ink">
                {visibleCols.map((c, i) => (
                  <th
                    key={c.field}
                    className={
                      "py-2 pr-4" +
                      (c.width
                        ? ` ${c.width}`
                        : !useFixedLayout
                          ? " whitespace-nowrap"
                          : i === 0
                            ? " w-[22%]"
                            : "")
                    }
                  >
                    {t(c.label)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-line-subtle">
              {rows.map((row) => (
                <tr
                  key={row.name}
                  className="cursor-pointer hover:bg-canvas"
                  onClick={() => openEdit(row)}
                >
                  {visibleCols.map((c) => (
                    <td
                      key={c.field}
                      className={
                        (c.wrap
                          ? "py-2.5 pr-4 align-top"
                          : !useFixedLayout
                            ? "whitespace-nowrap py-2.5 pr-4" + (c.badge || c.statusCheck || c.booleanBadge ? "" : " max-w-[240px] truncate")
                            : "truncate py-2.5 pr-4") +
                        (!c.render && typeof row[c.field] === "number"
                          ? " text-right tabular-nums"
                          : "")
                      }
                    >
                      {c.render ? (
                        c.render(row)
                      ) : c.statusCheck ? (
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
                      ) : c.emptyLabel && (row[c.field] == null || row[c.field] === "") ? (
                        <span className="text-muted-ink">{t(c.emptyLabel)}</span>
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
                    className="py-6 text-center text-sm text-muted-ink"
                  >
                    {t("Nothing here yet.")}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
            )
          })()}
        </div>
        {pageSize > 0 && (page > 0 || hasMorePages) && (
          <div className="mt-3 flex items-center justify-between text-sm text-muted-ink">
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
                disabled={!hasMorePages}
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
              !isSharedEdit &&
              canDeleteDoc &&
              config.allowDelete !== false &&
              (!config.canDelete || config.canDelete(draft)) ? (
                <Button
                  variant="ghost"
                  disabled={busy}
                  onClick={remove}
                  aria-label={t("Delete")}
                >
                  <Trash2 className="size-4 text-danger-text" aria-hidden />
                  {t("Delete")}
                </Button>
              ) : (
                <span />
              )}
              <div className="flex gap-2">
                <Button variant="outline" onClick={() => setEditing(null)}>
                  {t("Cancel")}
                </Button>
                <Button disabled={busy || Boolean(existingOverride)} onClick={save}>
                  {busy ? t("Saving…") : t("Save")}
                </Button>
              </div>
            </div>
            )
          }
        >
          {useDetail && config.detailPanel ? (
            <Suspense fallback={<p className="text-sm text-muted-ink">{t("Loading…")}</p>}>
              <config.detailPanel
                row={editing}
                reload={load}
                onClose={() => setEditing(null)}
              />
            </Suspense>
          ) : (
            <div className="space-y-4">
              {editing !== "new" && config.lead && (
                <Suspense fallback={null}>
                  <config.lead row={editing} reload={load} />
                </Suspense>
              )}
              {config.formNote && <p className="rounded-lg bg-canvas p-3 text-sm text-secondary-ink">{config.formNote}</p>}
              {isSharedEdit && (
                existingOverride ? (
                  <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-warning-line bg-warning-subtle px-3 py-2.5 text-sm text-warning-ink">
                    <span>
                      {t("This property already has its own override for {code}. Edit that property row instead.", {
                        code: editingCode,
                      })}
                    </span>
                    <Button
                      variant="outline"
                      onClick={() => openEdit(existingOverride)}
                    >
                      {t("Edit property override")}
                    </Button>
                  </div>
                ) : (
                  <p className="rounded-lg border border-info-line bg-info-subtle px-3 py-2 text-sm text-info-ink">
                    {t("Saving creates a property override for {property}. The shared default stays unchanged for other properties.", {
                      property: currentProperty,
                    })}
                  </p>
                )
              )}
              {(() => {
                const shown = config.form.filter(spec => spec.dependsOn ? spec.dependsOn(draft) : true)
                const sectioned = new Set((config.sections ?? []).map((section) => section.key))
                const grid = (specs: FieldSpec[]) => (
                  <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2">
                    {specs.map((spec) => spec.type === "table" ? (
                      <TableField
                        key={spec.field}
                        spec={spec}
                        value={draft[spec.field]}
                        onChange={(v) => setDraft((d) => ({ ...d, [spec.field]: v }))}
                        t={t}
                      />
                    ) : spec.type === "link" ? (
                      <div key={spec.field} className="block">
                        <label
                          htmlFor={`${formId}-${spec.field}`}
                          className="mb-1.5 block text-sm font-medium text-secondary-ink"
                        >
                          {t(spec.label)}
                          {spec.required && <span className="text-danger-text"> *</span>}
                        </label>
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
                          selectId={`${formId}-${spec.field}`}
                          disabled={Boolean(
                            (spec.readOnlyOnEdit && editing !== "new") ||
                            spec.disabledWhen?.(draft),
                          )}
                        />
                        {(() => {
                          const shownHint = spec.hintWhen?.(draft) ?? spec.hint
                          return shownHint ? (
                            <span className="mt-1 block text-xs leading-relaxed text-muted-ink">
                              {t(shownHint)}
                            </span>
                          ) : null
                        })()}
                      </div>
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
                        <span className="mb-1.5 block text-sm font-medium text-secondary-ink">
                          {t(spec.label)}
                          {spec.required && <span className="text-danger-text"> *</span>}
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
                            <span className="mt-1 block text-xs leading-relaxed text-muted-ink">
                              {t(shownHint)}
                            </span>
                          ) : null
                        })()}
                      </label>
                    ))}
                  </div>
                )
                return <>
                  {grid(shown.filter((spec) => !spec.section || !sectioned.has(spec.section)))}
                  {(config.sections ?? []).map((section) => (
                    <details key={section.key} open={editing === "new"}
                      className="rounded-xl border border-line px-4 py-3 [&[open]>summary]:mb-3">
                      <summary className="cursor-pointer text-sm font-semibold text-secondary-ink">{t(section.label)}</summary>
                      {section.hint && <p className="mb-3 text-xs leading-relaxed text-muted-ink">{t(section.hint)}</p>}
                      {grid(shown.filter((spec) => spec.section === section.key))}
                    </details>
                  ))}
                </>
              })()}
              {editing !== "new" && config.extra && (
                <Suspense fallback={null}>
                  <config.extra row={editing} reload={load} />
                </Suspense>
              )}
              {error && (
                <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
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
