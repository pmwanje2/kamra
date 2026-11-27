import { useEffect, useState } from "react"
import PrivacyNotice, { setPrivacyTerms } from "../components/PrivacyNotice"
import { CalendarDays, Check, Clock } from "lucide-react"
import { useParams } from "react-router-dom"
import { call } from "../lib/api"
import { Button } from "../components/ui/button"
import { SignaturePad } from "../components/SignaturePad"
import { IdDocumentField } from "../components/IdDocumentField"
import { GuestLaundryCard } from "./laundry/GuestLaundryCard"
import { adoptUiLocale } from "../lib/money"
import { useT, t as translate } from "../lib/i18n"
import { adoptGuestLanguage } from "../lib/dir"
import { LanguageSwitcher } from "../components/LanguageSwitcher"


/** Downscale a picked/captured photo so the upload stays small (max edge
 * 1600px, JPEG) - phone camera originals are 5-12 MB otherwise. */
function fileToDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    const url = URL.createObjectURL(file)
    img.onload = () => {
      const max = 1600
      const scale = Math.min(1, max / Math.max(img.width, img.height))
      const canvas = document.createElement("canvas")
      canvas.width = Math.round(img.width * scale)
      canvas.height = Math.round(img.height * scale)
      canvas.getContext("2d")!.drawImage(img, 0, 0, canvas.width, canvas.height)
      URL.revokeObjectURL(url)
      resolve(canvas.toDataURL("image/jpeg", 0.85))
    }
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("unreadable image")) }
    img.src = url
  })
}

function DocCapture(props: {
  title: string
  note: string
  value: string
  onChange: (v: string) => void
  onFile: (f: File) => Promise<string>
  hasExisting?: boolean
}) {
  const { title, note, value, onChange, onFile, hasExisting } = props
  const { t } = useT()
  return (
    <div className="rounded-xl border border-line p-3">
      <span className="block text-sm font-medium text-secondary-ink">{translate(title)}</span>
      <p className="mb-2 mt-0.5 text-xs text-muted-ink">{note}</p>
      {hasExisting && !value && (
        <p className="mb-2 rounded-lg bg-success-subtle px-2.5 py-1.5 text-xs text-success-ink">
          ✓ {t("We already have this from your last visit - we'll use it. Add a photo below only to replace it with a newer one.")}
        </p>
      )}
      {value ? (
        <div className="flex items-center gap-3">
          <img src={value} alt={translate(title)} className="h-20 rounded-lg border border-line object-cover" />
          <button type="button" className="text-sm font-medium text-danger-text hover:underline"
            onClick={() => onChange("")}>
            {t("Remove & retake")}
          </button>
        </div>
      ) : (
        <div className="flex flex-wrap gap-2">
          <label className="cursor-pointer rounded-lg bg-accent-fill px-3 py-2 text-sm font-semibold text-on-solid">
            {t("Take photo")}
            <input type="file" accept="image/*" capture="environment" className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0]
                if (f) onChange(await onFile(f))
              }} />
          </label>
          <label className="cursor-pointer rounded-lg border border-control px-3 py-2 text-sm font-semibold text-secondary-ink">
            {t("Upload image")}
            <input type="file" accept="image/*" className="hidden"
              onChange={async (e) => {
                const f = e.target.files?.[0]
                if (f) onChange(await onFile(f))
              }} />
          </label>
        </div>
      )}
    </div>
  )
}

const inputCls =
  "w-full rounded-lg border border-control bg-surface px-3.5 py-2.5 text-base " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

interface Info {
  /** ID documents the property's country accepts (its localization pack). */
  id_types: string[]
  /** a help line per ID type from the pack (which copy is fine) */
  id_type_hints?: Record<string, string>
  default_nationality?: string
  property: {
    property_name: string
    logo_url: string | null
    city: string
    checkin_time: string
    phone: string | null
    house_rules: string | null
    pets_policy: string | null
    children_policy: string | null
    extra_bed_policy: string | null
    id_retention: string
    privacy_contact?: string | null
    guest_retention_months?: number | null
  }
  stay: {
    reservation: string
    room_type: string
    check_in_date: string
    check_out_date: string
    nights: number
    adults: number
    children: number
    status: string
  }
  guest: {
    full_name: string
    phone: string | null
    email: string | null
    id_type: string | null
    has_id_file?: boolean
    has_address_file?: boolean
    nationality: string | null
    has_id_document: boolean
    id_document_on: string
  }
  /** the others staying, as named at booking (name and age only). A locked
   *  row was confirmed by the desk and cannot be changed here. */
  occupants?: { row: string; full_name: string; age: number | null; has_id: boolean; locked: boolean }[]
}

interface Companion {
  row?: string
  locked: boolean
  full_name: string
  age: string
  id_type: string
  id_number: string
  has_id: boolean
}

