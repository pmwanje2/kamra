import { useEffect, useMemo, useState } from "react"
import { Link } from "react-router-dom"
import { ArrowUpRight, Lock, RotateCcw, TriangleAlert } from "lucide-react"

import ImageField from "../../components/ImageField"
import { Card } from "../../components/ui/card"
import { useT, t as translate, useUILanguage } from "../../lib/i18n"
import { cur, fmtMoney } from "../../lib/money"
import { SERVICE_ROLE } from "../../lib/brand"
import { serverError } from "../../lib/resource"
import { cn } from "../../lib/utils"
import { saveSettings, type SettingSpec, type SettingsModule } from "./api"


export const inputCls =
  "w-full rounded-lg border border-control bg-surface px-3 py-1.5 text-sm " +
  "focus:border-accent-control focus:outline-none focus:ring-2 focus:ring-brand-100 " +
  "disabled:cursor-not-allowed disabled:bg-canvas disabled:text-muted-ink"

/** Money amounts carry the property currency instead of a unit. */
const MONEY_KEYS = new Set([
  "security_deposit_amount", "gst_slab_threshold", "cleaning_fee",
  "registration_fee", "max_cash_refund", "pet_charge",
  "room_levy_per_night", "na_high_balance_threshold", "na_rate_variance_tolerance",
  "till_variance_tolerance", "allowance_approval_above", "pos_room_charge_limit",
])

export interface OptionSources {
  countries: string[]
  timezones: string[]
  locales: string[]
  languages: string[]
}

const same = (a: unknown, b: unknown) =>
  String(a ?? "") === String(b ?? "") ||
  (typeof b === "number" && a !== null && a !== "" && Number(a) === b)

export function Switch({
  on, onToggle, disabled, label, id,
}: {
  on: boolean
  onToggle: () => void
  disabled?: boolean
  label?: string
  id?: string
}) {
  useUILanguage()
  return (
    <button
      id={id}
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={translate(label)}
      disabled={disabled}
      onClick={onToggle}
      className={cn(
        "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        "disabled:cursor-not-allowed disabled:opacity-60",
        on ? "bg-accent-fill" : "bg-fill-strong",
      )}
    >
      <span
        className={cn(
          "inline-block size-5 rounded-full bg-surface shadow transition-transform",
          on ? "translate-x-5.5 rtl:-translate-x-5.5" : "translate-x-0.5 rtl:-translate-x-0.5",
        )}
      />
    </button>
  )
}

function Control({
  spec, value, disabled, onChange, sources,
}: {
  spec: SettingSpec
  value: unknown
  disabled: boolean
  onChange: (v: unknown) => void
  sources: OptionSources
}) {
  const { t } = useT()
  const id = `setting-${spec.key}`
  if (spec.type === "check")
    return (
      <Switch
        id={id}
        on={Boolean(Number(value ?? 0))}
        disabled={disabled}
        onToggle={() => onChange(Number(value ?? 0) ? 0 : 1)}
      />
    )
  if (spec.type === "image")
    return <ImageField hint="" value={String(value ?? "")} onChange={(v) => onChange(v)} />
  if (spec.type === "select") {
    let options = spec.options ?? []
    if (spec.options_source) options = sources[spec.options_source] ?? []
    const v = spec.key === "allowance_approval" ? String(value || "Off") : String(value ?? "")
    if (v && !options.includes(v)) options = [v, ...options]
    return (
      <select
        id={id}
        className={inputCls}
        value={v}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        {(!v || spec.default === "") && <option value="">-</option>}
        {options.map((o) => (
          <option key={o} value={o}>
            {o ? t(o) : "-"}
          </option>
        ))}
      </select>
    )
  }
  if (spec.type === "textarea") {
    const text = String(value ?? "")
    const display = spec.key === "rate_override_limits_by_role"
      ? text.split("\n").map((line) => line.split(":", 1)[0]?.trim() === SERVICE_ROLE
        ? line.replace(SERVICE_ROLE, "AI Assistant") : line).join("\n")
      : text
    return (
      <textarea
        id={id}
        className={`${inputCls} min-h-24`}
        value={display}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      />
    )
  }
  if (spec.type === "number") {
    const unit = MONEY_KEYS.has(spec.key) ? cur() : spec.unit
    return (
      <div className="flex items-center gap-2">
        <input
          id={id}
          type="number"
          inputMode="decimal"
          className={cn(inputCls, "max-w-36 tabular-nums")}
          value={value === null || value === undefined ? "" : String(value)}
          min={spec.min}
          max={spec.max}
          step="any"
          disabled={disabled}
          onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
        />
        {unit && <span className="text-sm text-muted-ink">{t(unit)}</span>}
      </div>
    )
  }
  return (
    <input
      id={id}
      type={spec.type === "password" ? "password" : spec.type === "time" ? "time" : spec.type === "email" ? "email" : "text"}
      className={cn(inputCls, spec.type === "time" && "max-w-36")}
      value={String(value ?? "")}
      disabled={disabled}
      onChange={(e) => onChange(e.target.value)}
    />
  )
}

