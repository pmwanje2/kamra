import { ArrowRight } from "lucide-react"
import { Link } from "react-router-dom"
import { useT } from "../lib/i18n"

export function ManageTariffsLink({ roomType, roomTypeName, roomTypeCode }: {
  roomType?: string; roomTypeName?: string; roomTypeCode?: string
}) {
  const { t } = useT()
  const params = new URLSearchParams()
  const query = roomTypeName || roomTypeCode || roomType
  if (query) params.set("q", query)
  if (roomType && query !== roomType) params.set("room_type", roomType)
  if (roomTypeCode) params.set("room_type_code", roomTypeCode)
  const search = query ? `?${params}` : ""
  return <Link to={{ pathname: "/tariffs", search }}
    className="inline-flex items-center gap-1.5 rounded-lg border border-accent-line bg-surface px-3 py-1.5 text-sm font-medium text-accent-ink hover:bg-accent-subtle focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus">
    {t("Manage tariffs")} <ArrowRight aria-hidden className="size-4 rtl:rotate-180" />
  </Link>
}
