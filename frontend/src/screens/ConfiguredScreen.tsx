import { useCallback, useEffect, useMemo, useState } from "react"
import { ResourceScreen, type ScreenConfig } from "../components/ResourceScreen"
import { ManageTariffsLink } from "../components/ManageTariffsLink"
import { Link } from "react-router-dom"
import { useT } from "../lib/i18n"
import { call, frappeFetch, getCurrentProperty } from "../lib/api"
import { useLocale } from "../lib/money"
import { listResource } from "../lib/resource"
import { notifyTariffsChanged, onSettingsChanged, onTariffsChanged } from "../lib/tariffSync"
import * as configs from "./configs"

export type ConfigName =
  | {
      [K in keyof typeof configs]: (typeof configs)[K] extends ScreenConfig ? K : never
    }[keyof typeof configs]
  | "roomTypesConfig"

/** A ResourceScreen picked by config name, so the generic list screens and
 *  their configs load with the first route that needs them, not the shell. */
export default function ConfiguredScreen({ config }: { config: ConfigName }) {
  if (config === "roomTypesConfig") return <RoomTypesScreen />
  if (config === "ratePlansConfig") return <RatePlansScreen />
  const screen = configs[config]
  if (bridgeFields[screen.doctype]) return <BridgeScreen key={screen.doctype} config={screen} />
  if (["Season", "Holiday Package", "Long Stay Discount"].includes(screen.doctype))
    return <ResourceScreen config={screen} headerAction={<PrecedenceNotice />} />
  return <ResourceScreen config={screen} />
}

const bridgeFields: Record<string, [string, string, string]> = {
  "Rate Plan": ["custom_tariff_price_list", "Price List", "Price List"],
  "Corporate Account": ["customer", "Customer", "Customer"],
  "Travel Agent": ["customer", "Customer", "Customer"],
  "Channel Room Mapping": ["external_currency", "Currency", "Currency"],
  "Transaction Code": ["custom_erp_item", "Item", "Item"],
}

function BridgeScreen({ config, ...props }: React.ComponentProps<typeof ResourceScreen>) {
  const [field, label, target] = bridgeFields[config.doctype]
  const [available, setAvailable] = useState<boolean | null>(null)
  useEffect(() => {
    let live = true
    async function load() {
      try {
        const response = await frappeFetch<{data: {fields: {fieldname: string}[]}}>(
          `/api/v2/doctype/${encodeURIComponent(config.doctype)}/meta`)
        if (live) setAvailable(response.data.fields.some(f => f.fieldname === field))
      } catch { if (live) setAvailable(false) }
    }
    void load()
    return () => { live = false }
  }, [config.doctype, field])
  const extended = useMemo<ScreenConfig>(() => available ? { ...config,
    searchFields: field === "customer" ? [...(config.searchFields ?? []), field] : config.searchFields,
    form: [...config.form, { field, label, type: "link", linkDoctype: target }],
    columns: [...config.columns, { field, label, render: row => {
      const value = String(row[field] || "")
      const url = field === "customer" ? "/tariffs?customer=" : field === "custom_tariff_price_list" ? "/tariffs?price_list=" : ""
      return url && value ? <Link className="text-accent-ink underline" onClick={e => e.stopPropagation()}
        to={url + encodeURIComponent(value)}>{value}</Link> : value
    } }],
  } : config, [config, available, field, label, target])
  return available === null ? null : <ResourceScreen {...props} config={extended} />
}

function PrecedenceNotice() {
  const { t } = useT()
  const property = getCurrentProperty()
  const [active, setActive] = useState(false)
  useEffect(() => {
    let live = true
    call<{all_active_plans_tariff: boolean}>("kamra.rates.room_types_summary", { property })
      .then(r => { if (live) setActive(!!r.all_active_plans_tariff) }).catch(() => {})
    return () => { live = false }
  }, [property])
  return active ? <p className="rounded border border-accent-line p-2 text-sm">
    {t("Contract tariffs take precedence over card, season and package rates.")} <Link className="underline" to="/tariffs">{t("Contract tariffs")}</Link>
  </p> : null
}