function SettingRow({
  spec, value, saved, disabled, highlight, onChange, sources, taxIdLabel, inactive,
}: {
  spec: SettingSpec
  value: unknown
  saved: unknown
  disabled: boolean
  highlight: boolean
  onChange: (v: unknown) => void
  sources: OptionSources
  taxIdLabel?: string
  inactive?: boolean
}) {
  const { t } = useT()
  const dirty = !same(value, saved)
  const hasDefault = spec.default !== null && spec.default !== undefined && spec.default !== ""
  const offDefault = hasDefault && !same(value, spec.default)
  const label = spec.key === "gstin" && taxIdLabel ? taxIdLabel : spec.label
  const fmtDefault = (d: unknown) =>
    spec.type === "check"
      ? Number(d) ? t("On") : t("Off")
      : `${String(d)}${spec.unit && spec.type === "number" ? ` ${t(spec.unit)}` : ""}`
  return (
    <div
      id={`row-${spec.key}`}
      className={cn(
        "grid scroll-mt-24 gap-x-8 gap-y-2 px-5 py-4 transition-colors md:grid-cols-[minmax(0,2fr)_minmax(0,3fr)]",
        highlight && "bg-accent-subtle/70",
        inactive && "opacity-65",
      )}
    >
      <div className="min-w-0">
        <label htmlFor={`setting-${spec.key}`} className="flex items-center gap-2 text-sm font-medium text-ink">
          {t(label)}
          {dirty && (
            <span className="size-1.5 rounded-full bg-amber-500" title={t("Unsaved")} />
          )}
        </label>
        {(inactive || spec.hint) && <p className="mt-0.5 text-xs leading-relaxed text-muted-ink">
          {inactive ? t("Available when allowance approval is on.") : t(spec.hint)}
        </p>}
      </div>
      <div className="min-w-0">
        <Control spec={spec} value={value} disabled={disabled} onChange={onChange} sources={sources} />
        {offDefault && !disabled && (
          <button
            type="button"
            onClick={() => onChange(spec.default)}
            className="mt-1.5 inline-flex items-center gap-1 text-xs text-muted-ink hover:text-accent-ink"
          >
            <RotateCcw className="size-3" aria-hidden />
            {t("Reset to default ({value})", { value: fmtDefault(spec.default) })}
          </button>
        )}
      </div>
    </div>
  )
}