export default function PublicCheckin() {
  const { token } = useParams()
  const { t } = useT()
  // a guest's phone speaks their language: follow it until they choose
  useEffect(() => adoptGuestLanguage(), [])
  const [info, setInfo] = useState<Info | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState(false)
  const [form, setForm] = useState({
    id_type: "", id_number: "", email: "", nationality: "",
    address_line: "", city: "", billing_address: "", eta: "", special_requests: "",
  })
  const [signature, setSignature] = useState("")
  const [addrImage, setAddrImage] = useState("") // data-URL of the address proof
  const [consent, setConsent] = useState(false)
  const [idUploaded, setIdUploaded] = useState(false)
  // everyone else staying in the room: named at booking, or added here
  const [others, setOthers] = useState<Companion[]>([])
  const setOther = (i: number, patch: Partial<Companion>) =>
    setOthers((os) => os.map((o, j) => (j === i ? { ...o, ...patch } : o)))

  useEffect(() => {
    if (!token) {
      setError(t("This check-in link isn't valid. Please contact the hotel."))
      return
    }
    call<Info>("kamra.public_api.precheckin_info", { token })
      .then((i) => {
        adoptUiLocale((i as unknown as { ui_locale?: { currency_symbol?: string; locale?: string } }).ui_locale)
        setPrivacyTerms((i as unknown as { ui_locale?: { privacy_authority?: string } }).ui_locale)
        setInfo(i)
        setForm((f) => ({
          ...f,
          email: i.guest.email ?? "",
          id_type: i.guest.id_type || i.id_types[0] || "",
          nationality: i.guest.nationality ?? i.default_nationality ?? "",
        }))
        // a boolean, never a URL - the guest can't be shown their own photo
        // back (Frappe refuses a Guest session any private file), so after a
        // reload this is all we can honestly say
        setIdUploaded(i.guest.has_id_document)
        setOthers((i.occupants ?? []).map((o) => ({
          row: o.row, locked: o.locked,
          full_name: o.full_name, age: o.age != null ? String(o.age) : "",
          id_type: i.id_types[0] ?? "", id_number: "", has_id: o.has_id,
        })))
        if (i.stay.status === "Submitted") setDone(true)
      })
      .catch(() => setError(t("This check-in link isn't valid. Please contact the hotel.")))
  }, [token])

  async function submit() {
    setBusy(true)
    setError(null)
    try {
      await call("kamra.public_api.precheckin_submit", {
        token, ...form, signature, consent: consent ? 1 : 0,
        address_image: addrImage || "",
        occupants: JSON.stringify(
          others.filter((o) => o.full_name.trim()).map((o) => ({
            row: o.row, full_name: o.full_name.trim(), age: o.age,
            id_type: o.id_number.trim() ? o.id_type : "", id_number: o.id_number.trim(),
          })),
        ),
      })
      setDone(true)
    } catch {
      setError(t("Couldn't save - please check the details and try again."))
    } finally {
      setBusy(false)
    }
  }

  if (error && !info)
    return (
      <div className="flex min-h-screen items-center justify-center bg-canvas px-4">
        <p className="text-muted-ink">{translate(error)}</p>
      </div>
    )
  if (!info)
    return <p className="py-20 text-center text-muted-ink">{t("Loading…")}</p>

  const { property: p, stay, guest } = info

  return (
    <div className="min-h-screen bg-canvas px-4 py-8">
      <div className="mx-auto max-w-lg">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex size-12 items-center justify-center overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
            {p.logo_url ? (
              <img src={p.logo_url} alt="" className="size-full object-contain p-1" />
            ) : (
              <span className="text-lg font-bold text-accent-ink">
                {p.property_name.split(" ").map((w) => w[0]).slice(0, 2).join("")}
              </span>
            )}
          </div>
          <div className="min-w-0 flex-1">
            <h1 className="text-lg font-semibold">{p.property_name}</h1>
            <p className="text-sm text-muted-ink">{t("Online check-in")} · {p.city}</p>
          </div>
          <LanguageSwitcher />
        </div>

        <div className="mb-5 rounded-xl border border-line bg-surface p-4 text-sm shadow-sm">
          <p className="font-medium">
            {guest.full_name} · {t("{type} room", { type: stay.room_type })}
          </p>
          <p className="mt-1 flex items-center gap-2 text-muted-ink">
            <CalendarDays className="size-4" aria-hidden />
            <span dir="ltr">{stay.check_in_date} → {stay.check_out_date}</span> ·{" "}
            {t("{n} night{s}", { n: stay.nights, s: stay.nights === 1 ? "" : "s" })} ·{" "}
            {t("{n} adult{s}", { n: stay.adults, s: stay.adults === 1 ? "" : "s" })}
            {stay.children ? ` + ${t("{n} child", { n: stay.children })}` : ""}
          </p>
          <p className="mt-1 flex items-center gap-2 text-muted-ink">
            <Clock className="size-4" aria-hidden />
            {t("Rooms ready from {time}", { time: p.checkin_time.slice(0, 5) })}
          </p>
        </div>

        {/* self-hides unless the reservation is actually Checked In - 
            stay.status here is the pre-check-in status, not the stay status */}
        {token && <GuestLaundryCard token={token} />}

        {done ? (
          <div className="rounded-xl border border-success-line bg-success-subtle p-5 text-success-ink">
            <p className="flex items-center gap-2 text-lg font-semibold">
              <Check className="size-5" aria-hidden />
              {t("You're checked in online")}
            </p>
            <p className="mt-1 text-sm">
              {t("Skip the paperwork at the desk - just show your ID on arrival and pick up the key. See you soon!")}
            </p>
          </div>
        ) : (
          <div className="space-y-4 rounded-xl border border-line bg-surface p-5 shadow-sm">
            <p className="text-sm text-muted-ink">
              {t("Save time at the desk - fill your details now, show the ID once on arrival.")}
            </p>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("ID type")}</span>
                <select className={inputCls} value={form.id_type}
                  onChange={(e) => setForm({ ...form, id_type: e.target.value })}>
                  {(info?.id_types ?? []).map((x) => <option key={x} value={x}>{t(x)}</option>)}
                </select>
              </label>
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("ID number")}</span>
                <input className={inputCls} value={form.id_number}
                  onChange={(e) => setForm({ ...form, id_number: e.target.value })} />
              </label>
            </div>

            {/* Optional, and it stays optional: the submit button below never
                mentions this. A guest on a locked-down phone or a bad lobby
                connection must still be able to pre-register - gating here
                would just move the queue back to the desk. */}
            <IdDocumentField
              method="kamra.public_api.precheckin_upload_id"
              params={{ token }}
              uploaded={idUploaded}
              onUploaded={() => setIdUploaded(true)}
              label={t("Add a photo of your ID (optional)")}
              hint={t("It speeds up arrival. Bring the original card either way.")}
            />
            <p className="-mt-1 text-xs text-muted-ink">
              {p.id_retention === "Verify & Discard"
                ? t("Your ID photo is used only to confirm your identity at arrival, and is permanently deleted when you check out. Only hotel staff can see it.")
                : t("Your ID photo is kept with the guest register the hotel is required by law to maintain. Only hotel staff can see it.")}
              {info?.id_type_hints?.[form.id_type] && " " + t(info.id_type_hints[form.id_type])}
            </p>

            <DocCapture
              title={t("Address proof (optional)")}
              note={t("Only if your address proof is a different document from your ID.")}
              value={addrImage}
              onChange={setAddrImage}
              onFile={fileToDataUrl}
              hasExisting={info?.guest?.has_address_file}
            />

            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Email")}</span>
                <input className={inputCls} type="email" value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })} />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Nationality")}</span>
                <input className={inputCls} value={form.nationality}
                  onChange={(e) => setForm({ ...form, nationality: e.target.value })} />
              </label>
            </div>
            <label className="block">
              <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Address")}</span>
              <input className={inputCls} value={form.address_line}
                placeholder={t("Street, area")}
                onChange={(e) => setForm({ ...form, address_line: e.target.value })} />
            </label>
            <div className="grid grid-cols-2 gap-3">
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("City")}</span>
                <input className={inputCls} value={form.city}
                  onChange={(e) => setForm({ ...form, city: e.target.value })} />
              </label>
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Arriving around")}</span>
                <input className={inputCls} type="time" value={form.eta}
                  onChange={(e) => setForm({ ...form, eta: e.target.value })} />
              </label>
            </div>
            <label className="block">
              <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Billing address (optional)")}</span>
              <textarea className={inputCls} rows={2} maxLength={500} value={form.billing_address}
                onChange={(e) => setForm({ ...form, billing_address: e.target.value })} />
            </label>
            <label className="block">
              <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{t("Anything we should know?")}</span>
              <textarea className={inputCls} rows={2} value={form.special_requests}
                onChange={(e) => setForm({ ...form, special_requests: e.target.value })} />
            </label>
            {(p.house_rules || p.pets_policy || p.children_policy || p.extra_bed_policy) && (
              <details className="group rounded-lg border border-line bg-canvas/50 p-3 text-xs">
                <summary className="flex items-center justify-between font-medium text-secondary-ink cursor-pointer select-none [&::-webkit-details-marker]:hidden">
                  <span>{t("View Hotel House Rules & Policies")}</span>
                  <span className="transition group-open:rotate-180">
                    <svg fill="none" height="16" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="2" viewBox="0 0 24 24" width="16" className="size-3.5 text-muted-ink"><polyline points="6 9 12 15 18 9"></polyline></svg>
                  </span>
                </summary>
                <div className="mt-2 space-y-2 border-t border-line/60 pt-2 text-secondary-ink leading-relaxed whitespace-pre-line">
                  {p.house_rules && (
                    <div>
                      <span className="font-semibold text-secondary-ink">{t("House Rules:")} </span>
                      {p.house_rules}
                    </div>
                  )}
                  {p.pets_policy && (
                    <div>
                      <span className="font-semibold text-secondary-ink">{t("Pets Policy:")} </span>
                      {p.pets_policy}
                    </div>
                  )}
                  {p.children_policy && (
                    <div>
                      <span className="font-semibold text-secondary-ink">{t("Children Policy:")} </span>
                      {p.children_policy}
                    </div>
                  )}
                  {p.extra_bed_policy && (
                    <div>
                      <span className="font-semibold text-secondary-ink">{t("Extra Bed Policy:")} </span>
                      {p.extra_bed_policy}
                    </div>
                  )}
                </div>
              </details>
            )}

            <div className="space-y-2 rounded-lg border border-line p-3">
              <span className="block text-sm font-medium text-secondary-ink">{t("People staying with you")}</span>
              <p className="text-xs text-muted-ink">
                {t("Add everyone else in your room. Adults need an ID number; for children, name and age are enough.")}
              </p>
              {others.map((o, i) => (
                <div key={i} className="grid grid-cols-2 gap-2 rounded-lg bg-canvas p-2">
                  <input className={inputCls} placeholder={t("Full name")} aria-label={t("Full name")} readOnly={o.locked}
                    value={o.full_name} onChange={(e) => setOther(i, { full_name: e.target.value })} />
                  <input className={inputCls} placeholder={t("Age")} aria-label={t("Age")} inputMode="numeric" readOnly={o.locked}
                    value={o.age} onChange={(e) => setOther(i, { age: e.target.value.replace(/\D/g, "").slice(0, 3) })} />
                  <select className={inputCls} aria-label={t("ID type")} value={o.id_type}
                    onChange={(e) => setOther(i, { id_type: e.target.value })}>
                    {(info?.id_types ?? []).map((x) => <option key={x} value={x}>{t(x)}</option>)}
                  </select>
                  <input className={inputCls} aria-label={t("ID number")}
                    placeholder={o.has_id ? t("ID already given") : t("ID number")}
                    value={o.id_number} onChange={(e) => setOther(i, { id_number: e.target.value })} />
                  {!o.locked && (
                    <button type="button" className="col-span-2 text-right text-sm text-muted-ink hover:text-danger-text"
                      onClick={() => setOthers((os) => os.filter((_, j) => j !== i))}>{t("Remove")}</button>
                  )}
                </div>
              ))}
              <button type="button" className="text-sm font-medium text-accent-ink hover:underline"
                onClick={() => setOthers((os) => [...os, {
                  locked: false, full_name: "", age: "", id_type: info?.id_types[0] ?? "", id_number: "", has_id: false,
                }])}>
                + {t("Add a person")}
              </button>
            </div>

            <div className="rounded-lg border border-line bg-canvas p-3">
              <span className="mb-1.5 block text-sm font-medium text-secondary-ink">
                {t("Registration card - your signature")}
              </span>
              {/* One instrument, widened - not a second checkbox. The notice
                  has to cover the ID photo before it's collected, but adding
                  another gate to the form whose completion rate is the whole
                  point would cost more than it protects. */}
              <p className="mb-2 text-xs text-muted-ink">
                {t("I confirm the details above are correct, agree to the hotel's registration terms and house rules, and consent to the hotel holding a copy of my ID for this stay.")}
              </p>
              <SignaturePad onChange={setSignature} />
              <label className="mt-2 flex items-start gap-2 text-xs text-secondary-ink">
                <input
                  type="checkbox"
                  className="mt-0.5"
                  checked={consent}
                  onChange={(e) => setConsent(e.target.checked)}
                />
                {t("I accept the registration declaration.")}
              </label>
              <div className="mt-2">
                <PrivacyNotice propertyName={p.property_name} contact={p.privacy_contact}
                  retentionMonths={p.guest_retention_months} idRetention={p.id_retention} collectsId />
              </div>
            </div>
            {error && (
              <div className="rounded-lg border border-danger-line bg-danger-subtle px-3 py-2 text-sm text-danger-ink">
                {translate(error)}
              </div>
            )}
            <Button
              className="w-full justify-center py-2.5 text-base"
              disabled={busy || !form.id_number || !signature || !consent}
              onClick={submit}
            >
              {busy ? t("Saving…") : t("Sign & complete check-in")}
            </Button>
            <p className="text-center text-xs text-muted-ink">
              {t("Your details and signature form the guest register required by law.")}
            </p>
          </div>
        )}
      </div>
    </div>
  )
}
