import assert from "node:assert/strict"
import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import ts from "typescript"
import { fixturePage, settled, withFixture } from "./white-label-routes.mjs"

const source = readFileSync(new URL("../src/lib/apps.ts", import.meta.url), "utf8")
const modules = [...source.matchAll(/^    id: "([^"]+)",/gm)].map((m) => m[1])
assert.ok(modules.includes("fnb") && modules.includes("admin"))
const settingsModules = [...readFileSync(new URL("../../kamra/settings.py", import.meta.url), "utf8")
  .matchAll(/\bModule\(\s*"([^"]+)",/g)].map((m) => m[1])
assert.ok(settingsModules.length >= 10)

const require = createRequire(import.meta.url)
const imports = {
  react: require("react"),
  "lucide-react": require("lucide-react"),
  "./api": {},
  "./brand": { deskLabel: () => undefined, upstreamOnly: () => false, whiteLabel: () => true },
  "./routing": { toFullPath: (path) => `/kamra${path}` },
}
const appRegistry = {}
const js = ts.transpileModule(source.replaceAll("import.meta.env.PROD", "true"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText
new Function("require", "exports", js)((name) => {
  assert.ok(Object.hasOwn(imports, name), `Unexpected registry import ${name}`)
  return imports[name]
}, appRegistry)
const { appForPath, visibleApps } = appRegistry
const all = visibleApps(["Hotel Admin"])
const off = visibleApps(["Hotel Admin"], ["front-desk", "admin"])
assert.equal(appForPath("/settings/fnb", off).id, "admin")
assert.equal(appForPath("/settings/fnb", all).id, "fnb")
for (const id of settingsModules) {
  const path = `/settings/${id}`
  const app = appForPath(path, all)
  assert.ok(app.id === "admin" || app.items.some(({ to }) => to === path), path)
}
console.log("Settings ownership: disabled F&B stays in Admin; enabled F&B owns its settings")
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
      await settled(page, "/hotel/settings/fnb")
      await page.getByText(enabled ? "Module on" : "Module off", { exact: true }).waitFor()
      assert.equal(new URL(page.url()).pathname, "/hotel/settings/fnb")
      await page.getByRole("heading", { name: "External POS", exact: true }).waitFor()
      const limit = page.getByLabel("Largest room charge from an external POS", { exact: true })
      assert.equal(await limit.inputValue(), "150000")
      assert.equal(await limit.isEditable(), true)
      const links = await page.locator("main a[href]").evaluateAll((anchors) => anchors.map((a) => a.getAttribute("href")))
      const internal = links.filter((link) => internalScreens.some((s) => link === `/hotel/${s}`))
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
