import { lazy, Suspense, useCallback, useEffect, useState } from "react"
import { Link, useNavigate } from "react-router-dom"
import { useRealtime } from "../lib/realtime"
import { TariffNight } from "../components/ContractTariff"
import {
  BedDouble,
  CalendarDays,
  CreditCard,
  Fingerprint,
  FileText,
  LogIn,
  LogOut,
  ShieldCheck,
  Star,
  TriangleAlert,
  User,
  UserX,
} from "lucide-react"

import {
  acceptBookingRequest,
  amendStay,
  amendStayPreview,
  call,
  clearChannelReview,
  declineBookingRequest,
  idDocumentImage,
  promoteWaitlist,
  reservationDetail,
  setLateArrival,
  verifyPrecheckin,
  type RepricePreview,
  type ReservationDetail as Detail,
} from "../lib/api"
import { IdDocumentField } from "../components/IdDocumentField"
import { serverError, updateResource, type Row } from "../lib/resource"
import { Badge } from "../components/ui/badge"
import { Button } from "../components/ui/button"
import CancelPanel from "../components/CancelPanel"
import NoShowDialog from "../components/NoShowDialog"
import ReinstatePanel from "../components/ReinstatePanel"
import ReverseCheckInPanel from "../components/ReverseCheckInPanel"
import LinkedRecords from "../components/LinkedRecords"
import DepositPanel from "../components/DepositPanel"
import SecurityDepositPanel from "../components/SecurityDepositPanel"
import RoomChangeDialog from "../components/RoomChangeDialog"
import CheckOutDialog from "../components/CheckOutDialog"
import CheckInDialog from "../components/CheckInDialog"
import AgreedRatePanel from "../components/AgreedRatePanel"
import ChangesLog from "../components/ChangesLog"
import SupervisorSignOff, { SignOffReasons } from "../components/SupervisorSignOff"
import { supervisorArgs } from "../lib/reservationDiscount"
import { useAuth } from "../lib/auth"
import StayInstructions from "../components/StayInstructions"
import { fmtMoney, useLocale } from "../lib/money"
import { HOTKEY_EVENTS } from "../lib/hotkeys"
import { t as translate, useUILanguage } from "../lib/i18n"

const FixedChargesPanel = lazy(() => import("../components/FixedChargesPanel"))

const inr = (n: number) => fmtMoney(Number(n || 0))

/** One amount, or the lowest to the highest when the nights differ. */
const rangeOf = (values: number[]) =>
  new Set(values).size === 1 ? inr(values[0]) : `${inr(Math.min(...values))}-${inr(Math.max(...values))}`

/** Whole nights from `a` to `b` (YYYY-MM-DD). */
const nightsBetween = (a: string, b: string) =>
  Math.round((new Date(b).getTime() - new Date(a).getTime()) / 86_400_000)

const STATUS_TONE: Record<string, string> = {
  Waitlist: "bg-amber-100 text-amber-800",
  Confirmed: "bg-sky-100 text-sky-800",
  "Checked In": "bg-emerald-100 text-emerald-800",
  "Checked Out": "bg-zinc-200 text-zinc-700",
  Cancelled: "bg-rose-100 text-rose-800",
  "No Show": "bg-amber-100 text-amber-800",
}

function Card(props: {
  icon: React.ReactNode
  title: string
  action?: React.ReactNode
  children: React.ReactNode
}) {
  useUILanguage()
  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-4">
      <div className="mb-3 flex items-center justify-between">
        <div className="flex items-center gap-2 text-sm font-semibold text-zinc-700">
          <span className="text-zinc-400">{props.icon}</span>
          {translate(props.title)}
        </div>
        {props.action}
      </div>
      {props.children}
    </div>
  )
}

function Field(props: { label: string; value: React.ReactNode }) {
  useUILanguage()
  // min-w-0 and the wrap let a long value (a guest's email) break inside its
  // own grid column instead of running into the next one
  return (
    <div className="min-w-0">
      <dt className="text-xs text-zinc-400">{translate(props.label)}</dt>
      <dd className="mt-0.5 break-words text-sm text-zinc-800 [overflow-wrap:anywhere]">{props.value || "-"}</dd>
    </div>
  )
}

/* The guest's ID for this stay: the photo, and whether a human has checked it
   against the person. The image is fetched through a role-gated endpoint, not
   linked by its /private/files/ URL - Frappe authorises that URL through the
   Reservation's doctype permissions, which this site's Custom DocPerm rows
   deny to Front Desk. Linking it would show the GM a photo and the desk a
   broken image. */
