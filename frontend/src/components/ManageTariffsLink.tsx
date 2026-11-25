import { ArrowRight } from "lucide-react"
import { Link } from "react-router-dom"
import { useT } from "../lib/i18n"

export function ManageTariffsLink({ roomType }: { roomType?: string }) {
  const { t } = useT()
  const search = roomType ? `?${new URLSearchParams({ q: roomType })}` : ""
  return <Link to={{ pathname: "/tariffs", search }}
    className="inline-flex items-center gap-1.5 rounded-lg border border-accent-line bg-surface px-3 py-1.5 text-sm font-medium text-accent-ink hover:bg-accent-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
    {t("Manage tariffs")} <ArrowRight aria-hidden className="size-4 rtl:rotate-180" />
  </Link>
}
