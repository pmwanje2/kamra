import { asset } from "./asset"

export interface Brand {
  product: string
  assistant: string
  white_label: boolean
  logo?: string | null
  favicon?: string | null
}

declare global {
  interface Window {
    kamra_brand?: Brand
  }
}

// The real site supplies this before first paint, including guest routes.
export const siteBrand = () => typeof window === "undefined" ? undefined : window.kamra_brand
export const whiteLabel = () => siteBrand()?.white_label === true
export const product = () => siteBrand()?.product ?? "Kamra"
export const assistant = () => siteBrand()?.assistant ?? "Kamra Agent"
export const brandTitle = () => whiteLabel() ? product() : `${product()} PMS`
export const SERVICE_ROLE = "Kamra Agent"
export const roleLabel = (role: string, label = role) =>
  role === SERVICE_ROLE && (whiteLabel() || assistant() !== SERVICE_ROLE) ? "{assistant} (service)" : label

export const logo = () => siteBrand()?.logo || (whiteLabel() ? null : asset("kamra-mark.svg"))
export const upstreamHome = () => whiteLabel() ? undefined : "https://kamrapms.com"
export const upstreamRepo = () => whiteLabel() ? undefined : "https://github.com/Kamra-PMS/kamra-pms"
export const upstreamDocs = () => {
  const home = upstreamHome()
  return home && `${home}/docs`
}
export const deskLabel = () => whiteLabel() ? undefined : "Frappe Desk"
export const accountingImportLabel = () => whiteLabel() ? "Accounting system import" : "ERPNext"
export const voicePartner = "HeyKoala" // Stored provider option; not a display label.
export const providerLabel = (value: string) => whiteLabel() && value === voicePartner ? "Voice partner" : value

const upstreamRoutes = new Set([
  "/marketplace", "/developers", "/setup", "/finish-setup", "/agents", "/assistant",
  "/settings/ai", "/settings/claude", "/settings/mcp-access", "/settings/verify",
])
export function upstreamOnly(path: string) {
  return whiteLabel() && upstreamRoutes.has(path.replace(/\/+$/, "").toLowerCase())
}
