import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const source = readFileSync(new URL("../src/lib/apps.ts", import.meta.url), "utf8")
const modules = [...source.matchAll(/^    id: "([^"]+)",/gm)].map((m) => m[1])
assert.ok(modules.includes("fnb") && modules.includes("admin"))
const settingsModules = [...readFileSync(new URL("../../kamra/settings.py", import.meta.url), "utf8")
  .matchAll(/\bModule\(\s*"([^"]+)",/g)].map((m) => m[1])
assert.ok(settingsModules.length >= 10)
const internalScreens = ["outlets", "menu-items", "ingredients", "inventory", "kitchen", "printers", "pos", "pos-dashboard"]
const fnb = {
  id: "fnb", title: "F&B", description: "Restaurant settings", group: "modules", toggle: true,
  enabled: false, editors: ["Hotel Admin"], can_edit: true,
  related: internalScreens.map((s) => ({ label: s, to: `/${s}` })),
  sections: [
    { id: "kot", title: "Kitchen orders (KOT)", settings: [{ key: "pos_auto_kot", type: "check", label: "Auto-accept and print KOT", default: 0 }] },
    { id: "external_pos", title: "External POS", settings: [{ key: "pos_room_charge_limit", type: "number", label: "Largest room charge from an external POS", default: 0 }] },
  ],
  values: { pos_room_charge_limit: 150000 },
}

await withFixture(async (browser, origin) => {
  for (const enabled of [false, true]) {
    const ids = enabled ? [] : ["front-desk", "admin"]
    const page = await fixturePage(browser, origin, "Hotel Admin", { width: 1280, height: 800 }, {
      "kamra.api.enabled_modules": ids,
      "kamra.settings.settings_schema": { modules: [{ ...fnb, enabled }], enabled_modules: ids, is_admin: true, is_it_admin: true },
    })
    try {
      // Vite serves at /; /settings/fnb is the same route under the deployed /hotel mask.
      await settled(page, "/settings/fnb")
      await page.getByText(enabled ? "Module on" : "Module off", { exact: true }).waitFor()
      const ownership = await page.evaluate(async (ids) => {
        const { appForPath, visibleApps } = await import("/src/lib/apps.ts")
        const all = visibleApps(["Hotel Admin"])
        const off = visibleApps(["Hotel Admin"], ["front-desk", "admin"])
        return {
          disabled: appForPath("/settings/fnb", off).id,
          enabled: appForPath("/settings/fnb", all).id,
          missing: ids.filter((id) => {
            const path = `/settings/${id}`
            const app = appForPath(path, all)
            return app.id !== "admin" && !app.items.some(({ to }) => to === path)
          }),
        }
      }, settingsModules)
      assert.deepEqual(ownership, { disabled: "admin", enabled: "fnb", missing: [] })
      console.log("Settings ownership: disabled F&B stays in Admin; enabled F&B owns its settings")
      assert.equal(new URL(page.url()).pathname, "/settings/fnb")
      await page.getByRole("heading", { name: "External POS", exact: true }).waitFor()
      const limit = page.getByLabel("Largest room charge from an external POS", { exact: true })
      assert.equal(await limit.inputValue(), "150000")
      assert.equal(await limit.isEditable(), true)
      const links = await page.locator("main a[href]").evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href")))
      const internal = links.filter((link) => internalScreens.some((s) => link === `/${s}`))
      if (enabled) {
        assert.ok(internal.length, "Enabled F&B should retain working links")
      } else {
        assert.deepEqual(internal, [], "Disabled F&B exposes dead internal POS links")
        assert.equal(await page.getByRole("heading", { name: "Kitchen orders (KOT)" }).count(), 0)
      }
    } finally {
      await page.context().close()
    }
  }
})
console.log("F&B settings: on/off state, external POS limit, and internal link visibility")
