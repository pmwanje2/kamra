import { useCallback, useEffect, useState } from "react"
import { X, Plus, Trash2, Search, CircleAlert } from "lucide-react"
import { call, getCurrentProperty } from "../lib/api"
import { Button } from "../components/ui/button"
import { cn } from "../lib/utils"
import { t as translate, useUILanguage } from "../lib/i18n"

/* Recipes: what a dish takes off the shelf when it is fired.
   Deliberately reachable from the Menu screen rather than bolted into the
   generic ResourceScreen, which has no child-table support - teaching it any
   would be a framework-sized detour to serve one editor. */

interface Dish {
  name: string
  item_name: string
  outlet: string
  outlet_name: string
  category: string | null
  available: 0 | 1
  lines: number
}
interface IngredientRow {
  name: string
  ingredient_name: string
  uom: string
  category: string | null
  cost_per_unit: number | null
}
interface RecipeLine {
  ingredient: string
  ingredient_name?: string
  uom?: string
  qty: number | string
  note?: string | null
  qty_on_hand?: number | null
}

export default function RecipeManager({ onClose }: { onClose: () => void }) {
  useUILanguage()
  const [dishes, setDishes] = useState<Dish[]>([])
  const [ings, setIngs] = useState<IngredientRow[]>([])
  const [picked, setPicked] = useState<Dish | null>(null)
  const [lines, setLines] = useState<RecipeLine[]>([])
  const [q, setQ] = useState("")
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)

  const loadDishes = useCallback(() => {
    call<Dish[]>("kamra.inventory.recipe_overview", { property: getCurrentProperty() })
      .then(setDishes).catch((e) => setErr(String(e)))
  }, [])

  useEffect(() => {
    loadDishes()
    call<IngredientRow[]>("kamra.inventory.ingredients", { property: getCurrentProperty() })
      .then(setIngs).catch(() => {})
  }, [loadDishes])

  useEffect(() => {
    const esc = (e: KeyboardEvent) => e.key === "Escape" && onClose()
    window.addEventListener("keydown", esc)
    return () => window.removeEventListener("keydown", esc)
  }, [onClose])

  const open = useCallback((d: Dish) => {
    setPicked(d); setSaved(false); setErr(null); setLines([])
    call<{ recipe: RecipeLine[] }>("kamra.inventory.menu_recipe", { menu_item: d.name })
      .then((r) => setLines(r.recipe)).catch((e) => setErr(String(e)))
  }, [])

  const save = useCallback(async () => {
    if (!picked) return
    setBusy(true); setErr(null)
    try {
      await call("kamra.inventory.save_recipe", {
        menu_item: picked.name,
        rows: lines
          .filter((l) => l.ingredient && Number(l.qty) > 0)
          .map((l) => ({ ingredient: l.ingredient, qty: Number(l.qty), note: l.note ?? null })),
      })
      setSaved(true)
      loadDishes()
    } catch (e) {
      setErr(e instanceof Error ? translate(e.message) : String(e))
    } finally {
      setBusy(false)
    }
  }, [picked, lines, loadDishes])

  const shown = q
    ? dishes.filter((d) => `${d.item_name} ${d.outlet_name} ${d.category ?? ""}`
        .toLowerCase().includes(q.toLowerCase()))
    : dishes

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-scrim/30 p-4">
      <div className="flex h-[85vh] w-full max-w-5xl flex-col rounded-2xl bg-surface shadow-2xl">
        <div className="flex items-center justify-between border-b border-line px-5 py-3">
          <div>
            <h2 className="text-lg font-bold text-ink">{translate("Recipes")}</h2>
            <p className="text-xs text-muted-ink">{translate("What each dish takes off its outlet's stock when the kitchen fires it.")}</p>
          </div>
          <button onClick={onClose} aria-label={translate("Close")}
            className="grid size-9 place-items-center rounded-lg border border-control text-muted-ink hover:bg-subtle">
            <X className="size-4" />
          </button>
        </div>

        <div className="flex min-h-0 flex-1">
          <div className="flex w-72 shrink-0 flex-col border-r border-line">
            <div className="relative p-2">
              <Search className="absolute left-4 top-1/2 size-4 -translate-y-1/2 text-muted-ink" />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder={translate("Find a dish…")}
                className="w-full rounded-lg border border-control py-1.5 pl-8 pr-2 text-sm" />
            </div>
            <ul className="flex-1 overflow-y-auto px-2 pb-2">
              {shown.map((d) => (
                <li key={d.name}>
                  <button onClick={() => open(d)}
                    className={cn("w-full rounded-lg px-2 py-1.5 text-left hover:bg-canvas",
                      picked?.name === d.name && "bg-accent-subtle ring-1 ring-brand-200")}>
                    <div className="flex items-center justify-between gap-2">
                      <span className="truncate text-sm font-semibold text-ink">{d.item_name}</span>
                      <span className={cn("shrink-0 rounded px-1.5 py-0.5 text-[10px] font-bold",
                        d.lines ? "bg-success-subtle text-success-ink" : "bg-subtle text-muted-ink")}>
                        {d.lines || "-"}
                      </span>
                    </div>
                    <div className="truncate text-xs text-muted-ink">{d.outlet_name}</div>
                  </button>
                </li>
              ))}
            </ul>
          </div>

          <div className="flex min-w-0 flex-1 flex-col">
            {!picked ? (
              <div className="grid flex-1 place-items-center p-8 text-center text-sm text-muted-ink">{translate("Pick a dish to give it a recipe.")}</div>
            ) : (
              <>
                <div className="border-b border-line-subtle px-5 py-3">
                  <div className="text-base font-bold text-ink">{picked.item_name}</div>
                  <div className="text-xs text-muted-ink">{picked.outlet_name}</div>
                </div>

                <div className="flex-1 overflow-y-auto px-5 py-3">
                  {err && (
                    <div className="mb-3 rounded-lg border border-danger-line-strong bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
                      {translate(err)}
                    </div>
                  )}
                  {lines.length === 0 ? (
                    <div className="rounded-xl border border-dashed border-control p-6 text-center">
                      <CircleAlert className="mx-auto mb-2 size-5 text-muted-ink" />
                      <p className="text-sm font-semibold text-secondary-ink">{translate("No recipe")}</p>
                      <p className="mt-0.5 text-xs text-muted-ink">{translate("This dish never touches inventory. That's a valid choice - most menus only ever cost their big movers.")}</p>
                    </div>
                  ) : (
                    <table className="w-full text-sm">
                      <thead className="text-left text-xs uppercase text-muted-ink">
                        <tr>
                          <th className="pb-1">{translate("Ingredient")}</th>
                          <th className="pb-1">{translate("Qty per dish")}</th>
                          <th className="pb-1">{translate("On hand")}</th>
                          <th className="pb-1"></th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-line-subtle">
                        {lines.map((l, i) => {
                          const meta = ings.find((x) => x.name === l.ingredient)
                          return (
                            <tr key={i}>
                              <td className="py-1.5 pr-2">
                                <select value={l.ingredient}
                                  onChange={(e) => setLines(lines.map((x, j) =>
                                    j === i ? { ...x, ingredient: e.target.value } : x))}
                                  className="w-full rounded border border-control px-2 py-1 text-sm">
                                  <option value="">{translate("Pick…")}</option>
                                  {ings.map((x) => (
                                    <option key={x.name} value={x.name}>
                                      {x.ingredient_name} ({x.uom})
                                    </option>
                                  ))}
                                </select>
                              </td>
                              <td className="py-1.5 pr-2 whitespace-nowrap">
                                <input type="number" min="0" step="any" inputMode="decimal"
                                  value={l.qty}
                                  onChange={(e) => setLines(lines.map((x, j) =>
                                    j === i ? { ...x, qty: e.target.value } : x))}
                                  className="w-24 rounded border border-control px-2 py-1 tabular-nums" />
                                <span className="ml-1 text-xs text-muted-ink">
                                  {meta?.uom ?? l.uom ?? ""}
                                </span>
                              </td>
                              <td className="py-1.5 tabular-nums text-muted-ink">
                                {l.qty_on_hand === null || l.qty_on_hand === undefined
                                  ? "-"
                                  : <span className={cn(l.qty_on_hand < 0 && "font-bold text-danger-text")}>
                                      {l.qty_on_hand}
                                    </span>}
                              </td>
                              <td className="py-1.5 text-right">
                                <button aria-label={translate("Remove line")}
                                  onClick={() => setLines(lines.filter((_, j) => j !== i))}
                                  className="grid size-8 place-items-center rounded-lg text-muted-ink hover:bg-danger-subtle hover:text-danger-text">
                                  <Trash2 className="size-4" />
                                </button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  )}

                  <Button variant="outline" className="mt-3"
                    onClick={() => setLines([...lines, { ingredient: "", qty: "" }])}>
                    <Plus className="size-4" />{translate("Add ingredient")}</Button>

                  <p className="mt-4 text-xs text-muted-ink">{translate("Quantities are per one dish, in each ingredient's own unit - there are no conversions. Editing a recipe never rewrites history: past consumption was recorded at the quantity in force when it was fired.")}</p>
                </div>

                <div className="flex items-center gap-3 border-t border-line p-4">
                  {saved && <span className="text-sm font-medium text-success-text">{translate("Saved")}</span>}
                  <Button className="ml-auto h-11 px-6" disabled={busy} onClick={save}>{translate("Save recipe")}</Button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
