import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import ts from "typescript"
import { describe, expect, it } from "vitest"

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..")

/** Physical directional Tailwind utilities that break Arabic/RTL mirroring. */
const PHYSICAL_RTL_CLASS =
  /(?<![\w-])(?:(?:sm|md|lg|xl|2xl|hover|focus|focus-visible|active|disabled|group-hover|dark|print|rtl|ltr):)*(?:(?:ml|mr|pl|pr|left|right)-(?:\d+\/\d+|\d+(?:\.\d+)?|px|auto|full|\[[^\]]+\])|(?:border-[lr]|rounded-[lr])(?:-(?:\d+|none|sm|md|lg|xl|2xl|full|\[[^\]]+\]))?|text-(?:left|right))(?![\w-])/g

/**
 * PR 3 owned surfaces MUST have strictly ZERO physical directional classes.
 */
const PR3_ZERO_FILES = [
  "src/components/ui/sheet.tsx",
  "src/components/HelpPanel.tsx",
  "src/screens/BanquetMonth.tsx",
  "src/screens/banquet/shared.tsx",
  "src/screens/CashierBoard.tsx",
  "src/screens/CashierSessions.tsx",
  "src/screens/CashierShiftReport.tsx",
  "src/screens/CashierTill.tsx",
  "src/screens/settings/cards.tsx",
]

/**
 * Monotonic Cross-PR Ratchet: files owned by parallel PRs or future sweeps
 * (e.g., AppShell.tsx -> PR 1, TapeChart.tsx -> PR 2, FolioView.tsx/FolioHistory.tsx -> Pack 2 PR 4).
 * Counts may ONLY decrease to 0; no unlisted file may introduce physical RTL classes.
 */
const PENDING_CROSS_PR_ALLOWLIST: Record<string, number> = {
  "src/AppShell.tsx": 5,
  "src/components/AgentCards.tsx": 1,
  "src/components/BookingDialog.tsx": 9,
  "src/components/CalendarView.tsx": 6,
  "src/components/CommandPalette.tsx": 1,
  "src/components/EditableNationality.tsx": 1,
  "src/components/GroupBookingDialog.tsx": 29,
  "src/components/GroupControl.tsx": 27,
  "src/components/HkMedia.tsx": 1,
  "src/components/PhoneInput.tsx": 3,
  "src/components/RateOptions.tsx": 3,
  "src/components/ResourceScreen.tsx": 16,
  "src/components/RoomChangeDialog.tsx": 5,
  "src/components/RoomTypeTariffs.tsx": 1,
  "src/components/RowCorrection.tsx": 1,
  "src/components/SettlementPreview.tsx": 1,
  "src/components/TenderPicker.tsx": 1,
  "src/components/ui/data-table.tsx": 2,
  "src/lib/markdown.tsx": 2,
  "src/screens/AccountingExport.tsx": 2,
  "src/screens/Activity.tsx": 1,
  "src/screens/Agents.tsx": 4,
  "src/screens/AllowanceVoucher.tsx": 2,
  "src/screens/AppLauncher.tsx": 3,
  "src/screens/Assistant.tsx": 4,
  "src/screens/Banquet.tsx": 26,
  "src/screens/BanquetCatalogue.tsx": 38,
  "src/screens/BanquetDiary.tsx": 9,
  "src/screens/BanquetDocument.tsx": 34,
  "src/screens/BanquetFunction.tsx": 39,
  "src/screens/BanquetRegisters.tsx": 42,
  "src/screens/Billing.tsx": 15,
  "src/screens/BookingEngine.tsx": 3,
  "src/screens/BookingQuotation.tsx": 15,
  "src/screens/CRS.tsx": 6,
  "src/screens/CancellationLetter.tsx": 1,
  "src/screens/CashForecast.tsx": 23,
  "src/screens/CityLedgerPayment.tsx": 21,
  "src/screens/CurrencyDesk.tsx": 2,
  "src/screens/Dashboard.tsx": 29,
  "src/screens/FolioHistory.tsx": 1,
  "src/screens/FolioView.tsx": 48,
  "src/screens/GoLiveImport.tsx": 29,
  "src/screens/GroupQuotation.tsx": 34,
  "src/screens/GuestJourney.tsx": 12,
  "src/screens/Guests.tsx": 16,
  "src/screens/HkApp.tsx": 3,
  "src/screens/HkLaundry.tsx": 4,
  "src/screens/HousekeepingBoard.tsx": 2,
  "src/screens/InHouse.tsx": 4,
  "src/screens/Inventory.tsx": 19,
  "src/screens/Kitchen.tsx": 7,
  "src/screens/Laundry.tsx": 22,
  "src/screens/Login.tsx": 1,
  "src/screens/ManagementBooks.tsx": 3,
  "src/screens/Marketplace.tsx": 2,
  "src/screens/MenuItems.tsx": 2,
  "src/screens/OpsSLA.tsx": 59,
  "src/screens/POS.tsx": 31,
  "src/screens/Payables.tsx": 40,
  "src/screens/PettyCash.tsx": 1,
  "src/screens/PosDashboard.tsx": 2,
  "src/screens/PublicCheckin.tsx": 1,
  "src/screens/PublicListing.tsx": 2,
  "src/screens/Receivables.tsx": 43,
  "src/screens/RecipeManager.tsx": 11,
  "src/screens/RegistrationCard.tsx": 3,
  "src/screens/Reports.tsx": 21,
  "src/screens/ReservationDetail.tsx": 1,
  "src/screens/RevenueReports.tsx": 46,
  "src/screens/RoomsSetup.tsx": 4,
  "src/screens/Setup.tsx": 3,
  "src/screens/TapeChart.tsx": 7,
  "src/screens/Today.tsx": 2,
  "src/screens/TripConfirmation.tsx": 1,
  "src/screens/UpdatePassword.tsx": 1,
  "src/screens/VoidAllowanceReport.tsx": 1,
  "src/screens/WhatsAppChat.tsx": 5,
  "src/screens/banquet/Economics.tsx": 52,
  "src/screens/banquet/MenuComposer.tsx": 1,
  "src/screens/banquet/Steps.tsx": 2,
  "src/screens/laundry/GuestLaundryCard.tsx": 1,
  "src/screens/pos/MoveRoomBill.tsx": 1,
  "src/screens/pos/OpenBillsPopup.tsx": 2,
  "src/screens/pos/PosSidePanel.tsx": 1,
}

