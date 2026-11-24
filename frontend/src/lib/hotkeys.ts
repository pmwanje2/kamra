/** Pure OPERA keyboard shortcut matcher and event names.
 *  Guards text-entry targets and POS kiosk F4 so shortcuts never hijack typing
 *  or restaurant terminal settlement keys. */

export const HOTKEY_EVENTS = {
  openBooking: "kamra:open-booking",
  shortcutCheckIn: "kamra:shortcut-checkin",
  openPalette: "kamra:open-palette",
  fastCashier: "kamra:fast-cashier",
} as const

export type OperaHotkeyAction =
  | "new-booking"
  | "fast-checkin"
  | "room-search"
  | "escape"
  | "fast-cashier"

export function isTextInputTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== "object") return false
  const el = target as Partial<HTMLElement>
  const tag = (el.tagName ?? "").toUpperCase()
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true
  if (el.isContentEditable) return true
  if (typeof el.closest === "function" && el.closest('[contenteditable="true"]')) return true
  return false
}

export function isInteractiveClickTarget(target: EventTarget | null): boolean {
  if (!target || typeof target !== "object") return false
  const el = target as Partial<HTMLElement>
  if (typeof el.closest !== "function") return false
  return Boolean(el.closest('button, a, [role="button"], [role="menuitem"], summary'))
}

export function isPosRoute(pathname: string): boolean {
  return pathname === "/pos" || pathname.startsWith("/pos/")
}

export function matchOperaHotkey(e: KeyboardEvent, pathname: string): OperaHotkeyAction | null {
  if (e.defaultPrevented) return null

  if (e.key === "Escape") {
    return "escape"
  }

  if (e.key === "F4" && !e.altKey && !e.ctrlKey && !e.metaKey) {
    if (isPosRoute(pathname)) return null
    const tag = ((e.target as Partial<HTMLElement> | null)?.tagName ?? "").toUpperCase()
    if (tag === "SELECT") return null
    return "room-search"
  }

  if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (isTextInputTarget(e.target)) return null
    const key = e.key.toLowerCase()
    if (key === "n" || e.code === "KeyN") return "new-booking"
    if (key === "c" || e.code === "KeyC") return "fast-checkin"
  }

  if (e.key === "Enter" && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
    if (isTextInputTarget(e.target) || isInteractiveClickTarget(e.target)) return null
    return "fast-cashier"
  }

  return null
}
