import { Globe } from "lucide-react"
import { setLang } from "../lib/dir"
import { useOfferedLanguages, useT } from "../lib/i18n"

/** Language dropdown for the public guest pages. */
export function LanguageSwitcher(props?: { className?: string }) {
  const { t, lang } = useT()
  const langs = useOfferedLanguages()
  return (
    <label
      className={
        "flex shrink-0 items-center gap-1.5 rounded-lg border border-line bg-surface px-2 py-1 text-xs text-secondary-ink " +
        (props?.className ?? "")
      }
    >
      <Globe className="size-3.5 text-muted-ink" aria-hidden />
      <select
        aria-label={t("Language")}
        value={lang}
        onChange={(e) => setLang(e.target.value)}
        className="bg-transparent pe-1 outline-none"
      >
        {langs.map((l) => (
          <option key={l.code} value={l.code}>
            {l.nativeLabel}
          </option>
        ))}
      </select>
    </label>
  )
}