export function ModulePage({
  module, sources, focusKey, taxIdLabel, onSaved, onDirtyChange, canManageStaff, children,
}: {
  module: SettingsModule
  sources: OptionSources
  focusKey?: string | null
  taxIdLabel?: string
  onSaved: (values: Record<string, unknown>) => void
  onDirtyChange: (dirty: boolean) => void
  canManageStaff?: boolean
  /** extra cards a module owns (rate cards, hurdle tiers) */
  children?: React.ReactNode
}) {
  const { t } = useT()
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const readOnly = !module.can_edit
  const value = (key: string) => key in draft ? draft[key] : module.values[key]
  const finance = module.id === "finance"
  const approval = value("allowance_approval")
  const approvalOff = finance && (!approval || approval === "Off")
  const approvalSpec = module.sections.flatMap((section) => section.settings).find((spec) => spec.key === "allowance_approval")
  const controlGap = approvalOff && approvalSpec
  const readiness = module.cashier_readiness
  const tolerance = Number(module.values.till_variance_tolerance ?? 0)

  // another page drops the draft (save clears it itself)
  useEffect(() => {
    setDraft({})
    setMsg(null)
  }, [module.id])

  const changed = useMemo(
    () => Object.keys(draft).filter((k) => !same(draft[k], module.values[k])),
    [draft, module.values],
  )
  useEffect(() => onDirtyChange(changed.length > 0), [changed.length, onDirtyChange])

  useEffect(() => {
    if (!focusKey) return
    document.getElementById(`row-${focusKey}`)?.scrollIntoView({ block: "center", behavior: "smooth" })
  }, [focusKey, module.id])

  async function save() {
    setBusy(true)
    setMsg(null)
    try {
      const res = await saveSettings(
        module.id,
        Object.fromEntries(changed.map((k) => [k, draft[k]])),
      )
      onSaved(res.values)
      setDraft({})
      setMsg({ ok: true, text: t("Saved") })
    } catch (e) {
      setMsg({ ok: false, text: serverError(e) })
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-6 pb-24">
      {module.id === "revenue" && <Link to="/tariffs" className="text-accent-ink underline">{t("Contract tariffs")}</Link>}
      {readOnly && (
        <p className="flex items-center gap-2 rounded-lg border border-line bg-canvas px-3 py-2 text-sm text-secondary-ink">
          <Lock className="size-4 text-muted-ink" aria-hidden />
          {t("View only. {roles} can change these.", { roles: module.editors.map((role) => role === SERVICE_ROLE ? t("AI assistant") : t(role)).join(", ") })}
        </p>
      )}

      {module.sections.map((sec) => (
        <section key={sec.id} aria-labelledby={`sec-${module.id}-${sec.id}`}>
          <div className="mb-2 px-1">
            <h3 id={`sec-${module.id}-${sec.id}`} className="text-sm font-semibold text-ink">
              {t(sec.title)}
            </h3>
            {sec.description && <p className="text-xs text-muted-ink">{t(sec.description)}</p>}
          </div>
          {finance && sec.id === "cashier" && readiness && (
            <div className="mb-3 flex flex-wrap items-center gap-x-5 gap-y-2 rounded-lg border border-line bg-canvas px-4 py-3 text-sm">
              <dl className="flex flex-wrap gap-x-5 gap-y-2">
                <div><dt className="text-xs text-muted-ink">{t("Configured cashiers")}</dt><dd>{readiness.configured}</dd></div>
                <div><dt className="text-xs text-muted-ink">{t("PINs enrolled")}</dt><dd>{readiness.pin_enrolled}</dd></div>
                <div><dt className="text-xs text-muted-ink">{t("Cashier PIN")}</dt><dd>{Number(module.values.require_cashier_pin) ? t("On") : t("Off")}</dd></div>
                <div><dt className="text-xs text-muted-ink">{t("Till variance tolerance")}</dt><dd>{Number(module.values.use_cashier_tills ?? 1) && tolerance > 0 ? fmtMoney(tolerance) : t("Off")}</dd></div>
              </dl>
              <div className="ms-auto flex flex-wrap gap-3 font-medium text-accent-text">
                <a href="#cashier-limits" className="hover:underline">{t("Cashier limits")}</a>
                {canManageStaff && <Link to="/settings/staff" className="hover:underline">{t("Staff & access")}</Link>}
              </div>
            </div>
          )}
          {sec.id === "cashier" && controlGap && (
            <div role="alert" className="mb-3 flex flex-wrap items-center gap-3 rounded-lg border border-warning-line-strong bg-warning-subtle p-4 text-sm text-warning-ink">
              <TriangleAlert className="size-5 shrink-0" aria-hidden />
              <div className="min-w-0 flex-1">
                <p className="font-semibold">{t("Control gap")}</p>
                <p>{t("Front desk staff can post folio allowances without manager approval.")}</p>
              </div>
              {!readOnly && approvalSpec.can_edit !== false && (
                <button type="button" disabled={busy}
                  className="rounded-md border border-current px-3 py-1.5 font-medium hover:underline disabled:opacity-60"
                  onClick={() => {
                    setMsg(null)
                    setDraft((d) => ({ ...d, allowance_approval: "Hotel Admin or Finance" }))
                  }}>
                  {t("Enable manager approval")}
                </button>
              )}
            </div>
          )}
          <Card className="divide-y divide-line-subtle overflow-hidden">
            {sec.settings.map((s) => (
              <SettingRow
                key={s.key}
                spec={s}
                value={value(s.key)}
                saved={module.values[s.key]}
                inactive={approvalOff && s.key === "allowance_approval_above"}
                disabled={readOnly || busy || s.can_edit === false || (approvalOff && s.key === "allowance_approval_above")}
                highlight={focusKey === s.key}
                sources={sources}
                taxIdLabel={taxIdLabel}
                onChange={(v) => {
                  setMsg(null)
                  setDraft((d) => ({ ...d, [s.key]: v }))
                }}
              />
            ))}
          </Card>
        </section>
      ))}

      {children}

      {module.related.length > 0 && (
        <section>
          <h3 className="mb-2 px-1 text-sm font-semibold text-ink">{t("Managed on their own screens")}</h3>
          <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {module.related.map((r) => (
              <Link
                key={r.to}
                to={r.to}
                className="group flex items-center justify-between rounded-xl border border-line bg-surface px-4 py-3 text-sm font-medium text-secondary-ink shadow-sm hover:border-accent-line-strong hover:text-accent-ink"
              >
                {t(r.label)}
                <ArrowUpRight className="size-4 text-muted-ink group-hover:text-accent-text" aria-hidden />
              </Link>
            ))}
          </div>
        </section>
      )}

      {/* contextual save bar - only while there is something to save or report */}
      {(changed.length > 0 || msg) && !readOnly && (
        <div className="sticky bottom-4 z-30">
          <div
            role="status"
            className="flex flex-wrap items-center gap-3 rounded-xl border border-line bg-surface/95 px-4 py-3 shadow-lg backdrop-blur"
          >
            {changed.length > 0 && (
              <span className="text-sm text-secondary-ink">
                {changed.length === 1
                  ? t("1 unsaved change")
                  : t("{n} unsaved changes", { n: changed.length })}
              </span>
            )}
            {msg && (
              <span className={cn("text-sm", msg.ok ? "text-success-ink" : "text-danger-text")}>{translate(msg.text)}</span>
            )}
            <div className="ms-auto flex gap-2">
              {changed.length > 0 ? (
                <>
                  <button
                    type="button"
                    onClick={() => {
                      setDraft({})
                      setMsg(null)
                    }}
                    disabled={busy}
                    className="rounded-lg px-3 py-1.5 text-sm font-medium text-secondary-ink hover:bg-subtle"
                  >
                    {t("Discard")}
                  </button>
                  <button
                    type="button"
                    onClick={save}
                    disabled={busy}
                    className="rounded-lg bg-accent-fill px-4 py-1.5 text-sm font-medium text-on-solid hover:bg-accent-fill-hover disabled:opacity-60"
                  >
                    {busy ? t("Saving…") : t("Save changes")}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => setMsg(null)}
                  className="rounded-lg px-3 py-1.5 text-sm text-muted-ink hover:bg-subtle"
                >
                  {t("Dismiss")}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