/** Fixed 80mm thermal receipt CSS is print-layout only and exempt from RTL screen mirroring. */
const EXEMPT_FILES = new Set(["src/lib/thermal.ts"])

function walkTsFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walkTsFiles(full, out)
    else if (/\.tsx?$/.test(entry.name) && !entry.name.endsWith(".d.ts")) out.push(full)
  }
  return out
}

function scanPhysicalClasses(fullPath: string): string[] {
  const sf = ts.createSourceFile(fullPath, fs.readFileSync(fullPath, "utf8"), ts.ScriptTarget.Latest, true)
  const hits: string[] = []
  function visit(node: ts.Node) {
    if (ts.isStringLiteralLike(node) || ts.isTemplateLiteralToken(node)) {
      const matches = node.text.match(PHYSICAL_RTL_CLASS)
      if (matches) {
        const line = sf.getLineAndCharacterOfPosition(node.getStart()).line + 1
        for (const cls of matches) hits.push(`L${line}:${cls}`)
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(sf)
  return hits
}

describe("P58 Arabic (ar) RTL CSS logical spacing & animation guard", () => {
  it("enforces zero physical directional classes on all PR 3 owned surfaces", () => {
    const failures: string[] = []
    for (const rel of PR3_ZERO_FILES) {
      const hits = scanPhysicalClasses(path.join(root, rel))
      if (hits.length) failures.push(`${rel} (${hits.length}): ${hits.join(", ")}`)
    }
    expect(failures).toEqual([])
  })

  it("enforces monotonic cross-PR ratchet across the entire frontend/src tree", () => {
    const failures: string[] = []
    for (const full of walkTsFiles(path.join(root, "src"))) {
      const rel = path.relative(root, full).split(path.sep).join("/")
      if (EXEMPT_FILES.has(rel)) continue
      const hits = scanPhysicalClasses(full)
      const allowed = PENDING_CROSS_PR_ALLOWLIST[rel] ?? 0
      if (hits.length > allowed) {
        failures.push(`${rel}: found ${hits.length} physical RTL classes (allowed <= ${allowed}): ${hits.slice(0, 6).join(", ")}`)
      }
    }
    expect(failures).toEqual([])
  })

  it("pins Sheet drawer to inline-end (end-0) and defines RTL slide-in animation in index.css", () => {
    const sheet = fs.readFileSync(path.join(root, "src/components/ui/sheet.tsx"), "utf8")
    expect(sheet).toMatch(/\binset-y-0 end-0\b/)
    expect(sheet).not.toMatch(/\bright-0\b/)
    expect(sheet).not.toMatch(/Kamra/i)

    const css = fs.readFileSync(path.join(root, "src/index.css"), "utf8")
    expect(css).toMatch(/@keyframes\s+sheet-in-rtl\b/)
    expect(css).toMatch(/translateX\(-100%\)/)
    expect(css).toMatch(/\[dir="rtl"\]\s+\.animate-sheet-in[\s\S]*?sheet-in-rtl/)
    expect(css).toMatch(/\[dir="rtl"\]\s+select:not\(\[multiple\]\):not\(\[size\]\)/)
  })

  it("mirrors HelpPanel and BanquetMonth sticky columns and navigation chevrons for RTL", () => {
    const help = fs.readFileSync(path.join(root, "src/components/HelpPanel.tsx"), "utf8")
    expect(help).toMatch(/\bend-5\b/)
    expect(help).toMatch(/\bms-auto\b/)
    expect(help).toMatch(/\btext-start\b/)
    expect(help).not.toMatch(/\bKamra\b/)

    const month = fs.readFileSync(path.join(root, "src/screens/BanquetMonth.tsx"), "utf8")
    expect(month).toMatch(/\bsticky start-0\b/)
    expect(month).toMatch(/\bsticky start-28\b/)
    expect(month).toMatch(/<ChevronLeft[^>]*rtl:rotate-180/)
    expect(month).toMatch(/<ChevronRight[^>]*rtl:rotate-180/)
  })
})
