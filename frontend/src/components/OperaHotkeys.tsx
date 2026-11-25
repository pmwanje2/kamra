import { lazy, Suspense, useEffect, useState } from "react"
import { useLocation, useNavigate } from "react-router-dom"
import { useAuth } from "../lib/auth"
import { SERVICE_ROLE } from "../lib/brand"
import { HOTKEY_EVENTS, matchOperaHotkey } from "../lib/hotkeys"

const BookingDialog = lazy(() =>
  import("./BookingDialog").then((m) => ({ default: m.BookingDialog })),
)

const BOOKING_ROLES = [
  "Front Desk",
  SERVICE_ROLE,
  "Hotel Admin",
  "System Manager",
  "Administrator",
]

/** Global OPERA desk keyboard dispatcher mounted beside CommandPalette:
 *  - Alt+N: New Booking (cancelable kamra:open-booking; fallback BookingDialog if unhandled)
 *  - Alt+C: Fast Check-In (cancelable kamra:shortcut-checkin; fallback /in-house?view=arrivals)
 *  - F4: Room & Availability Search (skipped on /pos so POS settlement F4 stays intact)
 *  - Esc / Enter: overlay dismissal and quick-cashier submit */
export function OperaHotkeys({
  onOpenRoomSearch,
}: {
  onOpenRoomSearch: () => void
}) {
  const { roles } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  const [fallbackBooking, setFallbackBooking] = useState<Record<string, never> | null>(null)

  const canBook = BOOKING_ROLES.some((r) => roles.includes(r))

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const action = matchOperaHotkey(e, location.pathname)
      if (!action) return

      if (action === "new-booking") {
        e.preventDefault()
        const ev = new CustomEvent(HOTKEY_EVENTS.openBooking, {
          cancelable: true,
          detail: {},
        })
        window.dispatchEvent(ev)
        if (!ev.defaultPrevented && canBook) {
          setFallbackBooking({})
        }
        return
      }

      if (action === "fast-checkin") {
        e.preventDefault()
        const ev = new CustomEvent(HOTKEY_EVENTS.shortcutCheckIn, {
          cancelable: true,
          detail: {},
        })
        window.dispatchEvent(ev)
        if (!ev.defaultPrevented) {
          navigate("/in-house?view=arrivals")
        }
        return
      }

      if (action === "room-search") {
        e.preventDefault()
        window.dispatchEvent(
          new CustomEvent(HOTKEY_EVENTS.openPalette, { detail: { mode: "room" } }),
        )
        onOpenRoomSearch()
        return
      }

      if (action === "escape") {
        if (fallbackBooking) {
          e.preventDefault()
          setFallbackBooking(null)
        }
        return
      }

      if (action === "fast-cashier") {
        const ev = new CustomEvent(HOTKEY_EVENTS.fastCashier, { cancelable: true })
        window.dispatchEvent(ev)
        if (ev.defaultPrevented) {
          e.preventDefault()
          return
        }
        const submit = document.querySelector<HTMLButtonElement>(
          "[data-fast-cashier-submit]:not(:disabled)",
        )
        if (submit) {
          e.preventDefault()
          submit.click()
        }
      }
    }

    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [location.pathname, canBook, navigate, onOpenRoomSearch, fallbackBooking])

  if (!fallbackBooking || !canBook) return null

  return (
    <Suspense fallback={null}>
      <BookingDialog
        initial={fallbackBooking}
        onClose={() => setFallbackBooking(null)}
        onBooked={() => setFallbackBooking(null)}
      />
    </Suspense>
  )
}
