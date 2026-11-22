import { ArrowRight } from "lucide-react"
import { Link } from "react-router-dom"
import { useT } from "../lib/i18n"

export function ManageTariffsLink({ roomType }: { roomType?: string }) {
  const { t } = useT()
  const search = roomType ? `?${new URLSearchParams({ q: roomType })}` : ""
  return <Link to={{ pathname: "/tariffs", search }}
    className="inline-flex items-center gap-1.5 rounded-lg border border-brand-200 bg-white px-3 py-1.5 text-sm font-medium text-brand-700 hover:bg-brand-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand-600">
    {t("Manage tariffs")} <ArrowRight aria-hidden className="size-4 rtl:rotate-180" />
  </Link>
}
