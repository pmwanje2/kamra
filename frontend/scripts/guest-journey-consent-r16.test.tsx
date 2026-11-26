// @vitest-environment jsdom
import { act } from "react"
import { createRoot } from "react-dom/client"
import { MemoryRouter, Outlet, Route, Routes } from "react-router-dom"
import { afterEach, expect, it, vi } from "vitest"
import GuestJourney from "../src/screens/GuestJourney"
import { call } from "../src/lib/api"
import { setLang } from "../src/lib/dir"
import { t } from "../src/lib/i18n"
import { ensureDictionary } from "../src/lib/locales"

vi.mock("../src/lib/api", async (original) => ({ ...await original<typeof import("../src/lib/api")>(), call: vi.fn() }))
vi.mock("../src/lib/auth", () => ({ useAuth: () => ({ roles: ["Front Desk"] }) }))
vi.mock("../src/components/GuestDocuments", () => ({ default: () => null, GuestAvatar: () => null }))

afterEach(() => {
  setLang("en")
  vi.unstubAllGlobals()
})

it.each([
  ["en", "Desk", "Reception"],
  ["en", null, "Reception"],
  ["fr", "", "Réception"],
  ["fr", "Website", "Site web"],
  ["es", "Desk", "Recepción"],
  ["ar", "Desk", "الاستقبال"],
] as const)("shows dated consent in %s for source %s", async (language, source, expected) => {
  vi.stubGlobal("IS_REACT_ACT_ENVIRONMENT", true)
  vi.stubGlobal("localStorage", jsdom.window.localStorage)
  await ensureDictionary(language)
  setLang(language)
  vi.mocked(call).mockResolvedValue({
    guest: { name: "R16", full_name: "Guest", marketing_consent: 1, marketing_consent_date: "2031-03-12",
      marketing_consent_source: source, notes: "" },
    stats: { bookings: 0, stays: 0, nights: 0, lifetime_value: 0 }, documents: {}, reservations: [], timeline: [],
  })
  const host = document.createElement("div")
  const root = createRoot(host)
  try {
    await act(async () => root.render(<MemoryRouter initialEntries={["/guests/R16"]}><Routes>
      <Route element={<Outlet context={{ canBook: false }} />}><Route path="/guests/:name" element={<GuestJourney />} /></Route>
    </Routes></MemoryRouter>))
    expect(host.textContent).toContain(t("Opted in on {v0} ({v1})", { v0: "2031-03-12", v1: expected }))
  } finally {
    await act(async () => root.unmount())
  }
})
