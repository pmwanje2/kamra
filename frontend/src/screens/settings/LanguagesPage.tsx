import { useCallback, useEffect, useState } from "react"

import { call } from "../../lib/api"
import { LANGS, applyEnabledLanguages } from "../../lib/dir"
import { useT } from "../../lib/i18n"
import { serverError } from "../../lib/resource"
import { Card, CardContent, CardHeader, CardTitle } from "../../components/ui/card"
import { Switch } from "./ModulePage"

/** Site-wide on/off list. The rows are the language registry, so a language
 * added later shows up here without a change to this screen. */
export function LanguagesPage() {
  const { t } = useT()
  const shipped = LANGS
  const [enabled, setEnabled] = useState<string[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    call<{ enabled: string[] }>("kamra.languages.enabled_ui_languages")
      .then((res) => {
        setEnabled(res.enabled)
        applyEnabledLanguages(res.enabled)
      })
      .catch((e) => setError(serverError(e)))
  }, [])
  useEffect(load, [load])

  async function toggle(code: string) {
    if (code === "en" || !enabled || busy) return
    const next = new Set(enabled)
    if (next.has(code)) next.delete(code)
    else next.add(code)
    next.add("en")
    const disabled = shipped
      .map((lang) => lang.code)
      .filter((item) => item !== "en" && !next.has(item))
    setBusy(true)
    setError(null)
    try {
      const res = await call<{ enabled: string[] }>("kamra.languages.set_disabled_ui_languages", {
        disabled: JSON.stringify(disabled),
      })
      setEnabled(res.enabled)
      applyEnabledLanguages(res.enabled)
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  if (!enabled) {
    return <p className="py-10 text-center text-sm text-muted-ink">{t("Loading…")}</p>
  }

  return (
    <Card>
      <CardHeader>
        <div>
          <CardTitle>{t("Languages offered")}</CardTitle>
          <p className="mt-0.5 text-xs text-muted-ink">
            {t("Sign-in, the staff app and guest pages only offer these. Applies to the whole site. English stays on.")}
          </p>
          <p className="mt-2 flex flex-wrap items-center gap-1.5 text-xs text-muted-ink">
            {t("Changed by")}
            <span className="rounded-full bg-subtle px-2 py-0.5 font-medium text-secondary-ink">{t("Hotel Admin")}</span>
          </p>
        </div>
      </CardHeader>
      <CardContent className="divide-y divide-line-subtle px-5">
        {error && (
          <p className="mb-2 rounded-lg bg-danger-subtle px-3 py-2 text-sm text-danger-ink">{t(error)}</p>
        )}
        {shipped.map((lang) => {
          const on = lang.code === "en" || enabled.includes(lang.code)
          return (
            <div key={lang.code} className="flex items-center gap-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-ink" lang={lang.code}>
                  {lang.nativeLabel}
                  {lang.code !== "en" ? <span className="font-normal text-muted-ink"> ({lang.englishLabel})</span> : null}
                </p>
                {lang.code === "en" && (
                  <p className="text-xs text-muted-ink">{t("English is always available.")}</p>
                )}
                {lang.dir === "rtl" && (
                  <p className="text-xs text-muted-ink">{t("Switches the interface to right-to-left.")}</p>
                )}
              </div>
              <Switch
                on={on}
                disabled={lang.code === "en" || busy}
                label={lang.nativeLabel}
                onToggle={() => toggle(lang.code)}
              />
            </div>
          )
        })}
      </CardContent>
    </Card>
  )
}