function RoomTypesScreen() {
  const { currency, tax_basis, erp_tax_managed } = useLocale()
  const property = getCurrentProperty()
  const [summary, setSummary] = useState<configs.RoomTypesSummary | null>(null)
  const [rev, setRev] = useState(0)
  useEffect(() => {
    const bump = () => setRev((v) => v + 1)
    const offTariffs = onTariffsChanged(bump)
    const offSettings = onSettingsChanged(bump)
    return () => { offTariffs(); offSettings() }
  }, [])
  useEffect(() => {
    if (!property) {
      setSummary(null)
      return
    }
    let live = true
    call<configs.RoomTypesSummary>("kamra.rates.room_types_summary", { property })
      .then((res) => { if (live) setSummary(res) })
      .catch(() => { if (live) setSummary(null) })
    return () => { live = false }
  }, [property, rev])
  return (
    <ResourceScreen
      config={configs.roomTypesConfig(currency, tax_basis, summary, erp_tax_managed)}
      onReload={() => setRev((v) => v + 1)}
    />
  )
}

type PlanMeta = {
  name: string
  currency?: string | null
  base_rate_plan?: string | null
}

type TariffDirectory = {
  property_currency?: string | null
  price_lists?: { name: string; currency?: string | null; rate_plans?: string[] }[]
  rate_plan_policies?: Record<string, { currency?: string | null; base_rate_plan?: string | null }>
}

function RatePlansScreen() {
  const { currency: localeCurrency } = useLocale()
  const [plans, setPlans] = useState<PlanMeta[]>([])
  const [tariffs, setTariffs] = useState<TariffDirectory | null>(null)
  const [reloadTick, setReloadTick] = useState(0)

  const property = getCurrentProperty()

  useEffect(() => {
    let active = true
    listResource("Rate Plan", {
      fields: ["name", "currency", "base_rate_plan"],
      filters: [["property", "=", property]],
      limit: 500,
      orderBy: "name asc",
    })
      .then((rows) => {
        if (!active) return
        setPlans(
          rows.map((r) => ({
            name: String(r.name),
            currency: r.currency ? String(r.currency) : "",
            base_rate_plan: r.base_rate_plan ? String(r.base_rate_plan) : "",
          })),
        )
      })
      .catch(() => {})

    call<TariffDirectory>("kamra.rates.list_contract_tariffs", { property })
      .then((res) => {
        if (active && res) setTariffs(res)
      })
      .catch(() => {})

    return () => {
      active = false
    }
  }, [property, reloadTick])

  const propertyCurrency = tariffs?.property_currency || localeCurrency || ""

  const { planMap, tariffByPlan, tariffByList } = useMemo(() => {
    const pMap = new Map<string, PlanMeta>()
    for (const p of plans) pMap.set(p.name, p)
    const byPlan = new Map<string, string>()
    const byList = new Map<string, string>()
    for (const pl of tariffs?.price_lists ?? []) {
      const cur = pl.currency ? String(pl.currency) : ""
      if (!cur) continue
      byList.set(pl.name, cur)
      for (const rp of pl.rate_plans ?? []) byPlan.set(rp, cur)
    }
    for (const [rp, policy] of Object.entries(tariffs?.rate_plan_policies ?? {})) {
      if (policy?.currency && !byPlan.has(rp)) byPlan.set(rp, String(policy.currency))
    }
    return { planMap: pMap, tariffByPlan: byPlan, tariffByList: byList }
  }, [plans, tariffs])

  const tariffCurrencyFor = useCallback(
    (draft: Record<string, unknown>) => {
      const name = draft.name ? String(draft.name) : ""
      if (name && tariffByPlan.has(name)) return tariffByPlan.get(name)!
      const pl = draft.custom_tariff_price_list ? String(draft.custom_tariff_price_list) : ""
      if (pl && tariffByList.has(pl)) return tariffByList.get(pl)!
      return ""
    },
    [tariffByPlan, tariffByList],
  )

  const baseCurrencyFor = useCallback(
    (baseName: string) => {
      const seen = new Set<string>()
      let curName = baseName
      while (curName && !seen.has(curName)) {
        seen.add(curName)
        const plan = planMap.get(curName)
        if (plan?.currency) return plan.currency
        const tariffCur = tariffByPlan.get(curName)
        if (tariffCur) return tariffCur
        curName = plan?.base_rate_plan || tariffs?.rate_plan_policies?.[curName]?.base_rate_plan || ""
      }
      return propertyCurrency
    },
    [planMap, tariffByPlan, tariffs, propertyCurrency],
  )

  const config = useMemo(
    () =>
      configs.ratePlansScreenConfig({
        propertyCurrency,
        tariffCurrencyFor,
        baseCurrencyFor,
      }),
    [propertyCurrency, tariffCurrencyFor, baseCurrencyFor],
  )

  const handleSaved = useCallback(() => {
    setReloadTick((n) => n + 1)
    notifyTariffsChanged(property)
  }, [property])

  return <BridgeScreen config={config} headerAction={<ManageTariffsLink />} onSaved={handleSaved} />
}
