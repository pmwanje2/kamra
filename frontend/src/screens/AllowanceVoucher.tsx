import { useEffect, useState } from "react"
import { ArrowLeft, Printer } from "lucide-react"
import { Link, useParams } from "react-router-dom"
import { call } from "../lib/api"
import { serverError } from "../lib/resource"
import { Button } from "../components/ui/button"
import { cur, fmtAmount } from "../lib/money"
import { useT, t as translate } from "../lib/i18n"

/** The printable allowance voucher: the digital form of the paper slip,
 * numbered, with the manager who signed it off with their PIN. Data from
 * kamra.api.allowance_voucher; React escapes every value. */

interface Voucher {
  property: { property_name: string; address: string; phone: string | null }
  serial: string
  posted_on: string
  folio: string
  bill_to_name: string | null
  reservation: string | null
  guest_name: string | null
  room: string | null
  amount: number
  gst_rate: number
  tax: number
  total: number
  reason_code: string | null
  reason: string
  requested_by: string
  requested_by_name: string
  requested_on: string
  channel: string | null
  approved_by: string | null
  approved_by_name: string | null
  approved_by_role: string | null
  /** job title from Staff & access, else the role */
  approved_by_title: string | null
  approved_with_pin: boolean
  approved_on: string
}

const money = (n: number) =>
  `${cur()}${fmtAmount(n || 0)}`

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-4 py-1">
      <span className="text-muted-ink">{label}</span>
      <span className="text-right">{children}</span>
    </div>
  )
}

export default function AllowanceVoucher() {
  const { t } = useT()
  const { name } = useParams()
  const [v, setV] = useState<Voucher | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (name)
      call<Voucher>("kamra.api.allowance_voucher", { allowance_request: name })
        .then(setV)
        .catch((e) => setError(serverError(e)))
  }, [name])

  if (error) return <p className="py-10 text-center text-sm text-danger-text">{error}</p>
  if (!v) return <p className="py-10 text-center text-muted-ink">{t("Loading…")}</p>

  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-4 flex items-center justify-between print:hidden">
        <Link to="/allowances" className="inline-flex items-center gap-1 text-sm text-muted-ink hover:text-ink">
          <ArrowLeft className="size-4" aria-hidden />{t("Allowances")}
        </Link>
        <Button onClick={() => window.print()}>
          <Printer className="size-4" aria-hidden />{t("Print voucher")}
        </Button>
      </div>

      <div className="rounded-xl border border-line bg-surface p-8 text-sm print:border-0">
        <div className="mb-5 flex items-start justify-between border-b border-control pb-4">
          <div>
            <h1 className="text-lg font-bold">{v.property.property_name}</h1>
            <p className="text-xs text-muted-ink">{v.property.address}</p>
            {v.property.phone && <p className="text-xs text-muted-ink">{v.property.phone}</p>}
          </div>
          <div className="text-right">
            <p className="text-sm font-semibold">{t("ALLOWANCE VOUCHER")}</p>
            <p className="font-mono text-lg font-bold text-accent-ink">{v.serial}</p>
            <p className="text-xs text-muted-ink">{v.posted_on.slice(0, 16)}</p>
          </div>
        </div>

        <Row label={t("Guest")}>{v.bill_to_name || v.guest_name || "-"}</Row>
        <Row label={t("Room")}>{v.room || "-"}</Row>
        <Row label={t("Folio")}>{v.folio}</Row>
        {v.reservation && <Row label={t("Reservation")}>{v.reservation}</Row>}

        <div className="my-4 rounded-lg bg-canvas px-4 py-3">
          <Row label={t("Allowance")}>{money(v.amount)}</Row>
          <Row label={t("Tax reversed ({rate}%)", { rate: String(v.gst_rate) })}>{money(v.tax)}</Row>
          <div className="mt-1 border-t border-line pt-1 font-medium">
            <Row label={t("Taken off the bill")}>{money(v.total)}</Row>
          </div>
        </div>

        {v.reason_code && <Row label={t("Reason code")}>{v.reason_code}</Row>}
        <Row label={t("Reason")}>{v.reason}</Row>
        <Row label={t("Requested by")}>
          {v.requested_by_name}{" "}
          <span className="text-muted-ink">({v.requested_on.slice(0, 16)}{v.channel && v.channel !== "Desk" ? `, ${v.channel}` : ""})</span>
        </Row>
        <Row label={t("Approved by")}>
          {v.approved_by ? (
            <>
              {v.approved_by_name}
              {v.approved_by_title && <span className="text-muted-ink">, {translate(v.approved_by_title)}</span>}
              <span className="block text-xs text-muted-ink">
                {v.approved_with_pin ? t("Approved with PIN") : t("Approved")}, {v.approved_on.slice(0, 16)}
              </span>
            </>
          ) : (
            <span className="text-muted-ink">{t("No approval needed at this property")}</span>
          )}
        </Row>

        <div className="mt-12 grid grid-cols-2 gap-10 text-xs text-muted-ink">
          <div className="border-t border-control-strong pt-1">
            {t("Approved by")}{v.approved_by_name ? `: ${v.approved_by_name}` : ""}
          </div>
          <div className="border-t border-control-strong pt-1">{t("Guest or requester")}</div>
        </div>
      </div>
    </div>
  )
}