function IdentityCard({ d, reload }: { d: Detail; reload: () => void }) {
  useUILanguage()
  const [img, setImg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    setImg(null)
    if (!d.id_document) return
    idDocumentImage(d.name).then((r) => setImg(r.data)).catch(() => setImg(null))
  }, [d.id_document, d.name])

  const verify = async () => {
    setBusy(true)
    setErr(null)
    try {
      await verifyPrecheckin(d.name)
      reload()
    } catch (e) {
      setErr(e instanceof Error ? translate(e.message) : String(e))
    } finally {
      setBusy(false)
    }
  }

  const open = d.status === "Confirmed" || d.status === "Checked In"

  return (
    <Card
      icon={<Fingerprint className="size-4" />}
      title={translate("Identity")}
      action={
        d.warnings.id_unverified ? (
          <Button variant="outline" className="min-h-11 sm:min-h-9" disabled={busy} onClick={verify}>
            <ShieldCheck className="size-4" />{translate(" Verify identity")}</Button>
        ) : d.precheckin_verified_by ? (
          <span className="inline-flex items-center gap-1 text-xs font-medium text-emerald-700">
            <ShieldCheck className="size-3.5" />{translate("Verified by ")}{d.precheckin_verified_by.split("@")[0]}
          </span>
        ) : undefined
      }
    >
      <dl className="mb-3 grid grid-cols-2 gap-3">
        <Field label={translate("Pre-check-in")} value={d.precheckin_status} />
        <Field label={translate("ID captured by")} value={d.id_document_source} />
      </dl>

      {img ? (
        <img src={img} alt={translate("Guest ID document")}
          className="max-h-56 w-auto rounded-lg border border-zinc-200" />
      ) : d.id_document ? (
        <p className="text-sm text-zinc-400">{translate("Loading the document…")}</p>
      ) : d.id_document_discarded ? (
        <p className="rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2 text-xs text-zinc-500">{translate("ID document discarded at checkout, per this property's retention policy.")}</p>
      ) : open ? (
        <div className="space-y-2">
          {/* A warning, never a gate. The guest is standing here with a
              physical card; check-in works with or without this. */}
          <div className="flex items-start gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-xs font-medium text-amber-800">
            <TriangleAlert className="mt-px size-3.5 shrink-0" />{translate("No ID document on file - capture it at the counter.")}</div>
          <IdDocumentField
            method="kamra.api.upload_id_document"
            params={{ reservation: d.name }}
            uploaded={false}
            onUploaded={reload}
            label={translate("Photograph the guest's ID")}
          />
        </div>
      ) : null}

      {err && (
        <p className="mt-2 rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-xs text-rose-700">
          {translate(err)}
        </p>
      )}
    </Card>
  )
}

function todayLocal() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, "0")
  const day = String(d.getDate()).padStart(2, "0")
  return `${y}-${m}-${day}`
}

