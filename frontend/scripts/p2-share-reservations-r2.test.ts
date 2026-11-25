import { describe, it, expect } from "vitest"
import { shareRoomBars } from "../src/lib/shareRoomBars"
import es from "../src/i18n/locales/es.json"
import fr from "../src/i18n/locales/fr.json"
import ar from "../src/i18n/locales/ar.json"

describe("shared-room TapeChart and localization", () => {
  it("renders only active sharers per segment and gives occupancy priority to an in-house guest", () => {
    const bars = shareRoomBars([
      { name: "P", is_share_primary: 1, share_group_id: "SHARE", status: "Confirmed",
        check_in_date: "2026-11-25", check_out_date: "2026-11-27" },
      { name: "S", share_group_id: "SHARE", status: "Checked In",
        check_in_date: "2026-11-26", check_out_date: "2026-11-29" },
    ])
    expect(bars.map((stay) => [stay.name, stay.status, stay.check_in_date, stay.check_out_date, stay.share_count]))
      .toEqual([
        ["P", "Confirmed", "2026-11-25", "2026-11-26", 1],
        ["S", "Checked In", "2026-11-26", "2026-11-27", 2],
        ["S", "Checked In", "2026-11-27", "2026-11-29", 1],
      ])
  })

  it("localizes the new share controls and badge in Spanish, French and Arabic", () => {
    const keys = [
      "Add Sharer", "Combine Existing Reservation", "Break Share", "Split Equal", "Primary Pays",
      "Full Rate Each", "Share Reservations", "Primary Sharer", "Open Folio",
      "Search guests or enter a new name", "Search reservations", "Destination Room", "Guest profile",
      "Create new guest profile", "Check-in date", "Check-out date", "Custom Nightly Rate",
      "Rate Split Mode", "Save Rate Split", "Shared room with {n} reservations",
    ]
    for (const [lang, dictionary] of Object.entries({ es, fr, ar })) {
      for (const key of keys) {
        const text = (dictionary as Record<string, string>)[key]
        expect(text, `${lang}: ${key}`).toBeTruthy()
        expect(text, `${lang}: ${key}`).not.toBe(key)
        if (key.includes("{n}")) expect(text).toContain("{n}")
      }
    }
  })
})
