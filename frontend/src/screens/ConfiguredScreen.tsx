import { useEffect, useState } from "react"
import { ResourceScreen } from "../components/ResourceScreen"
import { ManageTariffsLink } from "../components/ManageTariffsLink"
import { call, getCurrentProperty } from "../lib/api"
import { useLocale } from "../lib/money"
import * as configs from "./configs"

export type ConfigName = keyof typeof configs

/** A ResourceScreen picked by config name, so the generic list screens and
 *  their configs load with the first route that needs them, not the shell. */
export default function ConfiguredScreen({ config }: { config: ConfigName }) {
  if (config === "roomTypesConfig") return <RoomTypesScreen />
  return <ResourceScreen config={configs[config]} headerAction={config === "ratePlansConfig" ? <ManageTariffsLink /> : undefined} />
}

function RoomTypesScreen() {
  const { currency, tax_basis } = useLocale()
  const property = getCurrentProperty()
  const [summary, setSummary] = useState<configs.RoomTypesSummary | null>(null)
  const [rev, setRev] = useState(0)
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
      config={configs.roomTypesConfig(currency, tax_basis, summary)}
      onReload={() => setRev((v) => v + 1)}
    />
  )
}
