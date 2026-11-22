import { ResourceScreen } from "../components/ResourceScreen"
import { useLocale } from "../lib/money"
import * as configs from "./configs"

export type ConfigName = keyof typeof configs

/** A ResourceScreen picked by config name, so the generic list screens and
 *  their configs load with the first route that needs them, not the shell. */
export default function ConfiguredScreen({ config }: { config: ConfigName }) {
  if (config === "roomTypesConfig") return <RoomTypesScreen />
  return <ResourceScreen config={configs[config]} />
}

function RoomTypesScreen() {
  const { currency, tax_basis } = useLocale()
  return <ResourceScreen config={configs.roomTypesConfig(currency, tax_basis)} />
}