export default function ReservationDetail({
  row,
  reload,
  onClose,
}: {
  row: Row
  reload: () => void
  onClose: () => void
}) {
  useUILanguage()
  useLocale()
  const name = String(row.name)
  const [d, setD] = useState<Detail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reinstated, setReinstated] = useState<string[]>([])
  const [reverseKeys, setReverseKeys] = useState<string | null>(null)
  const [changingRoom, setChangingRoom] = useState(false)
  const [declineReason, setDeclineReason] = useState("")
  // the roles set_agreed_rate admits (Hotel Admin and System Manager as admins)
  const { roles } = useAuth()
  const canAgreeRate = ["Front Desk", "Revenue Manager", "Hotel Admin", "System Manager"]
    .some((r) => roles.includes(r))
  // who may fix a rate over a plan that takes no discount: the server's
  // answer (actions.can_set_rate, kamra.api._can_set_rate), not a role list
  const canOverrideFixed = !!d?.actions.can_set_rate
  // booking_quotation / email_booking_quotation: Front Desk and above
  const canQuote = ["Front Desk", "Revenue Manager", "Hotel Admin", "System Manager"]
    .some((r) => roles.includes(r))
  const canClearChannelReview = ["Front Desk", "Revenue Manager", "Hotel Admin", "System Manager"]
    .some((r) => roles.includes(r))

  const refresh = useCallback(() => {
    reservationDetail(name)
      .then(setD)
      .catch((e) => setError(serverError(e)))
  }, [name])

  useEffect(() => refresh(), [refresh])

  // A payment through the gateway lands server-side (webhook → folio save →
  // "kamra_changed"); re-fetch this booking when the property changes.
  useRealtime(refresh)

  // While a payment link is out and money is still due, also poll gently
  // (the socket may be down, e.g. behind the dev proxy) and re-check when
  // the tab regains focus - the guest often pays while staff wait.
  const linkPending =
    !!d?.deposit?.link_url && (d?.deposit?.due ?? 0) > 0.009
  useEffect(() => {
    if (!linkPending) return
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") refresh()
    }, 8000)
    const onVis = () => {
      if (document.visibilityState === "visible") refresh()
    }
    document.addEventListener("visibilitychange", onVis)
    return () => {
      clearInterval(timer)
      document.removeEventListener("visibilitychange", onVis)
    }
  }, [linkPending, refresh])

  // stay-date amend - re-seeded only when the stored dates change, so a
  // background refresh never wipes what staff are typing
  const [ci, setCi] = useState("")
  const [co, setCo] = useState("")
  const savedCi = d?.check_in_date
  const savedCo = d?.check_out_date
  useEffect(() => {
    if (savedCi) setCi(savedCi)
    if (savedCo) setCo(savedCo)
  }, [savedCi, savedCo])
  // New dates re-price the stay: the server's preview is shown in the page
  // and the save sends confirm_price from it. A window.confirm() gate here
  // dropped the save silently whenever the browser suppressed the dialog.
  const [datePreview, setDatePreview] = useState<RepricePreview | null>(null)
  // a discounted stay's new dates may need a manager's sign-off, given here
  const [dateSupervisor, setDateSupervisor] = useState("")
  const [datePin, setDatePin] = useState("")
  const dateGov = datePreview?.discount_governance ?? null
  const dateSignOff = supervisorArgs(dateGov, dateSupervisor, datePin)
  const dateSigned = !dateGov?.needs_supervisor || !!dateSignOff.supervisor_user
  const datesDiffer = !!savedCi && (ci !== savedCi || co !== savedCo)
  const datesValid = !!ci && !!co && ci < co
  // The stay's rate plan (OPERA Stay Details' Rate Code), changed through
  // Amend stay: from the current business date for the nights still to
  // come, the posted ones keeping their rate. A Complimentary or House Use
  // plan takes a second manager's sign-off (comp_governance).
  const savedPlan = d?.rate_plan ?? ""
  const [plan, setPlan] = useState("")
  useEffect(() => {
    setPlan(savedPlan)
  }, [savedPlan])
  const planDiffers = !!d?.rate_plans?.length && !!plan && plan !== savedPlan
  const [compSupervisor, setCompSupervisor] = useState("")
  const [compPin, setCompPin] = useState("")
  const compGov = planDiffers ? datePreview?.comp_governance ?? null : null
  // one manager whose roles cover both prompts signs both (OPERA's one authorizer)
  const discountCoversComp = !!dateGov?.needs_supervisor && !!dateSupervisor &&
    !!compGov?.supervisors.some((s) => s.user === dateSupervisor)
  const compAsks = !!compGov?.needs_supervisor && !discountCoversComp
  const compSigned = !compAsks || (!!compSupervisor && !!compPin.trim())
  const compSignOff = compAsks && compSupervisor && compPin.trim()
    ? { comp_supervisor_user: compSupervisor, comp_supervisor_pin: compPin.trim() } : {}
  useEffect(() => {
    setDatePreview(null)
    setDatePin("")
    setCompPin("")
    if ((!datesDiffer && !planDiffers) || !datesValid) return
    let live = true
    amendStayPreview(name, ci, co, planDiffers ? { rate_plan: plan } : undefined)
      .then((p) => {
        if (!live) return
        setDatePreview(p)
        if (p.supervisors?.length === 1) setDateSupervisor(p.supervisors[0].user)
        if (p.comp_governance?.supervisors.length === 1) setCompSupervisor(p.comp_governance.supervisors[0].user)
      })
      .catch((e) => live && setError(serverError(e)))
    return () => {
      live = false
    }
  }, [name, ci, co, datesDiffer, datesValid, planDiffers, plan])

  // editable special requests
  const [req, setReq] = useState<string>("")
  const [reqDirty, setReqDirty] = useState(false)
  const savedReq = d ? d.special_requests ?? "" : null
  useEffect(() => {
    if (savedReq !== null) {
      setReq(savedReq)
      setReqDirty(false)
    }
  }, [savedReq])
  const navigate = useNavigate()
  const [checkingOut, setCheckingOut] = useState(false)
  const [checkingIn, setCheckingIn] = useState(false)
  const [markingNoShow, setMarkingNoShow] = useState(false)
  const [groupNotice, setGroupNotice] = useState<string | null>(null)

  useEffect(() => {
    const onShortcutCheckIn = (e: Event) => {
      if (!d?.actions.can_check_in || busy) return
      e.preventDefault()
      setCheckingIn(true)
    }
    window.addEventListener(HOTKEY_EVENTS.shortcutCheckIn, onShortcutCheckIn)
    return () => window.removeEventListener(HOTKEY_EVENTS.shortcutCheckIn, onShortcutCheckIn)
  }, [d?.actions.can_check_in, busy])

  function checkInGroup(group: NonNullable<Detail["group"]>) {
    if (!confirm(translate("Check in all {v0} waiting rooms of {v1}? Rooms with no room number get a free room of their type.", {
      v0: String(group.to_check_in), v1: group.group_name,
    }))) return
    act(async () => {
      const out = await call<{ results: { checked_in?: number; skipped?: string; error?: string }[] }>(
        "kamra.api.group_check_in", { group_booking: group.name })
      const done = out.results.filter((r) => r.checked_in).length
      const failed = out.results.filter((r) => r.skipped || r.error)
      setGroupNotice([
        translate("{v0} checked in", { v0: String(done) }),
        failed.length
          ? translate("{v0} skipped: {v1}", {
              v0: String(failed.length),
              v1: [...new Set(failed.map((r) => translate(r.skipped ?? r.error ?? "")))].join("; "),
            })
          : "",
      ].filter(Boolean).join(", "))
    })
  }

  async function act(fn: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await fn()
      refresh()
      reload()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  if (!d)
    return (
      <p className="py-10 text-center text-sm text-zinc-400">
        {translate(error) ?? translate("Loading…")}
      </p>
    )

  const datesChanged = ci !== d.check_in_date || co !== d.check_out_date || planDiffers
  const money = d.money
  // the desk's discount at a glance (OPERA Stay Details): what it takes off
  // and its reason, without opening the editor
  const disc = d.discount
  const offText = (type: "Percent" | "Amount", value: number, reason?: string | null) =>
    [type === "Percent"
      ? translate("{pct}% off", { pct: value })
      : translate("{amount} off a night", { amount: inr(value) }),
    reason].filter(Boolean).join(" · ")
  // a discount given only in Daily Details: each night's own, and its reason
  const ownNights = (d.night_rates ?? []).filter((n) => n.discount_type && Number(n.discount_value) > 0)
  const stayDiscount = disc?.discount_type
    ? offText(disc.discount_type, disc.discount_value, disc.discount_reason)
    : ownNights.length
      ? [...new Set(ownNights.map((n) => offText(n.discount_type!, Number(n.discount_value), n.discount_reason)))]
          .join(", ")
      : null

  return (
    <div className="space-y-4">
            <LinkedRecords doctype="Reservation" name={String(row.name)} />
      {/* status + provenance */}
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={
            "rounded-full px-2.5 py-1 text-xs font-semibold " +
            (STATUS_TONE[d.status] ?? "bg-zinc-100 text-zinc-700")
          }
        >
          {translate(d.status)}
        </span>
        {d.booking_type && d.booking_type !== "Individual" && (
          <Badge>{translate(d.booking_type)}</Badge>
        )}
        {d.source && <Badge>{translate(d.source)}</Badge>}
        {d.channel && <Badge>{translate(d.channel)}</Badge>}
        {d.guest?.vip === 1 && (
          <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-800">
            <Star className="size-3" /> VIP
          </span>
        )}
        {d.guest?.blacklisted === 1 && <Badge tone="rose">{translate("Blacklisted")}</Badge>}
        {d.channel_review_needed === 1 && (
          <span className="inline-flex items-center gap-1 rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-semibold text-amber-800">
            <TriangleAlert className="size-3" />
            {translate("Channel review needed")}
          </span>
        )}
      </div>

      {d.channel_review_needed === 1 && (() => {
        const reviewReason =
          d.channel_review_reason ??
          d.channel_reviews?.[0]?.reason ??
          (d.channel_reviews?.some(
            (r) => r.action_type === "channel.modify_needs_review" || r.action_type === "channel.cancel_needs_review",
          )
            ? "update_not_applied"
            : d.channel_reviews?.some((r) => r.action_type === "channel.restriction_mismatch")
              ? "restriction_mismatch"
              : d.channel_reviews?.some((r) => r.action_type === "channel.modify_discount_removed")
                ? "discount_removed"
                : d.channel_reviews?.some((r) => r.action_type === "channel.modify_discount_review")
                  ? "discount_review"
                  : "update_not_applied")
        const reviewTitle =
          reviewReason === "restriction_mismatch"
            ? translate("OTA booking honored despite active stay restrictions — review required")
            : reviewReason === "discount_removed"
              ? translate("Channel update applied — desk discount removed")
              : reviewReason === "discount_review"
                ? translate("Channel update applied — discount review required")
                : translate("Channel update not applied — review required")
        const reviewBody =
          reviewReason === "restriction_mismatch"
            ? translate(
                "The channel booking was accepted to avoid a walk even though active stay restrictions were in place. Review the restriction details below and adjust the stay or channel availability if needed.",
              )
            : reviewReason === "discount_removed"
              ? translate(
                  "The channel update was applied and removed the desk discount (new total: {total}). Review what was removed below and re-apply a discount if needed.",
                  { total: inr(money.total) },
                )
              : reviewReason === "discount_review"
                ? translate(
                    "The channel update was applied (new total: {total}). Approved discounted nights were kept; added nights were priced without the unapproved discount. Review below and adjust if needed.",
                    { total: inr(money.total) },
                  )
                : translate(
                    "The stay's dates, room, and charges were left unchanged. Review what the channel sent below and update the stay manually if needed.",
                  )
        return (
          <div className="rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="flex items-center gap-1.5 font-semibold">
                <TriangleAlert className="size-4 shrink-0 text-amber-700" />
                {reviewTitle}
              </div>
              {canClearChannelReview && (
                <Button
                  variant="outline"
                  className="min-h-11 sm:min-h-9"
                  disabled={busy}
                  onClick={() => act(() => clearChannelReview(name))}
                >
                  {translate("Mark as reviewed")}
                </Button>
              )}
            </div>
            <p className="mt-1 text-xs text-amber-800">{reviewBody}</p>
            {(d.channel_reviews ?? []).length > 0 && (
              <ul className="mt-2 space-y-1 text-xs text-amber-900">
                {(d.channel_reviews ?? []).map((item) => (
                  <li key={item.name} className="rounded-md bg-white/70 px-2.5 py-1.5">
                    <span>{item.rationale}</span>
                    {item.executed_at && (
                      <span className="ms-2 text-amber-700/80">{item.executed_at.slice(0, 16)}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )
      })()}

      {error && (
        <div className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {translate(error)}
        </div>
      )}

      {/* money - the question everyone asks first */}
      <Card
        icon={<CreditCard className="size-4" />}
        title={translate("Billing")}
        action={
          d.folio_name ? (
            <Link
              to={`/billing/${encodeURIComponent(d.folio_name)}`}
              onClick={onClose}
              className="inline-flex min-h-11 items-center text-xs font-medium text-brand-700 hover:underline sm:min-h-9"
            >{translate("Open folio →")}</Link>
          ) : undefined
        }
      >
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3 sm:gap-3">
          <div className="flex min-w-0 items-baseline justify-between gap-2 sm:block">
            <div className="text-xs text-zinc-400">{translate("Total")}</div>
            <div className="break-words text-base font-semibold tabular-nums text-zinc-900 sm:text-lg">
              {inr(money.total)}
            </div>
          </div>
          <div className="flex min-w-0 items-baseline justify-between gap-2 sm:block">
            <div className="text-xs text-zinc-400">{translate("Paid")}</div>
            <div className="break-words text-base font-semibold tabular-nums text-emerald-700 sm:text-lg">
              {inr(money.paid)}
            </div>
          </div>
          <div className="flex min-w-0 items-baseline justify-between gap-2 sm:block">
            <div className="text-xs text-zinc-400">{translate("Balance due")}</div>
            <div
              className={
                "break-words text-base font-semibold tabular-nums sm:text-lg " +
                (money.due > 0 ? "text-rose-600" : "text-zinc-400")
              }
            >
              {inr(money.due)}
            </div>
          </div>
        </div>
        {Number(money.refund_due ?? 0) > 0 && (
          <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800">{translate("Refund due: ")}<span className="font-semibold">{inr(money.refund_due ?? 0)}</span>{" "}{translate("- the advance taken at booking, with no folio or fee against it.")}</div>
        )}
        <p className="mt-2 text-xs text-zinc-400">
          {money.source === "folio"
            ? translate("Live from the guest folio.")
            : money.source === "none"
              ? translate("The booking ended without a stay - nothing is owed.")
              : money.has_folio
                ? translate("Booking total; deposits sit on the guest folio and count at check-in.")
                : money.paid > 0
                  ? translate("Advance taken at booking; the rest is due at check-in.")
                  : translate("No payment yet - take a deposit below to secure the booking.")}
        </p>
      </Card>

      {/* Pack 2 slot: <ReservationAlertsBanner /> / <ReservationTracesCard /> / <FixedChargesCard /> */}

      {d.deposit && (d.deposit.can_take || d.deposit.paid > 0) && (
        <DepositPanel
          reservation={d.name}
          deposit={d.deposit}
          guestPhone={d.guest?.phone}
          onChanged={() => refresh()}
        />
      )}

      {d.security_deposit && (
        <SecurityDepositPanel
          reservation={d.name}
          deposit={d.security_deposit}
          canStart={["Confirmed", "Checked In"].includes(d.status)}
          onChanged={() => {
            refresh()
            reload()
          }}
        />
      )}

      <div className="grid gap-4 sm:grid-cols-2">
        {/* stay + dates (editable) */}
        <Card icon={<CalendarDays className="size-4" />} title={translate("Stay")}>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <label className="block">
              <span className="text-xs text-zinc-400">{translate("Check-in")}</span>
              <input
                type="date"
                value={ci}
                min={
                  // keep existing past check-in visible for in-house stays,
                  // but do not allow amending into an earlier past date
                  d.check_in_date && d.check_in_date < todayLocal()
                    ? d.check_in_date
                    : todayLocal()
                }
                disabled={!d.actions.can_amend}
                onChange={(e) => setCi(e.target.value)}
                className="mt-0.5 min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm disabled:bg-zinc-50 disabled:text-zinc-400 sm:min-h-9"
              />
            </label>
            <label className="block">
              <span className="text-xs text-zinc-400">{translate("Check-out")}</span>
              <input
                type="date"
                value={co}
                disabled={!d.actions.can_amend}
                onChange={(e) => setCo(e.target.value)}
                className="mt-0.5 min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm disabled:bg-zinc-50 disabled:text-zinc-400 sm:min-h-9"
              />
            </label>
          </div>
          {d.actions.can_amend && (d.rate_plans?.length ?? 0) > 0 && (
            <label className="mt-3 block">
              <span className="text-xs text-zinc-400">{translate("Rate plan")}</span>
              <select
                aria-label={translate("Rate plan")}
                value={plan}
                disabled={busy}
                onChange={(e) => setPlan(e.target.value)}
                className="mt-0.5 min-h-11 w-full rounded-lg border border-zinc-300 bg-white px-2 py-1.5 text-sm disabled:bg-zinc-50 disabled:text-zinc-400 sm:min-h-9"
              >
                {!savedPlan && <option value="">—</option>}
                {(d.rate_plans ?? []).map((p) => (
                  <option key={p.name} value={p.name}>{p.rate_plan_name || p.code || p.name}</option>
                ))}
              </select>
            </label>
          )}
          {datesChanged && d.actions.can_amend && (
            <div className="mt-3 space-y-2">
              {!datesValid ? (
                <p className="text-xs text-rose-600">{translate("Check-out must be after check-in.")}</p>
              ) : !datePreview ? (
                !error && <p className="text-xs text-zinc-400">{translate("Checking price")}</p>
              ) : datePreview.difference === 0 ? (
                <p className="text-sm text-zinc-600">
                  {translate("{n} nights. No change to the price.", { n: nightsBetween(ci, co) })}
                </p>
              ) : (
                <p className={`text-sm ${datePreview.difference > 0 ? "text-amber-700" : "text-emerald-700"}`}>
                  {datePreview.difference > 0
                    ? translate("{n} nights. Stay total {from} to {to} (+{amt}).", {
                        n: nightsBetween(ci, co),
                        from: inr(datePreview.current_amount),
                        to: inr(datePreview.new_amount),
                        amt: inr(datePreview.difference),
                      })
                    : translate("{n} nights. Stay total {from} to {to} (-{amt}).", {
                        n: nightsBetween(ci, co),
                        from: inr(datePreview.current_amount),
                        to: inr(datePreview.new_amount),
                        amt: inr(Math.abs(datePreview.difference)),
                      })}
                </p>
              )}
              {planDiffers && (
                <p className="text-xs text-zinc-500">
                  {translate("Applies from the current business date for remaining nights; posted nights keep their rate.")}
                </p>
              )}
              {dateGov?.needs_supervisor && (
                <SupervisorSignOff reason={<SignOffReasons gov={dateGov} />}
                  supervisors={datePreview?.supervisors ?? dateGov.supervisors}
                  supervisor={dateSupervisor} pin={datePin} onSupervisor={setDateSupervisor} onPin={setDatePin} />
              )}
              {compAsks && compGov && (
                <SupervisorSignOff
                  reason={translate("A {category} rate plan needs a manager's sign-off.",
                    { category: translate(compGov.rate_category) })}
                  supervisors={compGov.supervisors} supervisor={compSupervisor} pin={compPin}
                  onSupervisor={setCompSupervisor} onPin={setCompPin} />
              )}
              <Button
                className="min-h-11 sm:min-h-9"
                disabled={busy || !datesValid || !datePreview || !dateSigned || !compSigned}
                onClick={() =>
                  datePreview &&
                  act(() => amendStay(name, ci, co, datePreview.needs_confirmation ? 1 : 0, {
                    ...dateSignOff,
                    ...compSignOff,
                    ...(planDiffers ? { rate_plan: plan } : {}),
                  }))
                }
              >
                {busy
                  ? translate("Updating…")
                  : planDiffers
                    ? datePreview?.needs_confirmation
                      ? translate("Confirm rate plan and price")
                      : translate("Confirm rate plan")
                    : datePreview?.needs_confirmation
                      ? translate("Confirm new dates and price")
                      : translate("Confirm new dates")}
              </Button>
            </div>
          )}
          {d.status === "Confirmed" && (
            <label className="mt-3 flex min-h-11 items-start gap-2 text-sm text-zinc-700">
              <span className="inline-flex min-h-11 min-w-11 shrink-0 items-center justify-center sm:min-h-6 sm:min-w-6">
                <input
                  type="checkbox"
                  checked={Boolean(d.late_arrival)}
                  disabled={busy}
                  onChange={(e) => act(() => setLateArrival(name, e.target.checked))}
                />
              </span>
              <span className="pt-2 sm:pt-0">{translate("Late arrival")}<span className="block text-xs text-zinc-400">{translate("The night audit holds this arrival as Due In instead of marking a no-show; the next audit decides.")}</span>
              </span>
            </label>
          )}
          <dl className="mt-3 grid grid-cols-2 gap-3">
            <Field label={translate("Nights")} value={d.nights} />
            <Field
              label={translate("Guests")}
              value={`${d.adults} adult${d.adults === 1 ? "" : "s"}${
                d.children ? ` · ${d.children} child` : ""
              }`}
            />
            {d.eta && <Field label={translate("ETA")} value={d.eta} />}
            {stayDiscount && <Field label={translate("Discount")} value={stayDiscount} />}
            {stayDiscount && d.night_rates && d.night_rates.length > 0 && (
              <Field label={translate("Base rate / night")} value={rangeOf(d.night_rates.map(
                (n) => n.entry_base_rate ?? n.base_rate ?? n.rate))} />
            )}
            {d.night_rates && d.night_rates.length > 0 && (
              <Field
                label={translate("Confirmed rate / night")}
                value={
                  <>
                    {rangeOf(d.night_rates.map((n) => n.entry_rate ?? n.rate))}
                    {d.discount?.fixed_rate ? <Badge className="ms-2">{translate("Fixed rate")}</Badge> : null}
                  </>
                }
              />
            )}
            {d.precheckin_status && d.precheckin_status !== "Not Started" && (
              <Field label={translate("Pre-check-in")} value={d.precheckin_status} />
            )}
          </dl>
          {d.actions.can_amend && (d.night_rates?.length ?? 0) > 0 && canAgreeRate && (
            <AgreedRatePanel reservation={name} nightRates={d.night_rates ?? []} current={d.discount}
              canOverrideFixed={canOverrideFixed} reasonCategories={d.reason_categories}
              onSaved={() => { refresh(); reload() }} />
          )}
          <ChangesLog reservation={name} money={inr} />
          {d.booked_check_out_date && (
            <p className="mt-3 rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800">
              {translate("Left early: booked to {date}.", { date: d.booked_check_out_date })}
              {d.early_departure_reason ? ` ${d.early_departure_reason}` : ""}
            </p>
          )}
          {(d.occupants ?? []).length > 0 && (
            <div className="mt-3">
              <span className="text-xs text-zinc-400">{translate("In the room")}</span>
              <ul className="mt-1 space-y-0.5 text-sm">
                {(d.occupants ?? []).map((o, i) => (
                  <li key={i} className="flex flex-wrap justify-between gap-x-2">
                    <span className={o.left_at ? "text-zinc-400 line-through" : ""}>{o.full_name}</span>
                    <span className="text-xs tabular-nums text-zinc-500">
                      {o.arrived_at ? translate("Arrived {time}", { time: o.arrived_at.slice(0, 16) }) : ""}
                      {o.left_at ? ` · ${translate("Left {time}", { time: o.left_at.slice(0, 16) })}` : ""}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>

        <IdentityCard d={d} reload={refresh} />

        {/* room */}
        <Card
          icon={<BedDouble className="size-4" />}
          title={translate("Room")}
          action={
            d.status === "Confirmed" || d.status === "Checked In" ? (
              <button
                type="button"
                onClick={() => setChangingRoom(true)}
                className="inline-flex min-h-11 items-center text-xs font-medium text-brand-700 hover:underline sm:min-h-9"
              >
                {d.room ? translate("Change room") : translate("Assign room")}
              </button>
            ) : undefined
          }
        >
          <dl className="grid grid-cols-2 gap-3">
            <Field label={translate("Room type")} value={d.room_type_name ?? d.room_type} />
            <Field label={translate("Room")} value={d.room_number ?? d.room ?? "Unassigned"} />
            {d.meal_plan && <Field label={translate("Meal plan")} value={d.meal_plan_name || d.meal_plan} />}
            {d.rate_plan && <Field label={translate("Rate plan")} value={d.rate_plan} />}
          </dl>
          {d.tariff && <p className="mt-2 text-sm text-zinc-600">
            {translate("Booked contract tariff")}: <TariffNight {...d} />
          </p>}
          {!d.room && d.status === "Confirmed" && (
            <p className="mt-2 text-xs text-amber-600">{translate("No room assigned yet - use Assign room above before check-in.")}</p>
          )}
        </Card>

        {/* guest - connected to the journey */}
        <Card
          icon={<User className="size-4" />}
          title={translate("Guest")}
          action={
            d.guest ? (
              <Link
                to={`/guests/${encodeURIComponent(d.guest.name)}`}
                onClick={onClose}
                className="inline-flex min-h-11 items-center text-xs font-medium text-brand-700 hover:underline sm:min-h-9"
              >{translate("Guest journey →")}</Link>
            ) : undefined
          }
        >
          {d.guest ? (
            <dl className="grid grid-cols-2 gap-3">
              <Field label={translate("Name")} value={d.guest.full_name} />
              <Field label={translate("Phone")} value={d.guest.phone} />
              {d.guest.email && <Field label={translate("Email")} value={d.guest.email} />}
              <Field
                label={translate("History")}
                value={
                  d.guest.stays > 0
                    ? `${d.guest.stays} stay${d.guest.stays === 1 ? "" : "s"}${
                        d.guest.last_stay ? ` · last ${d.guest.last_stay}` : ""
                      }`
                    : "First stay"
                }
              />
            </dl>
          ) : (
            <p className="text-sm text-zinc-400">{translate("No linked guest profile.")}</p>
          )}
        </Card>

        {/* booker / company, when someone booked on the guest's behalf */}
        {(d.booker || d.company || d.travel_agent) && (
          <Card icon={<User className="size-4" />} title={translate("Booked by")}>
            <dl className="grid grid-cols-2 gap-3">
              {d.booker && (
                <>
                  <Field label={translate("Booker")} value={d.booker.name} />
                  <Field label={translate("Phone")} value={d.booker.phone} />
                  {d.booker.relation && (
                    <Field label={translate("Relation")} value={d.booker.relation} />
                  )}
                  {d.booker.contact_preference && (
                    <Field
                      label={translate("Send updates to")}
                      value={d.booker.contact_preference}
                    />
                  )}
                </>
              )}
              {d.company && <Field label={translate("Company")} value={d.company} />}
              {d.travel_agent && (
                <Field label={translate("Travel agent")} value={d.travel_agent} />
              )}
            </dl>
          </Card>
        )}
      </div>

      {/* recurring fixed charges (Reservation Fixed Charge rows) */}
      <Suspense fallback={null}>
        <FixedChargesPanel reservation={name} status={d.status} />
      </Suspense>

      {/* special requests - editable */}
      <Card icon={<FileText className="size-4" />} title={translate("Special requests")}>
        <textarea
          value={req}
          onChange={(e) => {
            setReq(e.target.value)
            setReqDirty(true)
          }}
          placeholder={translate("Late check-in, high floor, allergies…")}
          className="w-full rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"
          rows={2}
        />
        {reqDirty && (
          <Button
            variant="outline"
            className="mt-2 min-h-11 sm:min-h-9"
            disabled={busy}
            onClick={() =>
              act(() =>
                updateResource("Reservation", name, {
                  special_requests: req,
                }),
              )
            }
          >{translate("Save note")}</Button>
        )}
      </Card>

      {/* per-department instructions (Stay Instruction rows) */}
      <StayInstructions
        reservation={name}
        canAdd={!["Cancelled", "Checked Out", "No Show"].includes(d.status)}
      />

      {/* cancellation record, if any */}
      {d.cancellation && (
        <div className="rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-800">
          <p className="font-semibold">{translate("Cancelled - ")}{d.cancellation.number}
          </p>
          <p className="mt-0.5">
            {d.cancellation.reason}
            {d.cancellation.fee > 0
              ? translate(" · fee {v0}", { v0: String(inr(d.cancellation.fee)) })
              : translate(" · no fee")}
            {d.cancellation.cancelled_on
              ? ` · ${d.cancellation.cancelled_on}`
              : ""}
          </p>
          <Link
            to={`/cancelled/${encodeURIComponent(name)}`}
            onClick={onClose}
            className="mt-1 inline-block font-medium text-rose-900 underline"
          >{translate("Cancellation letter")}</Link>
        </div>
      )}

      {d.status === "Requested" && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <p className="font-semibold">{translate("Booking request - waiting for your answer")}</p>
          <p className="mt-0.5 text-xs">{translate("A request holds no room. Accepting checks availability again and takes a room; declining cancels it with no fee and tells the guest.")}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <Button className="min-h-11 sm:min-h-9" disabled={busy} onClick={() => act(() => acceptBookingRequest(name))}>{translate("Accept")}</Button>
            <input
              value={declineReason}
              onChange={(e) => setDeclineReason(e.target.value)}
              placeholder={translate("Reason for declining")}
              className="min-h-11 min-w-0 flex-1 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm focus:outline-2 focus:outline-offset-1 focus:outline-brand-600 sm:min-h-9"
            />
            <Button
              variant="outline"
              className="min-h-11 sm:min-h-9"
              disabled={busy || !declineReason.trim()}
              onClick={() =>
                act(() => declineBookingRequest(name, declineReason.trim()))
              }
            >{translate("Decline")}</Button>
          </div>
        </div>
      )}

      {/* Pack 2 slot: reservation actions extensions */}
      {/* primary actions */}
      <div className="flex flex-wrap items-center gap-2 border-t border-zinc-200 pt-4">
        {d.status === "Waitlist" && (
          <Button
            className="min-h-11 sm:min-h-9"
            disabled={busy}
            onClick={() =>
              act(async () => {
                await promoteWaitlist(name)
              })
            }
          >
            <LogIn className="size-4" />{translate(" Promote to a room")}</Button>
        )}
        {/* Adjacent to the button, never wired into it: can_check_in stays
            `status === "Confirmed" && room`. A stale or missing upload must
            not turn a real guest away at the counter. */}
        {d.actions.can_check_in && d.warnings.id_document_missing && (
          <span className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-800">
            <TriangleAlert className="size-3.5" />{translate(" No ID document on file")}</span>
        )}
        {d.actions.can_check_in && (
          <Button className="min-h-11 sm:min-h-9" disabled={busy} onClick={() => setCheckingIn(true)}>
            <LogIn className="size-4" />{translate(" Check in")}</Button>
        )}
        {d.group && d.group.to_check_in > 1 && (
          <Button variant="outline" className="min-h-11 sm:min-h-9" disabled={busy} onClick={() => checkInGroup(d.group!)}>
            <LogIn className="size-4" />
            {translate("Check in whole group ({v0})", { v0: String(d.group.to_check_in) })}
          </Button>
        )}
        {groupNotice && (
          <span className="rounded-lg border border-emerald-200 bg-emerald-50 px-2.5 py-1.5 text-xs font-medium text-emerald-800">
            {d.group?.group_name}: {groupNotice}
          </span>
        )}
        {d.status === "Checked In" && (d.checkin_backfill?.length ?? 0) > 0 && (
          <Button variant="outline" className="min-h-11 sm:min-h-9" disabled={busy} onClick={() => setCheckingIn(true)}>
            <TriangleAlert className="size-4 text-amber-600" />
            {translate("Finish check-in ({n})", { n: d.checkin_backfill.length })}
          </Button>
        )}
        {d.actions.can_no_show && (
          <Button variant="outline" className="min-h-11 text-rose-700 sm:min-h-9" disabled={busy} onClick={() => setMarkingNoShow(true)}>
            <UserX className="size-4" />{translate("Mark no-show")}</Button>
        )}
        {d.actions.can_check_out && d.security_deposit?.blocks_checkout && (
          <span className="inline-flex items-center gap-1.5 rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-1.5 text-xs font-medium text-amber-800">
            <ShieldCheck className="size-3.5" />{translate(" Security deposit still held - withhold or refund it")}</span>
        )}
        {d.actions.can_check_out && (
          <Button className="min-h-11 sm:min-h-9" disabled={busy} onClick={() => setCheckingOut(true)}>
            <LogOut className="size-4" />{translate(" Check out")}</Button>
        )}
        {d.actions.can_reinstate && (
          <ReinstatePanel reservation={name} onDone={(w) => { setReinstated(w); refresh(); reload() }} />
        )}
        {d.actions.can_reverse_check_in && (
          <ReverseCheckInPanel reservation={name} onDone={(k) => { setReverseKeys(k); refresh(); reload() }} />
        )}
        {reverseKeys && (
          <div className="w-full rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            <p>{translate(reverseKeys)}</p>
          </div>
        )}
        {reinstated.length > 0 && (
          <div className="w-full space-y-1 rounded-xl border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
            {reinstated.map((w) => <p key={w}>{w}</p>)}
          </div>
        )}
        <Link
          to={`/grc/${encodeURIComponent(name)}`}
          onClick={onClose}
          className="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-zinc-200 px-3 py-1.5 text-sm font-medium text-zinc-600 hover:border-brand-600 hover:text-brand-700 sm:min-h-9"
        >
          <FileText className="size-4" />{translate(" Registration card")}</Link>
        {d.actions.can_amend && canQuote && (
          <Link
            to={`/booking-quote/${encodeURIComponent(name)}`}
            onClick={onClose}
            className="inline-flex min-h-11 items-center gap-1.5 rounded-lg border border-zinc-200 px-3 py-1.5 text-sm font-medium text-zinc-600 hover:border-brand-600 hover:text-brand-700 sm:min-h-9"
          >
            <FileText className="size-4" />{translate("Quotation")}</Link>
        )}
      </div>

      {/* cancel flow (self-guards to Confirmed) */}
      {d.actions.can_cancel && (
        <CancelPanel
          row={row}
          reload={() => {
            refresh()
            reload()
          }}
        />
      )}
      {markingNoShow && (
        <NoShowDialog
          reservation={name}
          onClose={() => setMarkingNoShow(false)}
          onDone={() => {
            setMarkingNoShow(false)
            refresh()
            reload()
          }}
        />
      )}
      {checkingIn && (
        <CheckInDialog
          reservation={name}
          onClose={() => {
            setCheckingIn(false)
            refresh()
          }}
          onDone={() => {
            setCheckingIn(false)
            refresh()
            reload()
          }}
        />
      )}
      {checkingOut && (
        <CheckOutDialog
          reservation={name}
          onClose={() => setCheckingOut(false)}
          onDone={() => {
            setCheckingOut(false)
            refresh()
            reload()
          }}
          onOpenReservation={(resName) => {
            setCheckingOut(false)
            onClose()
            navigate("/", { state: { checkout: resName } })
          }}
        />
      )}
      {changingRoom && (
        <RoomChangeDialog
          reservation={d.name}
          currentRoomNumber={d.room_number}
          onClose={() => setChangingRoom(false)}
          onMoved={() => {
            refresh()
            reload()
          }}
        />
      )}
    </div>
  )
}
