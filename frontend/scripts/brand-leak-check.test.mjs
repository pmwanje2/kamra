// The brand guard must see what a person sees: a vendor name joined to an
// identifier inside prose, and a hard-coded screen path. Asset and API paths
// and declared identifiers stay out of scope.
import fs from "node:fs"
import os from "node:os"
import path from "node:path"
import { afterEach, describe, expect, it } from "vitest"
import { scanTree } from "./brand-leak-check.mjs"

let dir
afterEach(() => { if (dir) fs.rmSync(dir, { recursive: true, force: true }) })

function fixture(files) {
  dir = fs.mkdtempSync(path.join(os.tmpdir(), "brand-guard-"))
  for (const [name, text] of Object.entries(files)) {
    fs.mkdirSync(path.dirname(path.join(dir, name)), { recursive: true })
    fs.writeFileSync(path.join(dir, name), text)
  }
  return dir
}

const shell = {
  "index.html": "<html><head><title>Hotel</title></head></html>",
  "src/i18n/catalog.csv": "key,english,arabic,spanish,portuguese,chinese,sources,placeholders,status\n" +
    "Phone,Phone,هاتف,Teléfono,Telefone,电话,screens/Fixture.tsx,,translated\n",
  "src/i18n/locales/es.json": "{}",
}

describe("brand leak guard", () => {
  it("flags hard-coded screen paths and identifiers inside prose", () => {
    const { findings } = scanTree(fixture({
      ...shell,
      "src/screens/Fixture.tsx": [
        'export const A = () => <a href="/kamra/assistant">Open</a>',
        'export const go = () => window.location.assign("/kamra/")',
        'export const B = () => <p>{t("MCP is off for this site (kamra_mcp_disabled).")}</p>',
        'export const C = () => <p>{t("Ask the kamra-agent desk")}</p>',
      ].join("\n"),
      "src/i18n/locales/es.json": JSON.stringify({ "MCP is off (kamra_mcp_disabled).": "MCP apagado (kamra_mcp_disabled)." }),
    }))
    expect(findings).toEqual([
      "src/screens/Fixture.tsx:1: /kamra/assistant",
      "src/screens/Fixture.tsx:2: /kamra/",
      "src/screens/Fixture.tsx:3: MCP is off for this site (kamra_mcp_disabled).",
      "src/screens/Fixture.tsx:4: Ask the kamra-agent desk",
      "es.json: MCP is off (kamra_mcp_disabled).",
      "es.json (MCP is off (kamra_mcp_disabled).): MCP apagado (kamra_mcp_disabled).",
    ])
  })

  it("keeps asset paths, API paths, identifiers and the router mount out of scope", () => {
    const { findings } = scanTree(fixture({
      ...shell,
      "src/lib/routing.ts": 'export const ROUTER_BASENAME = "/kamra"',
      "src/screens/Fixture.tsx": [
        'export const icon = "/assets/kamra/frontend/icon.svg"',
        'export const who = () => fetch("/api/method/kamra.api.whoami")',
        'export const key = "kamra.table.columns"',
        'export const flag = "kamra_mcp_disabled"',
      ].join("\n"),
    }))
    expect(findings).toEqual([])
  })
})
