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
    kamra_mcp_disabled?: boolean
    kamra_fiscal_bridge?: boolean
    kamra_brand?: Brand
  }
}

// The real site supplies this before first paint, including guest routes.
export const siteBrand = () => typeof window === "undefined" ? undefined : window.kamra_brand
export const whiteLabel = () => siteBrand()?.white_label === true
export const product = () => siteBrand()?.product?.trim() || "Hotel PMS"
export const assistant = () => {
  const name = siteBrand()?.assistant?.trim()
  return name && name !== SERVICE_ROLE ? name : "Concierge Assistant"
}
export const brandTitle = () => whiteLabel() ? product() : `${product()} PMS`
export const SERVICE_ROLE = "Kamra Agent"
export const roleLabel = (role: string, label = role) => {
  if (role === SERVICE_ROLE) {
    return "{assistant} (service)"
  }
  return label
}

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
const assistantRoutes = new Set(["/activity", "/assistant", "/agents", "/channels", "/whatsapp", "/settings/ai", "/settings/claude", "/settings/mcp-access"])
const outletRoutes = new Set(["/pos", "/pos-dashboard", "/kitchen", "/menu", "/menu-items", "/ingredients", "/inventory", "/outlets", "/printers"])
export const assistantDisabled = () => typeof window !== "undefined" && window.kamra_mcp_disabled === true
export const fiscalBridge = () => typeof window !== "undefined" && window.kamra_fiscal_bridge === true
export function upstreamOnly(path: string) {
  const route = path.replace(/\/+$/, "").toLowerCase()
  return (whiteLabel() && upstreamRoutes.has(route))
    || (assistantDisabled() && assistantRoutes.has(route))
    || (typeof window !== "undefined" && !!window.kamra_erp_pos_url && outletRoutes.has(route))
    || (fiscalBridge() && (route === "/settings/zatca" || route === "/vouchers"))
}
