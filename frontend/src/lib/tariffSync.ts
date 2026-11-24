/** Cross-screen invalidation events so Contract Tariffs, Room Types, Rate
 *  Calendar, Settings and the booking dialog agree immediately without a page
 *  reload. Event names follow the existing kamra:<name> identifier pattern. */

const TARIFFS_EVENT = "kamra:tariffs-changed"
const SETTINGS_EVENT = "kamra:settings-changed"

export function notifyTariffsChanged(property?: string) {
  if (typeof window === "undefined") return
  window.dispatchEvent(new CustomEvent(TARIFFS_EVENT, { detail: { property: property || "" } }))
}

export function onTariffsChanged(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {}
  const listener = () => handler()
  window.addEventListener(TARIFFS_EVENT, listener)
  return () => window.removeEventListener(TARIFFS_EVENT, listener)
}

export function notifySettingsChanged(property?: string, module?: string) {
  if (typeof window === "undefined") return
  window.dispatchEvent(
    new CustomEvent(SETTINGS_EVENT, { detail: { property: property || "", module: module || "" } }),
  )
}

export function onSettingsChanged(handler: () => void): () => void {
  if (typeof window === "undefined") return () => {}
  const listener = () => handler()
  window.addEventListener(SETTINGS_EVENT, listener)
  return () => window.removeEventListener(SETTINGS_EVENT, listener)
}
