import { SettlementPreview, useSettlementQuote } from "./SettlementPreview"
import TenderPicker, { TenderInput, resolveTender, tenderAmount, tenderModes, tenderReady, useTenderRates } from "./TenderPicker"
import { RateOptions } from "./RateOptions"
import { currencyText, formatFxRate, fxLockNote, TariffRoomTotal } from "./ContractTariff"
import { useEffect, useRef, useState } from "react"
import { htmlToText, formatDate, roomLabel } from "../lib/utils"
import { useNavigate } from "react-router-dom"
import { ChevronDown, Loader2, Megaphone, Plus, ShieldAlert, Star, Trash2, X } from "lucide-react"
import {
  call,
  getCurrentProperty,
  isTaxUndetermined,
  TAX_NOT_SET_UP_MSG,
  type BookingOptions,
  type GuestAlert,
  type GuestHit,
  type Quote,
} from "../lib/api"
import { pickDefaultRoomType } from "../lib/bookingDefaults"
import { Button } from "./ui/button"
import { activeMoneyLocale, moneyDigits, moneyStep, roundMoney, useLocale } from "../lib/money"
import { isPhoneComplete } from "../lib/phone"
import { nightsBetween, parseDate, shiftDate } from "../lib/stay"
import { onSettingsChanged, onTariffsChanged } from "../lib/tariffSync"
import { typedNights, type NightPrice } from "../lib/nightPrices"
import {
  discountArgs,
  discountNote,
  discountEligible,
  discountError,
  discountReady,
  emptyDiscount,
  entryBase,
  supervisorArgs,
  type DiscountGovernance,
  type DiscountState,
} from "../lib/reservationDiscount"
import DiscountPanel from "./DiscountPanel"
import SupervisorSignOff from "./SupervisorSignOff"
import PhoneInput from "./PhoneInput"
import { clampGuests, sleeps } from "../lib/occupancy"
import { useT, t as translate, useUILanguage } from "../lib/i18n"
import { petChargeBasis } from "../lib/pets"
import { useCashierAuth } from "../lib/cashierAuth"
import DepositPanel, { type DepositState } from "./DepositPanel"
import CheckInDialog from "./CheckInDialog"
import TillCard, { useTill } from "./TillCard"



interface CompanyCredit {
  applies: boolean
  ok: boolean
  blocked?: boolean
  unverified?: boolean
  policy: string
  company?: string
  reasons: string[]
  can_override?: boolean
}

interface ExtraRoom {
  room_type: string
  adults: number
  children: number
  meal_plan: string
  /** blank → the main guest; named later from the rooming list */
  guest_name: string
  /** rooms of this kind ("Deluxe × 5") */
  count: number
  /** nights the desk priced by hand for this room (its own, not the main room's) */
  night_prices?: NightPrice[]
}

const inputCls =
  "w-full rounded-lg border border-control bg-surface px-3.5 py-2.5 text-base " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"


function Field(props: { required?: boolean; label: string; children: React.ReactNode }) {
  useUILanguage()
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium text-secondary-ink">
        {props.label}{props.required && <span aria-hidden="true"> *</span>}
      </span>
      {props.children}
    </label>
  )
}


interface FreeRoom {
  name: string
  room_number: string
  housekeeping_status?: string
}

interface WalkInResult {
  reservation: string
  room: string
  room_number: string
  folio: string | null
  paid?: number
  amount_after_tax?: number
  /** 0: booked and room assigned, check-in still to finish (defer_check_in) */
  checked_in?: 0 | 1
}

/** The quote's nights for Daily Details and the discount bounds, on the
 * basis the desk sees a rate: gross where the property's rates include tax
 * (the night's net plus the discount, so a lower tax slab never moves it). */
function discountNights(q: Quote) {
  const inclusive = !!q.line_items?.[0]?.is_tax_inclusive
  const places = Math.max(moneyDigits(), 2)
  return q.nightly.map((n) => ({
    date: n.date,
    base_rate: entryBase(n, inclusive, places),
    ...(n.contract_base_rate !== undefined ? { contract_base_rate: n.contract_base_rate } : {}),
    ...(n.contract_currency ? { contract_currency: n.contract_currency } : {}),
  }))
}

function todayLocal() {
  const d = new Date()
  const m = `${d.getMonth() + 1}`.padStart(2, "0")
  const day = `${d.getDate()}`.padStart(2, "0")
  return `${d.getFullYear()}-${m}-${day}`
}


export function BookingDialog(props: {
  initial: {
    room_type?: string
    room?: string
    room_number?: string
    date?: string
    check_in_date?: string
    guest?: string
    guest_name?: string
    phone?: string
    /** nights to book (a quote handed over, say); default one */
    nights?: number
    /** the picked guest's past stays, for their profile card - never nights */
    stays?: number
    adults?: number
    children?: number
    walkIn?: boolean
  }
  onClose: () => void
  onBooked: () => void
}) {
  const { t, tn } = useT()
  const [options, setOptions] = useState<BookingOptions | null>(null)
  const [quoted, setQuoted] = useState<{ rateKey: string; quote: Quote } | null>(null)
  const [quoting, setQuoting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const dialog = useRef<HTMLDivElement>(null)
  const [attempted, setAttempted] = useState(false)
  const [messagesOpen, setMessagesOpen] = useState(false)
  const [submitError, setSubmitError] = useState("")
  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null
    dialog.current?.focus()
    return () => { opener?.focus() }
  }, [])
  const [busy, setBusy] = useState(false)
  const [done, setDone] = useState<{
    ref: string
    room_number?: string | null
    room: string | null
    waitlist?: boolean
    walkIn?: WalkInResult
    /** a single booking: take the deposit right here (#114) */
    deposit?: DepositState
  } | null>(
    null,
  )
  /** a deferred walk-in: the guided check-in is open for it */
  const [guidedCheckIn, setGuidedCheckIn] = useState<string | null>(null)
  const [walkInCheckedIn, setWalkInCheckedIn] = useState(false)

  // the other people staying, by name; their IDs come at check-in
  const [others, setOthers] = useState("")
  async function addOthers(reservation: string | undefined) {
    if (!reservation || !others.trim()) return
    try {
      await call("kamra.api.add_occupant_names", { reservation, names: others })
    } catch (e) {
      setError(t("Booked, but the other guests' names were not saved: {v0}", { v0: shortErr(e) }))
    }
  }
  const [form, setForm] = useState({
    guest_name: props.initial.guest_name ?? "",
    phone: props.initial.phone ?? "",
    room_type: props.initial.room_type ?? "",
    // a walk-in arrives today, whatever date the screen it came from showed
    check_in_date: props.initial.walkIn
      ? todayLocal()
      : props.initial.date ?? props.initial.check_in_date ?? todayLocal(),
    nights: props.initial.nights ?? 1,
    adults: props.initial.adults ?? 2,
    children: props.initial.children ?? 0,
    meal_plan: "",
    // "" = no plan picked: the company's negotiated plan applies, if any
    rate_plan: "",
    voucher_code: "",
    over_capacity_reason: "",
    over_capacity_guests: 0,
    over_capacity_charge: 0,
    pets: 0,
    company: "",
    travel_agent: "",
    booking_type: "",
    market_segment: "",
    booked_by_name: "",
    booked_by_phone: "",
    booker_relation: "",
    contact_preference: "Booker",
  })
  const rateCurKey = `${form.rate_plan}|${form.company}|${form.room_type}`
  const quote = quoted?.rateKey === rateCurKey ? quoted.quote : null
  // Booked via an OTA (keyed from its email): the site, its reference and
  // the price it charged (all rooms) and its commission %, prefilled from
  // the site's default. "" = direct, "other" = a site typed by hand.
  const [via, setVia] = useState({ site: "", other: "", ref: "", total: "", pct: "" })
  const viaName = via.site === "other" ? via.other.trim() : via.site
  const otaTotal = viaName ? Number(via.total) || 0 : 0
  const viaArgs = viaName
    ? {
        booked_via: viaName,
        ota_ref: via.ref.trim() || undefined,
        ota_total: otaTotal || undefined,
        // blank = the site's default
        ota_commission_pct: via.pct.trim() === "" ? undefined : Number(via.pct) || 0,
      }
    : {}
  const [onBehalf, setOnBehalf] = useState(false)
  // explicit opt-in to promotional WhatsApp; never ticked by default
  const [offers, setOffers] = useState(false)
  // Day use (hourly): check in and out the same day, inside a time slot.
  // The server prices it (one billable date) - the quote shows that rate.
  const [dayUse, setDayUse] = useState(false)
  const [slot, setSlot] = useState({ from: "10:00", to: "18:00" })
  const badSlot = dayUse && (!slot.from || !slot.to || slot.to <= slot.from)
  const [moreOpen, setMoreOpen] = useState(false)
  const [moreRooms, setMoreRooms] = useState<ExtraRoom[]>([])
  const [groupName, setGroupName] = useState("")
  const [moreQuotes, setMoreQuotes] = useState<(Quote | null)[]>([])
  const [addonQty, setAddonQty] = useState<Record<string, number>>({})
  const [extra, setExtra] = useState({
    special_requests: "",
    arrival_mode: "",
    arrival_ref: "",
    arrival_datetime: "",
    purpose: "",
    guest_category: "",
    extra_beds: 0,
    pickup_required: false,
    valet_parking: false,
    early_checkin: false,
    late_checkout: false,
  })
  const setX = (
    k: keyof typeof extra,
    v: string | number | boolean,
  ) => setExtra((e) => ({ ...e, [k]: v }))
  const [profile, setProfile] = useState<GuestHit | null>(() =>
    props.initial.guest
      ? {
          name: props.initial.guest,
          full_name: props.initial.guest_name ?? "",
          phone: props.initial.phone ?? null,
          email: null,
          vip: 0,
          blacklisted: 0,
          stays: props.initial.stays ?? 0,
          last_stay: null,
        }
      : null,
  )
  const [hits, setHits] = useState<GuestHit[]>([])
  // blacklist check for the profile this booking will attach to - asked of
  // the server (picked profile, else the phone match create_booking uses)
  const [alert, setAlert] = useState<GuestAlert | null>(null)
  const [blAck, setBlAck] = useState(false)
  const [blReason, setBlReason] = useState("")
  // company credit control (#238): the same check create_booking runs,
  // asked up front so the desk sees a warning or a block before booking
  const [credit, setCredit] = useState<CompanyCredit | null>(null)
  const [creditReason, setCreditReason] = useState("")
  const creditAmount = quote?.amount_after_tax ?? 0
  useEffect(() => {
    if (!form.company) {
      setCredit(null)
      return
    }
    let live = true
    const timer = setTimeout(() => {
      call<CompanyCredit>("kamra.credit.company_credit_status", {
        company: form.company,
        property: getCurrentProperty(),
        amount: creditAmount,
      })
        .then((c) => live && setCredit(c))
        .catch(
          () =>
            live &&
            setCredit({
              applies: true,
              ok: false,
              blocked: false,
              unverified: true,
              policy: "warn",
              company: form.company,
              reasons: ["Company credit could not be verified."],
            }),
        )
    }, 300)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [form.company, creditAmount])

  // Walk-in: book + ID + room + payment + check-in on this one screen
  // (issue #97). Arrival is always today, one room, no waitlist.
  const navigate = useNavigate()
  // ID documents and ways to pay come from the property's country pack
  const loc = useLocale()
  const moneyLoc = activeMoneyLocale()
  const homeText = (amount: number) => currencyText(loc.currency, amount, moneyLoc)
  const anyMoneyText = (amount: number, currency?: string) =>
    currencyText(currency || loc.currency, amount, moneyLoc)
  const { ensureUnlocked, status: pinStatus } = useCashierAuth()
  const [walkIn, setWalkIn] = useState(!!props.initial.walkIn)
  // Overnight stay: the arrival and departure time. They default to the
  // property's standard check-in / check-out time until the desk changes them,
  // and are kept on the reservation as planned times (display only, never price).
  const [stayTimes, setStayTimes] = useState<{ from: string | null; to: string | null }>({ from: null, to: null })
  const stayIn = stayTimes.from ?? (options?.property?.checkin_time ?? "").slice(0, 5)
  const stayOut = stayTimes.to ?? (options?.property?.checkout_time ?? "").slice(0, 5)
  // what is typed in the check-out box until it is a valid date after check-in;
  // null once it is (the nights then follow it)
  const [outDraft, setOutDraft] = useState<string | null>(null)
  const outError = outDraft !== null && parseDate(outDraft) !== null
  const slotArgs = dayUse
    ? { check_in_time: slot.from, check_out_time: slot.to }
    : !walkIn && stayIn && stayOut
      ? { check_in_time: stayIn, check_out_time: stayOut }
      : {}
  // the room clicked on the tape chart; dropped once the desk picks another type
  const pinnedRoom = !!props.initial.room && !walkIn && form.room_type === props.initial.room_type
  const [freeRooms, setFreeRooms] = useState<FreeRoom[] | null>(null)
  const [walk, setWalk] = useState({
    room: "",
    id_type: "",
    id_number: "",
    nationality: "",
    pay_mode: "Cash",
    pay_reference: "",
  })
  const setW = (k: keyof typeof walk, v: string) =>
    setWalk((w) => ({ ...w, [k]: v }))
  useEffect(() => {
    setWalk((w) => ({
      ...w,
      nationality: w.nationality || loc.default_nationality,
    }))
  }, [loc])

  function toggleWalkIn(on: boolean) {
    setWalkIn(on)
    setError(null)
    if (on) {
      setForm((f) => ({ ...f, check_in_date: todayLocal() }))
      setMoreRooms([])
      setGroupName("")
      setAddonQty({})
      setMoreOpen(false)
    }
  }

  // profile typeahead - find the returning guest before creating a dupe
  useEffect(() => {
    if (profile || form.guest_name.trim().length < 2) {
      setHits([])
      return
    }
    const t = setTimeout(
      () =>
        call<GuestHit[]>("kamra.api.guest_search", { q: form.guest_name })
          .then((rows) => setHits(Array.isArray(rows) ? rows : []))
          .catch(() => setHits([])),
      250,
    )
    return () => clearTimeout(t)
  }, [form.guest_name, profile])

  useEffect(() => {
    const guest = profile?.name
    const phone = form.phone && isPhoneComplete(form.phone, options?.property?.country)
      ? form.phone
      : ""
    if (!guest && !phone) {
      setAlert(null)
      return
    }
    let live = true
    const t = setTimeout(() => {
      call<GuestAlert>("kamra.api.guest_alert", { guest: guest || undefined, phone: guest ? undefined : phone })
        .then((a) => {
          if (!live) return
          setAlert(a)
          if (!a.blacklisted) {
            setBlAck(false)
            setBlReason("")
          }
        })
        .catch(() => live && setAlert(null))
    }, 250)
    return () => {
      live = false
      clearTimeout(t)
    }
  }, [profile?.name, form.phone, options?.property?.country])

  // When the desk did not pass a room type, keep auto-picking the cheapest
  // type that has rooms for the selected dates until they change it (#549).
  const autoRoomType = useRef(!props.initial.room_type)
  const optionsLoaded = useRef(false)
  const mealPlanTouched = useRef(false)
  const ratePlanTouched = useRef(false)

  const [syncRev, setSyncRev] = useState(0)
  useEffect(() => {
    const bump = () => setSyncRev((v) => v + 1)
    const offTariffs = onTariffsChanged(bump)
    const offSettings = onSettingsChanged(bump)
    return () => { offTariffs(); offSettings() }
  }, [])

  useEffect(() => {
    let live = true
    call<BookingOptions>("kamra.api.booking_options", { property: getCurrentProperty() }).then((o) => {
      if (!live) return
      setOptions(o)
      const isInitial = !optionsLoaded.current
      optionsLoaded.current = true
      setForm((f) => {
        const defaultMeal = o.meal_plans.find((m) => m.is_default)?.name ?? ""
        const mealValid = !f.meal_plan || o.meal_plans.some((m) => m.name === f.meal_plan)
        const nextMeal = isInitial && !mealPlanTouched.current
          ? (f.meal_plan && mealValid ? f.meal_plan : defaultMeal)
          : mealValid
            ? (f.meal_plan || (mealPlanTouched.current ? "" : defaultMeal))
            : defaultMeal

        const governed = o.all_active_plans_tariff && o.rate_plans.length > 0
        const defaultRate = o.default_rate_plan || o.default_plan
          || o.rate_plans.find((rp) => rp.is_default)?.name
          || ((o.rate_plan_required || governed) ? o.rate_plans[0]?.name : "") || ""
        const rateValid = !f.rate_plan || o.rate_plans.some((rp) => rp.name === f.rate_plan)
        const canKeepBlankRate = (!o.rate_plan_required && !governed) || !!f.company
        const nextRate = isInitial && !ratePlanTouched.current
          ? (f.rate_plan && rateValid ? f.rate_plan : defaultRate)
          : rateValid
            ? (f.rate_plan || (ratePlanTouched.current && canKeepBlankRate ? "" : defaultRate))
            : defaultRate

        return { ...f, meal_plan: nextMeal, rate_plan: nextRate }
      })
    })
    return () => { live = false }
  }, [syncRev])

  useEffect(() => {
    document.body.style.overflow = "hidden"
    return () => {
      document.body.style.overflow = ""
    }
  }, [])

  // With no plan picked, a company's negotiated plan prices the stay - the
  // server decides (pricing.negotiated_rate_plan); the quote says which.
  const companyPlan =
    form.company && !form.rate_plan && quote?.rate_plan
      ? options?.rate_plans.find((rp) => rp.name === quote.rate_plan) ?? null
      : null

  // a new booking cannot start in the past - the input's `min` is advisory
  // only (typed and pasted values bypass it), so gate the submit too
  const pastCheckIn = !!form.check_in_date && form.check_in_date < todayLocal()

  // a date typed key by key is empty, or a year like 0002, until the year is
  // complete: leave check-out empty and hold quotes and submit until then
  const badDate = !parseDate(form.check_in_date) || pastCheckIn
  const checkOut = badDate
    ? ""
    : dayUse
      ? form.check_in_date
      : shiftDate(form.check_in_date, Math.max(1, form.nights))

  // Nights the desk priced by hand (date -> price, in the basis the rates
  // screen shows). Only a Revenue Manager / Hotel Admin may; an OTA total
  // already sets the price, and a day-use stay is priced by its slot.
  const [nightPrices, setNightPrices] = useState<Record<string, number>>({})
  const planRequired = !!options?.rate_plan_required || (!!options?.all_active_plans_tariff && options.rate_plans.length > 0)
  const canSetPrice = !!options?.can_set_rate && otaTotal <= 0 && !dayUse && !form.rate_plan && !planRequired
  const stayNights = badDate || dayUse
    ? []
    : Array.from({ length: Math.max(1, form.nights) }, (_, i) => shiftDate(form.check_in_date, i))
  const nightPricesArg = canSetPrice ? typedNights(nightPrices, stayNights) : []
  // a night that is no longer in the stay is dropped with the date change
  const stayKey = stayNights.join(",")
  useEffect(() => {
    if (!stayKey) return // a date still being typed: keep what was entered
    setNightPrices((p) => {
      const keep = Object.fromEntries(Object.entries(p).filter(([d]) => stayKey.split(",").includes(d)))
      return Object.keys(keep).length === Object.keys(p).length ? p : keep
    })
  }, [stayKey])
  const nightPricesParam = nightPricesArg.length ? { night_prices: nightPricesArg } : {}

  // The desk's discount (OPERA Look to Book / Walk-In): % or an amount off
  // each night, with a reason, on any rate plan that allows one - tariff
  // plans and walk-ins included. One room at a time (a group's rooms are
  // discounted on their own stays afterwards); an OTA total sets the price.
  const [discount, setDiscount] = useState<DiscountState>(emptyDiscount)
  const [discountSupervisor, setDiscountSupervisor] = useState("")
  const [discountPin, setDiscountPin] = useState("")
  const [compSupervisor, setCompSupervisor] = useState("")
  const [compPin, setCompPin] = useState("")
  useEffect(() => {
    // a PIN given for one plan (or company's negotiated plan) is never sent for another
    setCompSupervisor("")
    setCompPin("")
  }, [form.rate_plan, form.company])
  const canDiscount = !!options?.can_discount && otaTotal <= 0 && moreRooms.length === 0
  const discountPlan = form.rate_plan || quote?.rate_plan || ""
  const discountOk = discountEligible(options?.rate_plans, discountPlan)
  // OPERA's Fixed Rate: a Revenue Manager / Hotel Admin may fix the rate of a
  // plan that takes no discount and discount it (kamra.api fixed_rate)
  const [fixedOverride, setFixedOverride] = useState(false)
  const canFixRate = !discountOk && !!options?.can_set_rate
  const discountAllowed = discountOk || (canFixRate && fixedOverride)
  useEffect(() => {
    // a plan that can be discounted needs no override; one that can't
    // clears what was entered unless a manager fixes its rate (OPERA)
    if (discountOk) setFixedOverride(false)
    if (discountAllowed) return
    setDiscount(emptyDiscount())
    setDiscountSupervisor("")
    setDiscountPin("")
  }, [discountOk, discountAllowed])
  const quoteTariff = otaTotal <= 0 ? quote?.tariff : null
  const contractSummary = otaTotal <= 0 ? (quote?.contract_summary ?? null) : null
  const planCurrency = (
    options?.rate_plans?.find((p) => p.name === discountPlan)?.currency || ""
  ).trim().toUpperCase()
  const [knownRate, setKnownRate] = useState<{ key: string; cur: string; fx: number | null } | null>(null)
  useEffect(() => {
    const qCur = (quote?.rate_currency || quote?.tariff?.currency || "").trim().toUpperCase()
    if (!qCur) return
    const qFx = quote?.contract_summary?.fx_rate ?? quote?.tariff?.fx_rate ?? null
    setKnownRate((prev) =>
      prev && prev.key === rateCurKey && prev.cur === qCur && prev.fx === qFx
        ? prev
        : { key: rateCurKey, cur: qCur, fx: qFx },
    )
  }, [quote, rateCurKey])
  const rateCurrency = (
    quote?.rate_currency
    || quoteTariff?.currency
    || (knownRate?.key === rateCurKey ? knownRate.cur : "")
    || planCurrency
  ).trim().toUpperCase()
  const isForeignRate = Boolean(rateCurrency && loc.currency && rateCurrency !== loc.currency.toUpperCase())
  const rateFx = contractSummary?.fx_rate ?? quoteTariff?.fx_rate ?? (knownRate?.key === rateCurKey ? knownRate.fx : null)
  useEffect(() => {
    if (!rateCurrency) return
    const allowed = new Set([rateCurrency, (loc.currency || "").toUpperCase()].filter(Boolean))
    setDiscount((prev) => {
      const stayBad = Boolean(prev.currency && !allowed.has(prev.currency.toUpperCase()))
      const nightsBad = Object.values(prev.nights).some(
        (n) => Boolean(n.currency && !allowed.has(n.currency.toUpperCase())),
      )
      if (!stayBad && !nightsBad) return prev
      const nextNights = Object.fromEntries(
        Object.entries(prev.nights).map(([k, v]) => [
          k,
          v.currency && !allowed.has(v.currency.toUpperCase()) ? { ...v, currency: undefined } : v,
        ]),
      )
      return { ...prev, currency: stayBad ? undefined : prev.currency, nights: nextNights }
    })
  }, [rateCurrency, loc.currency])
  const discountEntered = canDiscount && discountAllowed
    ? discountArgs(discount, stayNights, rateCurrency || undefined)
    : {}
  const discountParam = discountOk || !(discountEntered.discount_type || discountEntered.night_discounts)
    ? discountEntered : { ...discountEntered, fixed_rate: 1 as const }
  const discountGov: DiscountGovernance | null = quote?.discount_governance ?? null
  // a Complimentary or House Use plan: a manager signs the booking off
  // (kamra.api.comp_rate_governance). When the discount asks for a manager
  // too, one whose roles cover both signs both off (OPERA's one authorizer
  // for both prompts); otherwise the comp takes its own sign-off.
  const compGov = quote?.comp_governance ?? null
  const discountCoversComp = !!discountGov?.needs_supervisor && !!discountSupervisor &&
    !!compGov?.supervisors.some((s) => s.user === discountSupervisor)
  const compAsks = !!compGov?.needs_supervisor && !discountCoversComp
  const compSignOff = compAsks && compSupervisor && compPin.trim()
    ? { comp_supervisor_user: compSupervisor, comp_supervisor_pin: compPin.trim() } : {}
  const discountSignOff = {
    ...supervisorArgs(discountGov, discountSupervisor, discountPin),
    ...compSignOff,
    // the desk's optional note goes with the booking, never with a quote
    ...discountNote(discount, discountEntered),
  }
  // the dates the quote priced, so the Total always names its own nights:
  // its first night to the day after its last
  const quoteStay = (() => {
    const first = quote?.nightly?.[0]?.date ?? form.check_in_date
    const last = quote?.nightly?.at(-1)?.date
    return { from: first, to: last ? shiftDate(last, 1) : shiftDate(first, Math.max(1, quote?.nights ?? 1)) }
  })()
  const discountReasonText = (() => {
    const r = options?.discount_reasons?.find((x) => x.name === discount.reason)
    return r ? translate(r.description || r.name) : discount.reason
  })()

  useEffect(() => {
    if (!autoRoomType.current || !options?.room_types.length || badDate) return
    let live = true
    const types = options.room_types
    Promise.all(
      types.map((rt) =>
        call<FreeRoom[]>("kamra.api.available_rooms", {
          property: getCurrentProperty(),
          room_type: rt.name,
          check_in_date: form.check_in_date,
          check_out_date: checkOut,
          ...(walkIn ? { ready_now: 1 } : {}),
        })
          .then((rs) => ({ name: rt.name, n: rs.length }))
          .catch(() => ({ name: rt.name, n: 0 })),
      ),
    ).then((hits) => {
      if (!live || !autoRoomType.current) return
      const counts = Object.fromEntries(hits.map((h) => [h.name, h.n]))
      const pick = pickDefaultRoomType(types, counts)
      setForm((f) => (f.room_type === pick ? f : { ...f, room_type: pick }))
    })
    return () => {
      live = false
    }
  }, [options, form.check_in_date, checkOut, walkIn, badDate])

  // a blacklisted guest needs an explicit, reasoned confirmation (Hotel
  // Admin only - the server enforces it); a multi-room group can't override
  const blacklisted = !!alert?.blacklisted
  const blacklistBlocked =
    blacklisted && (moreRooms.length > 0 || !blAck || !blReason.trim() || !alert?.can_override)
  const blacklistOverride = blacklisted && blAck ? blReason.trim() || undefined : undefined

  const creditFails = !!credit?.applies && !credit.ok
  const creditBlocked =
    creditFails && !!credit?.blocked &&
    (moreRooms.length > 0 || !credit.can_override || !creditReason.trim())
  const creditOverride =
    creditFails && credit?.blocked && creditReason.trim() ? creditReason.trim() : undefined

  // phone is optional, but a half-typed one is a data-entry slip, not a choice
  const country = options?.property?.country
  const badPhone =
    !isPhoneComplete(form.phone, country) ||
    (onBehalf && !isPhoneComplete(form.booked_by_phone, country))

  useEffect(() => {
    if (!options || !form.room_type) return
    if (badDate) {
      setQuoted(null)
      setQuoting(false)
      return
    }
    let live = true
    setQuoted(null)
    setQuoting(true)
    const t = setTimeout(() => {
      call<Quote>("kamra.api.get_quote", {
        property: getCurrentProperty(),
        room_type: form.room_type,
        check_in_date: form.check_in_date,
        check_out_date: checkOut,
        adults: form.adults,
        children: form.children,
        meal_plan: form.meal_plan || undefined,
        rate_plan: form.rate_plan || undefined,
        company: form.company || undefined,
        voucher_code: form.voucher_code || undefined,
        over_capacity_charge: extraCharge || undefined,
        pets: form.pets || undefined,
        extra_beds: extra.extra_beds || undefined,
        ...slotArgs,
        ...nightPricesParam,
        ...discountParam,
      })
        .then((q) => {
          if (!live) return
          setQuoted({ rateKey: rateCurKey, quote: q })
          setError(null)
        })
        .catch((e) => {
          if (!live) return
          setQuoted(null)
          setError(shortErr(e))
        })
        .finally(() => { if (live) setQuoting(false) })
    }, 300)
    return () => { live = false; clearTimeout(t) }
  }, [
    form.room_type,
    form.check_in_date,
    form.nights,
    form.adults,
    form.children,
    form.meal_plan,
    options,
    form.rate_plan,
    form.company,
    form.voucher_code,
    // the agreed amount for guests above capacity rides the room night, so a
    // change to it has to re-quote - without this the panel kept showing the
    // rate card total while the desk typed the price they had agreed
    form.over_capacity_charge,
    form.over_capacity_guests,
    form.pets,
    extra.extra_beds,
    slot.from,
    slot.to,
    checkOut,
    // a typed price re-quotes: the totals, tax and advance follow it
    JSON.stringify(nightPricesArg),
    // so does the desk's discount: the walk-in's amount collected follows it
    JSON.stringify(discountParam),
    syncRev,
  ])

  // quotes for the additional rooms
  useEffect(() => {
    if (moreRooms.length === 0 || badDate) {
      setMoreQuotes([])
      return
    }
    const t = setTimeout(() => {
      Promise.all(
        moreRooms.map((r) =>
          r.room_type
            ? call<Quote>("kamra.api.get_quote", {
                property: getCurrentProperty(),
                room_type: r.room_type,
                check_in_date: form.check_in_date,
                check_out_date: checkOut,
                adults: r.adults,
                children: r.children,
                meal_plan: r.meal_plan || undefined,
                rate_plan: form.rate_plan || undefined,
                company: form.company || undefined,
                ...(r.night_prices?.length && canSetPrice ? { night_prices: r.night_prices } : {}),
              }).catch(() => null)
            : Promise.resolve(null),
        ),
      ).then(setMoreQuotes)
    }, 300)
    return () => clearTimeout(t)
  }, [moreRooms, form.check_in_date, checkOut, form.rate_plan, form.company, canSetPrice, syncRev])

  // walk-in: the actual rooms free tonight, clean ones first
  useEffect(() => {
    if (!walkIn || !form.room_type || badDate) return
    setFreeRooms(null)
    let live = true
    call<FreeRoom[]>("kamra.api.available_rooms", {
      property: getCurrentProperty(),
      room_type: form.room_type,
      check_in_date: form.check_in_date,
      check_out_date: checkOut,
      // the guest takes the key now: a room whose guest has not checked
      // out yet is not free, even on their departure day
      ready_now: 1,
    })
      .then((rs) => {
        if (!live) return
        const rank = (r: FreeRoom) =>
          r.housekeeping_status === "Clean" ||
          r.housekeeping_status === "Inspected"
            ? 0
            : 1
        const sorted = [...rs].sort(
          (a, b) =>
            rank(a) - rank(b) ||
            a.room_number.localeCompare(b.room_number, undefined, {
              numeric: true,
            }),
        )
        setFreeRooms(sorted)
        setWalk((w) => ({
          ...w,
          room: sorted.some((r) => r.name === w.room)
            ? w.room
            : (sorted[0]?.name ?? ""),
        }))
      })
      .catch(() => live && setFreeRooms([]))
    return () => {
      live = false
    }
  }, [walkIn, form.room_type, form.check_in_date, checkOut])

  function shortErr(e: unknown): string {
    const body = (e as { body?: string }).body
    if (body) {
      try {
        const msgs = JSON.parse(JSON.parse(body)._server_messages ?? "[]")
        if (msgs.length)
          return htmlToText(String(JSON.parse(msgs[0]).message))
      } catch {
        /* fall through */
      }
    }
    return (e as Error).message
  }

  async function submit(waitlist = false) {
    if (!waitlist) {
      setAttempted(true)
      setSubmitError("")
      if (problems.length) { setMessagesOpen(true); return }
    }
    setBusy(true)
    setError(null)
    try {
      if (waitlist) {
        const res = await call<{ reservation: string; room: string | null; room_number: string | null }>(
          "kamra.api.create_booking",
          {
            property: getCurrentProperty(),
            over_capacity_reason: form.over_capacity_reason || undefined,
            over_capacity_guests: form.over_capacity_guests || undefined,
            over_capacity_charge: extraCharge || undefined,
            guest_name: form.guest_name,
            phone: form.phone || undefined,
            guest: profile?.name,
            room_type: form.room_type,
            check_in_date: form.check_in_date,
            check_out_date: checkOut,
            adults: form.adults,
            children: form.children,
            meal_plan: form.meal_plan || undefined,
            rate_plan: form.rate_plan || undefined,
            voucher_code: form.voucher_code || undefined,
            pets: form.pets || undefined,
            company: form.company || undefined,
            booking_type: form.booking_type || (form.company ? "Corporate" : undefined),
            waitlist: 1,
            ...nightPricesParam,
            ...discountParam,
            ...discountSignOff,
            ...slotArgs,
            blacklist_override_reason: blacklistOverride,
          },
        )
        setDone({ ref: res.reservation, room: null, waitlist: true })
        props.onBooked()
        return
      }
      if (moreRooms.length > 0 && !dayUse) {
        // several rooms → one group booking, billable as a block
        const rooms = [
          {
            room_type: form.room_type,
            count: 1,
            adults: form.adults,
            children: form.children,
            meal_plan: form.meal_plan || undefined,
            // the main room's own nights; each extra room carries its own below
            ...nightPricesParam,
          },
          ...moreRooms.map((r) => ({
            room_type: r.room_type,
            count: r.count,
            adults: r.adults,
            children: r.children,
            meal_plan: r.meal_plan || undefined,
            guest_name: (r.count === 1 && r.guest_name.trim()) || undefined,
            night_prices: (canSetPrice && r.night_prices?.length ? r.night_prices : undefined),
          })),
        ]
        const roomCount = rooms.reduce((s, r) => s + r.count, 0)
        const out = await call<{
          group_booking: string
          created: string[]
          skipped: { room_type: string; reason: string }[]
        }>("kamra.api.create_group_booking", {
          property: getCurrentProperty(),
          group_name:
            groupName.trim() || `${form.guest_name} · ${roomCount} rooms`,
          check_in_date: form.check_in_date,
          check_out_date: checkOut,
          rooms,
          guest_name: form.guest_name,
          phone: form.phone || undefined,
          company: form.company || undefined,
          booking_type: form.booking_type || undefined,
          rate_plan: form.rate_plan || undefined,
          ...viaArgs,
          // a comp or house-use plan: one sign-off for every room
          ...compSignOff,
        })
        if (out.skipped.length > 0) {
          setError(
            translate("Booked {v0} of {v1} rooms - ", { v0: String(out.created.length), v1: String(roomCount) }) +
              out.skipped.map((s) => s.reason).join("; "),
          )
          if (out.created.length === 0) return
        }
        setDone({ ref: out.group_booking, room: null })
        props.onBooked()
        return
      }
      const res = await call<{ reservation: string; room: string | null; room_number: string | null }>(
        "kamra.api.create_booking",
        {
          property: getCurrentProperty(),
          over_capacity_reason: form.over_capacity_reason || undefined,
          over_capacity_guests: form.over_capacity_guests || undefined,
          over_capacity_charge: extraCharge || undefined,
        guest_name: form.guest_name,
        phone: form.phone || undefined,
        guest: profile?.name,
        room_type: form.room_type,
        room: pinnedRoom ? props.initial.room : undefined,
        check_in_date: form.check_in_date,
        check_out_date: checkOut,
        adults: form.adults,
        children: form.children,
        meal_plan: form.meal_plan || undefined,
        rate_plan: form.rate_plan || undefined,
        voucher_code: form.voucher_code || undefined,
        pets: form.pets || undefined,
        company: form.company || undefined,
        travel_agent: form.travel_agent || undefined,
        booking_type: form.booking_type || (form.company ? "Corporate" : undefined),
        market_segment: form.market_segment || undefined,
        booked_by_name: onBehalf ? form.booked_by_name || undefined : undefined,
        booked_by_phone: onBehalf
          ? form.booked_by_phone || undefined
          : undefined,
        booker_relation: onBehalf
          ? form.booker_relation || undefined
          : undefined,
        contact_preference:
          onBehalf && form.booked_by_name ? form.contact_preference : undefined,
        guest_category: extra.guest_category || undefined,
        stay_details: {
          arrival_mode: extra.arrival_mode || undefined,
          arrival_ref: extra.arrival_ref || undefined,
          arrival_datetime: extra.arrival_datetime || undefined,
          purpose: extra.purpose || undefined,
          extra_beds: extra.extra_beds || undefined,
          pickup_required: extra.pickup_required ? 1 : undefined,
          valet_parking: extra.valet_parking ? 1 : undefined,
          early_checkin: extra.early_checkin ? 1 : undefined,
          late_checkout: extra.late_checkout ? 1 : undefined,
        },
        instructions: extra.special_requests
          ? [
              {
                department: "Front Desk",
                instruction: extra.special_requests,
              },
            ]
          : undefined,
        addons: Object.entries(addonQty)
          .filter(([, q]) => q > 0)
          .map(([experience, qty]) => ({ experience, qty })),
        ...slotArgs,
        blacklist_override_reason: blacklistOverride,
        credit_override_reason: creditOverride,
        ...viaArgs,
        marketing_consent: offers ? 1 : undefined,
        ...nightPricesParam,
        ...discountParam,
        ...discountSignOff,
      })
      await addOthers(res.reservation)
      setDone({ ref: res.reservation, room: res.room, room_number: res.room_number })
      props.onBooked()
      call<{ deposit: DepositState }>("kamra.api.reservation_detail", {
        reservation: res.reservation,
      })
        .then((d) =>
          setDone((cur) => (cur && cur.ref === res.reservation ? { ...cur, deposit: d.deposit } : cur)),
        )
        .catch(() => {})
    } catch (e) {
      if (waitlist) setError(shortErr(e))
      else { setSubmitError(shortErr(e)); setMessagesOpen(true) }
    } finally {
      setBusy(false)
    }
  }

  // The first quote uses the property tender. A different plan starts with its own currency.
  const [requestedTender, setRequestedTender] = useState<{ key: string; currency: string } | null>(null)
  const [manualPayment, setManualPayment] = useState<{ key: string; currency: string; amount: string } | null>(null)
  const [tenderPlan, setTenderPlan] = useState<{ key: string; followRate: boolean } | null>(null)
  useEffect(() => {
    if (!walkIn || !quote) return
    setTenderPlan((current) => current?.key === rateCurKey ? current : { key: rateCurKey, followRate: !!current })
  }, [walkIn, quote, rateCurKey])
  useEffect(() => {
    setRequestedTender((current) => current?.key === rateCurKey ? current : null)
    setManualPayment((current) => current?.key === rateCurKey ? current : null)
  }, [rateCurKey])

  const { today: settlementRates } = useTenderRates(getCurrentProperty(), walkIn)
  const modes = tenderModes(loc, loc.currency)
  const tariffTenderCurrency = quote?.rate_currency || quote?.tariff?.currency || loc.currency
  const followRate = tenderPlan && (tenderPlan.followRate || tenderPlan.key !== rateCurKey)
  const defaultTender = resolveTender(modes, loc.currency, { mode: walk.pay_mode, currency: "" })
  const tariffModes = modes.filter(({ currencies }) => currencies.includes(tariffTenderCurrency))
  const tariffMode = tariffModes.find(({ mode }) => mode === defaultTender.mode) ?? tariffModes[0]
  const tariffTender = { mode: tariffMode?.mode ?? "", currency: tariffTenderCurrency }
  const tender = requestedTender?.key === rateCurKey
    ? resolveTender(modes, loc.currency, { mode: defaultTender.mode, currency: requestedTender.currency })
    : followRate && tenderReady(tariffTender, loc.currency, settlementRates) ? tariffTender : defaultTender
  const payCurrency = tender.currency
  const foreign = !!payCurrency && payCurrency !== loc.currency
  const paymentReady = tenderReady(tender, loc.currency, settlementRates)
  // one key per open dialog: a double-tap replays instead of double-booking
  const [walkInKey] = useState(() =>
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `walkin-${Date.now()}-${Math.random().toString(36).slice(2)}`,
  )

  async function submitWalkIn() {
    // never reach the cashier PIN or the payment with a discount that
    // would be refused: the button is off, and this holds it too
    if (discountProblem) {
      setError(t(discountProblem))
      return
    }
    setBusy(true)
    setError(null)
    try {
      const amount = payAmount
      if (amount > 0 && !paymentReady) throw new Error(t("Choose an available payment method and currency."))
      if (foreign && amount > 0 && !fx?.quote) throw new Error(fx?.error || t("Wait for the settlement preview."))
      if (amount > 0 && pinStatus?.required) await ensureUnlocked()
      const res = await call<WalkInResult>("kamra.api.walk_in", {
        property: getCurrentProperty(),
        room_type: form.room_type,
        room: walk.room,
        check_out_date: checkOut,
        guest_name: form.guest_name,
        phone: form.phone || undefined,
        guest: profile?.name,
        adults: form.adults,
        children: form.children,
        meal_plan: form.meal_plan || undefined,
        rate_plan: form.rate_plan || undefined,
        company: form.company || undefined,
        voucher_code: form.voucher_code || undefined,
        id_type: walk.id_type || undefined,
        id_number: walk.id_number.trim() || undefined,
        nationality: walk.nationality.trim() || undefined,
        payment_mode: amount > 0 ? tender.mode : undefined,
        payment_amount: amount, currency: foreign ? payCurrency : undefined, quote_id: fx?.quote?.quote_id,
        extra_beds: extra.extra_beds || undefined,
        payment_reference: walk.pay_reference.trim() || undefined,
        idempotency_key: walkInKey,
        ...slotArgs,
        blacklist_override_reason: blacklistOverride,
        ...viaArgs,
        ...nightPricesParam,
        ...discountParam,
        ...discountSignOff,
        // book, assign and take the money now; the guided check-in
        // (property's required steps) finishes it right after
        defer_check_in: 1,
      })
      await addOthers(res.reservation)
      setDone({ ref: res.reservation, room: res.room, room_number: res.room_number, walkIn: res })
      setGuidedCheckIn(res.checked_in === 0 ? res.reservation : null)
      props.onBooked()
    } catch (e) {
      setError(shortErr(e))
    } finally {
      setBusy(false)
    }
  }

  const set = (k: string, v: string | number) =>
    setForm((f) => ({ ...f, [k]: v }))

  const selectedRt = options?.room_types.find(
    (rt) => rt.name === form.room_type,
  )
  const roomTypeName = selectedRt?.room_type_name ?? ""
  // Adults and Children stop at what the room sleeps; anyone above that is an
  // extra guest by arrangement, counted on its own up to the room type's
  // extra beds, at a price the desk agrees and a reason that records who did
  const adultsCap = sleeps(selectedRt, "adults")
  const childrenCap = sleeps(selectedRt, "children")
  const extraBeds = Number(selectedRt?.max_extra_beds || 0)
  // a charge left behind after the count goes back to 0 must not bill
  const extraCharge =
    form.over_capacity_guests > 0 ? form.over_capacity_charge : 0
  const extraNeedsReason =
    form.over_capacity_guests > 0 && !form.over_capacity_reason.trim()

  const problems: {field: string; severity: "error"; message: string}[] = []
  function requireField(blocked: boolean, field: string, message: string) {
    if (blocked) problems.push({field, severity: "error", message})
  }
  requireField(!form.guest_name.trim(), "guest", t("Enter the guest name."))
  requireField(!form.room_type || !quote || quoting, "room", t("Select a room type and wait for a valid quote."))
  requireField(badDate, "arrival", pastCheckIn ? t("That date has already passed.") : t("Enter a valid check-in date."))
  requireField(!isPhoneComplete(form.phone, country), "phone", t("Check the guest phone number."))
  requireField(onBehalf && !isPhoneComplete(form.booked_by_phone, country), "booker-phone", t("Check the booker phone number."))
  requireField(badSlot, "slot", t("The slot must end after it starts."))
  requireField(outDraft !== null, "departure", t("Check-out must be after check-in."))
  requireField(blacklistBlocked, "blacklist", t("Resolve the guest blacklist restriction."))
  requireField(creditBlocked, "credit", t("Resolve the company credit restriction."))
  requireField(extraNeedsReason, "extra", t("Enter the reason for the extra guests."))
  requireField(planRequired && !form.rate_plan && !companyPlan, "rate-plan", t("Choose a rate plan for this booking."))
  // the discount's own checks, for a booking and a walk-in alike: the
  // bounds (the stay's against its lowest night, each Daily Details night
  // against its own), a reason, and a manager's PIN when the server asks
  const discountBad = canDiscount && discountAllowed
    ? discountError(discount, quote ? discountNights(quote) : undefined, {
        rateCurrency: isForeignRate ? rateCurrency : undefined,
        homeCurrency: loc.currency || undefined,
      }) : null
  const discountAsked = !!(discountParam.discount_type || discountParam.night_discounts)
  const discountProblem = discountBad
    ?? (discountAsked && !discountParam.discount_reason ? "Pick a reason for the discount."
      : discountAsked && !discountReady({ args: discountParam, governance: discountGov,
          supervisor: discountSupervisor, pin: discountPin, fixedOverride: discountAllowed && !discountOk })
        ? "A manager signs this off with their PIN." : null)
  if (discountProblem) requireField(true, "discount", t(discountProblem))
  const compProblem = compAsks && !(compSupervisor && compPin.trim())
  requireField(compProblem, "comp", t("A manager signs this off with their PIN."))
  const messages = attempted ? [...problems, ...(submitError ? [{field: "server", message: submitError}] : [])] : []
  const target = (field: string) => ({id: `booking-${field}`, "aria-invalid": attempted && problems.some(p => p.field === field) || undefined})
  function jump(field: string) {
    const focusField = () => {
      const element = document.getElementById(`booking-${field}`)
      element?.focus(); element?.scrollIntoView({block: "center"})
    }
    setMessagesOpen(false)
    if (field === "booker-phone" && !moreOpen) {
      setMoreOpen(true)
      requestAnimationFrame(focusField)
    } else focusField()
  }

  // the advance each room's dates ask for, priced by the server
  const advanceNow = quote
    ? (quote.advance?.amount ?? 0) +
      moreQuotes.reduce((s, q) => s + (q?.advance?.amount ?? 0), 0)
    : 0
  // each room's own advance (server amounts): one % for all reads as one
  // figure, mixed ones are listed per room
  const advanceRooms = quote
    ? [
        { room_type: form.room_type, advance: quote.advance },
        ...moreRooms.map((r, i) => ({ room_type: r.room_type, advance: moreQuotes[i]?.advance })),
      ].map((r) => ({
        name: options?.room_types.find((x) => x.name === r.room_type)?.room_type_name ?? r.room_type,
        percent: r.advance?.percent ?? 0,
        amount: r.advance?.amount ?? 0,
      }))
    : []
  const advancePercents = new Set(advanceRooms.map((r) => r.percent))

  const addonsGross = Object.entries(addonQty).reduce((s, [n, q]) => {
    const x = options?.experiences.find((e) => e.name === n)
    return x && q > 0 ? s + q * x.price * (1 + x.gst_rate / 100) : s
  }, 0)

  const grandTotal = (() => {
    if (!quote) return 0
    // the OTA's sold price covers every room of the booking
    return (
      (otaTotal > 0
        ? otaTotal
        : quote.amount_after_tax +
          moreQuotes.reduce(
            (s, q, i) =>
              s + (q?.amount_after_tax ?? 0) * (moreRooms[i]?.count ?? 1),
            0,
          )) +
      addonsGross
    )
  })()
  const quoteTaxBasis = quote?.tax_basis === undefined ? loc.tax_basis : quote.tax_basis

  const pureContractStay = Boolean(
    contractSummary &&
    !contractSummary.has_home_currency_extras &&
    moreRooms.length === 0 &&
    addonsGross === 0,
  )
  const fullAmount = (currency: string) => {
    const curUp = currency.toUpperCase()
    if (pureContractStay && contractSummary && contractSummary.currency.toUpperCase() === curUp) {
      const cashierRate = settlementRates[curUp]
      const contractFx = contractSummary.fx_rate
      const fxMatches = !cashierRate || !contractFx || Math.abs(cashierRate - contractFx) / contractFx < 1e-4
      if (fxMatches) {
        return String(roundMoney(contractSummary.room_after_tax_total, curUp))
      }
    }
    if (
      quoteTariff &&
      quote &&
      quoteTariff.currency === currency &&
      quote.agreed_rate != null &&
      quote.agreed_rate * quote.nights === grandTotal &&
      !(quote.reservation_discount_total ?? 0)
    ) {
      return String(roundMoney(quoteTariff.nightly * quote.nights, currency))
    }
    return tenderAmount(grandTotal, currency, loc.currency, settlementRates)
  }
  const suggestedAmount = quote && paymentReady ? fullAmount(payCurrency) : ""
  const matchingManualPayment = manualPayment?.key === rateCurKey && manualPayment.currency === payCurrency
  const paymentInput = quote && matchingManualPayment ? manualPayment.amount : suggestedAmount
  const payAmount = Number(paymentInput) || 0
  const fx = useSettlementQuote(getCurrentProperty(), foreign && paymentReady ? payCurrency : "", payAmount)
  // taking the walk-in's money needs the operator's till open
  const { till, refresh: refreshTill } = useTill(undefined, walkIn)
  const tillClosed = walkIn && payAmount > 0 && !!till && !till.open
  const walkInBlocked =
    blacklistBlocked ||
    // the same discount checks as a booking, before any money or PIN
    !!discountProblem ||
    compProblem ||
    busy ||
    !form.guest_name ||
    !quote ||
    badDate ||
    badPhone ||
    badSlot ||
    !walk.room ||
    (planRequired && !form.rate_plan && !companyPlan) ||
    payAmount < 0 ||
    (payAmount > 0 && (!paymentReady || (foreign && !fx?.quote))) ||
    (!!walk.id_number.trim() && !walk.id_type)

  // the server judges the terms from today (season, tiers, free window);
  // a window that has already closed reads as non-refundable, never as a
  // "free until" date in the past
  const cancelText = (() => {
    const c = quote?.cancellation
    if (!c) return ""
    if (c.non_refundable_season)
      return t("Non-refundable dates ({season}): the full stay is charged on cancellation.", {
        season: c.non_refundable_season,
      })
    if (!c.free_now)
      return t("Non-refundable from now: {fee} is charged on cancellation.", {
        fee: c.fee_label,
      })
    if (c.free_until)
      return t("Free cancellation until {date}; after that {fee} is charged.", {
        date: formatDate(c.free_until),
        fee: c.fee_label,
      })
    return t("Free cancellation.")
  })()

  return (
    <div
      className="booking-dialog fixed inset-0 z-50"
      ref={dialog} tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={(walkIn ? t("Walk-in") : t("New booking"))}
      onKeyDown={(e) => {
        if (e.key === "Escape") { props.onClose(); return }
        if (e.key !== "Tab") return
        const controls = [...e.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]')].filter(el => el.getClientRects().length)
        const first = controls[0], last = controls.at(-1)
        if (e.shiftKey && (document.activeElement === first || document.activeElement === dialog.current)) { e.preventDefault(); last?.focus() }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first?.focus() }
      }}
    >
      <div
        className="absolute inset-0 bg-black/40 animate-fade-in"
        onClick={props.onClose}
        aria-hidden
      />
      <div
        className="absolute inset-y-0 right-0 flex h-full w-full flex-col bg-surface shadow-2xl animate-sheet-in md:w-2/3"
      >
        <header className="flex shrink-0 items-center justify-between gap-4 border-b border-line px-6 py-4 md:px-8">
          <div className="min-w-0">
            <h2 className="text-xl font-semibold tracking-tight text-ink">
              {walkIn ? t("Walk-in") : t("New booking")}
            </h2>
            <p className="mt-0.5 truncate text-sm text-muted-ink">
              {getCurrentProperty()}
              <span className="text-muted-ink"> · </span>
              {walkIn
                ? t("Book, check in and collect in one step")
                : t("Live quote as you type")}
              {pinnedRoom && (
                <>
                  <span className="text-muted-ink"> · </span>
                  {t("Room {n}", { n: roomLabel(props.initial) })}
                </>
              )}
            </p>
          </div>
          {!done && (
            <div
              role="radiogroup"
              aria-label={t("Booking mode")}
              className="ml-auto inline-flex shrink-0 rounded-lg bg-subtle p-0.5 text-sm font-medium"
            >
              {[
                { on: false, label: t("Reservation") },
                { on: true, label: t("Walk-in") },
              ].map((m) => (
                <button
                  key={m.label}
                  type="button"
                  role="radio"
                  aria-checked={walkIn === m.on}
                  onClick={() => toggleWalkIn(m.on)}
                  className={
                    "rounded-md px-3 py-1.5 transition-colors " +
                    (walkIn === m.on
                      ? "bg-surface text-ink shadow-sm"
                      : "text-muted-ink hover:text-ink")
                  }
                >
                  {translate(m.label)}
                </button>
              ))}
            </div>
          )}
          <Button variant="ghost" onClick={props.onClose} aria-label={t("Close")}>
            <X className="size-5" />
          </Button>
        </header>

        {done ? (
          <div className="space-y-5 overflow-y-auto px-6 py-8 md:px-8">
            {done.walkIn ? (
              <div className="rounded-xl border border-success-line bg-success-subtle px-5 py-4 text-success-ink">
                <p className="text-lg font-semibold">
                  {done.walkIn.checked_in === 0 && !walkInCheckedIn
                    ? t("Booked · Room {n}. Check-in not finished yet.", { n: done.walkIn.room_number })
                    : t("Checked in · Room {n}", { n: done.walkIn.room_number })}
                </p>
                <p className="mt-1 text-sm">
                  {done.ref}
                  {" · "}
                  {(done.walkIn.paid ?? 0) > 0
                    ? t("{amt} of {total} collected", {
                        amt: homeText(done.walkIn.paid ?? 0),
                        total: homeText(done.walkIn.amount_after_tax ?? 0),
                      })
                    : t("Nothing collected yet - it's all on the bill.")}
                </p>
              </div>
            ) : done.waitlist ? (
              <div className="rounded-xl border border-warning-line bg-warning-subtle px-5 py-4 text-warning-ink">
                <p className="text-lg font-semibold">{t("Waitlisted · {ref}", { ref: done.ref })}</p>
                <p className="mt-1 text-sm">
                  {t("Parked with no room. Promote it from the reservation when a room frees. Auto-purges 2 days after departure.")}
                </p>
              </div>
            ) : (
              <div className="rounded-xl border border-success-line bg-success-subtle px-5 py-4 text-success-ink">
                <p className="text-lg font-semibold">{t("Booked · {ref}", { ref: done.ref })}</p>
                <p className="mt-1 text-sm">
                  {done.room
                    ? t("Room {n} assigned.", { n: roomLabel(done) })
                    : t("No room auto-assigned - pick one from Reservations.")}{" "}
                  {t("Find it under Arrivals on the stay date.")}
                </p>
              </div>
            )}

            {/* Stay receipt for the desk: guest, room and dates in one place
                so staff can confirm without opening the reservation. */}
            <div className="space-y-3 border-t border-line-subtle pt-4 text-sm text-ink">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Guest name")}</span>
                <span className="font-medium">{form.guest_name || "-"}</span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Phone")}</span>
                <span className="font-medium">{form.phone || "-"}</span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Room")}</span>
                <span className="text-right font-medium">
                  {done.room
                    ? roomLabel(done)
                    : t("Unassigned")}
                  <span className="mt-0.5 block text-xs font-normal text-muted-ink">
                    {roomTypeName || form.room_type}
                    {moreRooms.length > 0
                      ? ` · ${1 + moreRooms.reduce((s, r) => s + r.count, 0)} ${t("Rooms")}`
                      : ""}
                  </span>
                </span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Check-in")}</span>
                <span className="font-medium">
                  {formatDate(form.check_in_date)}
                  {!dayUse && !walkIn && stayIn ? ` · ${stayIn}` : ""}
                </span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Check-out")}</span>
                <span className="font-medium">
                  {formatDate(checkOut)}
                  {!dayUse && !walkIn && stayOut ? ` · ${stayOut}` : ""}
                </span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">
                  {dayUse ? t("Day use") : t("Nights")}
                </span>
                <span className="font-medium">
                  {dayUse ? `${slot.from}-${slot.to}` : form.nights}
                </span>
              </div>
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-muted-ink">{t("Guests")}</span>
                <span className="font-medium">
                  {form.adults} {t("Adults")}
                  {form.children > 0
                    ? ` · ${form.children} ${t("Children")}`
                    : ""}
                </span>
              </div>
            </div>

            {done.deposit && !done.walkIn && !done.waitlist && (
              <DepositPanel
                reservation={done.ref}
                deposit={done.deposit}
                guestPhone={form.phone}
                onChanged={(dep) => {
                  setDone((cur) => (cur ? { ...cur, deposit: dep } : cur))
                  props.onBooked()
                }}
              />
            )}

            <div className="flex flex-wrap gap-2">
              {done.walkIn?.checked_in === 0 && !walkInCheckedIn && (
                <Button className="px-5 py-2.5 text-base" onClick={() => setGuidedCheckIn(done.ref)}>
                  {t("Finish check-in")}
                </Button>
              )}
              <Button
                variant={done.walkIn?.checked_in === 0 && !walkInCheckedIn ? "outline" : undefined}
                className="px-5 py-2.5 text-base"
                onClick={props.onClose}
              >
                {t("Done")}
              </Button>
              {done.walkIn?.folio && (
                <Button
                  variant="outline"
                  className="px-5 py-2.5 text-base"
                  onClick={() => {
                    navigate(`/billing/${done.walkIn!.folio}`)
                    props.onClose()
                  }}
                >
                  {t("Open bill")}
                </Button>
              )}
            </div>
          </div>
        ) : (
          <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
            {/* Form */}
            <div className="min-w-0 shrink-0 px-4 lg:min-h-0 lg:flex-1 lg:overflow-y-auto lg:px-6 py-5 md:px-8 md:py-6">
              <div className="w-full max-w-none space-y-6">
                {options?.property?.sell_message && (
                  <div className="flex items-start gap-2 rounded-xl bg-accent-subtle px-4 py-3 text-sm text-accent-ink">
                    <Megaphone
                      className="mt-0.5 size-4 shrink-0 text-accent-ink"
                      aria-hidden
                    />
                    <span>{options.property.sell_message}</span>
                  </div>
                )}

                <div className="grid gap-4 sm:grid-cols-2">
                  <Field required label={t("Guest name")}>
                    <div className="relative">
                      <input
                        className={inputCls}
                        aria-required="true"
                        {...target("guest")} value={form.guest_name}
                        onChange={(e) => {
                          setProfile(null)
                          set("guest_name", e.target.value)
                        }}
                        placeholder={t("Type to find or create")}
                      />
                      {hits.length > 0 && (
                        <ul className="absolute z-10 mt-1 w-full overflow-hidden rounded-xl border border-line bg-surface shadow-lg">
                          {hits.map((h) => (
                            <li key={h.name}>
                              <button
                                type="button"
                                className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-canvas"
                                onClick={() => {
                                  setProfile(h)
                                  setHits([])
                                  setForm((f) => ({
                                    ...f,
                                    guest_name: h.full_name,
                                    phone: h.phone ?? f.phone,
                                  }))
                                }}
                              >
                                <span className="font-medium">{h.full_name}</span>
                                {Boolean(h.blacklisted) && (
                                  <span className="rounded-full bg-danger-muted px-1.5 py-0.5 text-[10px] font-semibold uppercase text-danger-ink">
                                    {t("Blacklisted")}
                                  </span>
                                )}
                                {Boolean(h.vip) && (
                                  <Star
                                    className="size-3 fill-amber-400 text-amber-400"
                                    aria-label={t("VIP")}
                                  />
                                )}
                                <span className="ml-auto text-xs text-muted-ink">
                                  {h.phone ? `${h.phone} · ` : ""}
                                  {h.stays} {t("stay{s}", { s: h.stays === 1 ? "" : "s" })}
                                </span>
                              </button>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                    {profile && (
                      <span className="mt-1.5 inline-flex items-center gap-1.5 rounded-full bg-accent-subtle px-2.5 py-1 text-xs font-medium text-accent-ink">
                        {t("Returning guest · {n} stay{s}", {
                          n: profile.stays,
                          s: profile.stays === 1 ? "" : "s",
                        })}
                        <button
                          type="button"
                          aria-label={t("Detach profile")}
                          onClick={() => setProfile(null)}
                          className="text-accent-ink/60 hover:text-accent-ink"
                        >
                          <X className="size-3" aria-hidden />
                        </button>
                      </span>
                    )}
                  </Field>
                  <Field label={t("Phone")}>
                    <PhoneInput
                      id="booking-phone" value={form.phone}
                      country={options?.property?.country}
                      onChange={(v) => set("phone", v)}
                    />
                  </Field>
                </div>
                <Field label={t("Others staying (optional)")}>
                  <input
                    className="w-full rounded-lg border border-control bg-surface px-3 py-2 text-sm"
                    value={others}
                    placeholder={t("e.g. Ravi Sharma; Meera Sharma - their IDs are taken at check-in")}
                    onChange={(e) => setOthers(e.target.value)}
                  />
                </Field>

                {blacklisted && (
                  <div {...target("blacklist")} tabIndex={-1}
                    role="alert"
                    className="space-y-3 rounded-xl border-2 border-danger-line-strong bg-danger-subtle px-4 py-3 text-sm text-danger-ink"
                  >
                    <div className="flex items-start gap-2">
                      <ShieldAlert className="mt-0.5 size-5 shrink-0 text-danger-text" aria-hidden />
                      <div>
                        <p className="font-semibold">
                          {alert?.full_name
                            ? t("{name} is blacklisted", { name: alert.full_name })
                            : t("This guest is blacklisted")}
                        </p>
                        {alert?.blacklist_reason && (
                          <p className="mt-0.5">{alert.blacklist_reason}</p>
                        )}
                      </div>
                    </div>
                    {moreRooms.length > 0 ? (
                      <p>{t("A multi-room booking can't override a blacklist. Book one room, or remove the flag on the guest profile.")}</p>
                    ) : alert?.can_override ? (
                      <>
                        <label className="flex items-start gap-2">
                          <input
                            type="checkbox"
                            className="mt-0.5"
                            checked={blAck}
                            onChange={(e) => setBlAck(e.target.checked)}
                          />
                          <span>{t("I understand and want to book this guest anyway")}</span>
                        </label>
                        {blAck && (
                          <input
                            className={inputCls}
                            value={blReason}
                            onChange={(e) => setBlReason(e.target.value)}
                            placeholder={t("Reason for overriding the blacklist")}
                          />
                        )}
                      </>
                    ) : (
                      <p>{t("Only a Hotel Admin can book a blacklisted guest.")}</p>
                    )}
                  </div>
                )}

                {creditFails && credit && (
                  <div {...target("credit")} tabIndex={-1}
                    role="alert"
                    className={
                      credit.blocked
                        ? "space-y-2 rounded-xl border-2 border-danger-line-strong bg-danger-subtle px-4 py-3 text-sm text-danger-ink"
                        : "space-y-2 rounded-xl border border-warning-line-strong bg-warning-subtle px-4 py-3 text-sm text-warning-ink"
                    }
                  >
                    <div className="flex items-start gap-2">
                      <ShieldAlert className="mt-0.5 size-5 shrink-0" aria-hidden />
                      <div>
                        <p className="font-semibold">
                          {credit.blocked
                            ? t("{company}: company credit is blocked", { company: credit.company ?? "" })
                            : t("{company}: company credit warning", { company: credit.company ?? "" })}
                          {credit.unverified && (
                            <span className="ms-2 inline-flex items-center rounded-full bg-warning-soft/80 px-2 py-0.5 text-xs font-medium text-warning-ink">
                              {t("Not verified")}
                            </span>
                          )}
                        </p>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">
                          {credit.reasons.map((r) => (
                            <li key={r}>{translate(r)}</li>
                          ))}
                        </ul>
                      </div>
                    </div>
                    {credit.blocked &&
                      (moreRooms.length > 0 ? (
                        <p>{t("A multi-room booking can't override a credit block. Book one room, or have Finance raise the limit.")}</p>
                      ) : credit.can_override ? (
                        <input
                          className={inputCls}
                          value={creditReason}
                          maxLength={500}
                          onChange={(e) => setCreditReason(e.target.value)}
                          placeholder={t("Reason for the credit override (logged)")}
                        />
                      ) : (
                        <p>{t("Only Finance or a Hotel Admin can override it.")}</p>
                      ))}
                  </div>
                )}

                {walkIn && (
                  <div className="grid gap-3 sm:grid-cols-3">
                    <Field label={t("ID type")}>
                      <select
                        className={inputCls}
                        value={walk.id_type}
                        onChange={(e) => setW("id_type", e.target.value)}
                      >
                        <option value="">{t("Select")}</option>
                        {loc.id_types.map((x) => (
                          <option key={x} value={x}>
                            {t(x)}
                          </option>
                        ))}
                      </select>
                    </Field>
                    <Field label={t("ID number")}>
                      <input
                        className={inputCls}
                        value={walk.id_number}
                        onChange={(e) => setW("id_number", e.target.value)}
                        autoComplete="off"
                      />
                      {!!walk.id_number.trim() && !walk.id_type && (
                        <p className="mt-1.5 text-xs text-danger-text">
                          {t("Pick the ID type too.")}
                        </p>
                      )}
                    </Field>
                    <Field label={t("Nationality")}>
                      <input
                        className={inputCls}
                        value={walk.nationality}
                        onChange={(e) => setW("nationality", e.target.value)}
                      />
                    </Field>
                  </div>
                )}

                <div className={walkIn ? "grid gap-3 sm:grid-cols-2" : ""}>
                <Field required label={t("Room type")}>
                  <select
                    className={inputCls}
                    {...target("room")} value={form.room_type}
                    onChange={(e) => {
                      // a smaller room type leaves a party it cannot sleep
                      autoRoomType.current = false
                      const rt = options?.room_types.find(
                        (x) => x.name === e.target.value,
                      )
                      setForm((f) => ({
                        ...f,
                        room_type: e.target.value,
                        adults: clampGuests(f.adults, 1, sleeps(rt, "adults")),
                        children: clampGuests(
                          f.children,
                          0,
                          sleeps(rt, "children"),
                        ),
                        over_capacity_guests: Math.min(
                          f.over_capacity_guests,
                          Number(rt?.max_extra_beds || 0),
                        ),
                      }))
                    }}
                  >
                    {options?.room_types.map((rt) => (
                      <option key={rt.name} value={rt.name}>
                        {rt.room_type_name}
                        {!form.rate_plan && !planRequired && !quoteTariff && <> · {t("BAR")} {homeText(rt.base_price)}/{t("night")}</>}
                      </option>
                    ))}
                  </select>
                  {selectedRt &&
                    (selectedRt.adults_capacity > 0 ||
                      selectedRt.children_capacity > 0) && (
                      <p className="mt-1.5 text-xs text-muted-ink">
                        {t("Sleeps up to {guests}", {
                          guests: [
                            selectedRt.adults_capacity > 0 &&
                              tn(selectedRt.adults_capacity, "{n} adult", "{n} adults"),
                            selectedRt.children_capacity > 0 &&
                              tn(selectedRt.children_capacity, "{n} child", "{n} children"),
                          ]
                            .filter(Boolean)
                            .join(" · "),
                        })}
                      </p>
                    )}
                </Field>
                {walkIn && (
                  <Field label={t("Room")}>
                    <select
                      className={inputCls}
                      value={walk.room}
                      disabled={!freeRooms?.length}
                      onChange={(e) => setW("room", e.target.value)}
                    >
                      {freeRooms === null ? (
                        <option value="">{t("Loading…")}</option>
                      ) : freeRooms.length === 0 ? (
                        <option value="">{t("No free room tonight")}</option>
                      ) : (
                        freeRooms.map((r) => (
                          <option key={r.name} value={r.name}>
                            {r.room_number}
                            {r.housekeeping_status
                              ? ` · ${t(r.housekeeping_status)}`
                              : ""}
                          </option>
                        ))
                      )}
                    </select>
                    {freeRooms?.length === 0 && (
                      <p className="mt-1.5 text-xs text-danger-text">
                        {t("This type is full tonight - try another room type.")}
                      </p>
                    )}
                    {(() => {
                      const r = freeRooms?.find((x) => x.name === walk.room)
                      return r?.housekeeping_status &&
                        !["Clean", "Inspected"].includes(r.housekeeping_status) ? (
                        <p className="mt-1.5 text-xs text-warning-ink">
                          {t("{room} hasn't been cleaned yet - housekeeping will see the room flip to occupied.", {
                            room: r.room_number,
                          })}
                        </p>
                      ) : null
                    })()}
                  </Field>
                )}
                </div>

                <div
                  role="radiogroup"
                  aria-label={t("Stay type")}
                  className="inline-flex rounded-lg border border-line bg-canvas p-0.5 text-sm"
                >
                  {([false, true] as const).map((du) => (
                    <button
                      key={String(du)}
                      type="button"
                      role="radio"
                      aria-checked={dayUse === du}
                      onClick={() => {
                        setDayUse(du)
                        if (du) setMoreRooms([])
                      }}
                      className={
                        "rounded-md px-3 py-1.5 font-medium " +
                        (dayUse === du
                          ? "bg-surface text-accent-ink shadow-sm"
                          : "text-muted-ink hover:text-secondary-ink")
                      }
                    >
                      {du ? t("Day use") : t("Overnight")}
                    </button>
                  ))}
                </div>

                <div className={"grid grid-cols-2 gap-3 " + (dayUse ? "sm:grid-cols-4" : "sm:grid-cols-5")}>
                  <Field required={!walkIn} label={t("Check-in")}>
                    <input
                      type="date"
                      className={inputCls + (walkIn ? " bg-canvas text-muted-ink" : "")}
                      min={todayLocal()}
                      {...target("arrival")} value={form.check_in_date}
                      readOnly={walkIn}
                      title={(walkIn ? t("A walk-in arrives today") : undefined)}
                      onChange={(e) => {
                        setOutDraft(null)
                        set("check_in_date", e.target.value)
                      }}
                    />
                    {pastCheckIn && (
                      <p className="mt-1.5 text-xs text-danger-text">
                        {t("That date has already passed.")}
                      </p>
                    )}
                  </Field>
                  {dayUse ? (
                    /* Two time inputs need two columns - a single cell overflows
                       into Adults and looks like a double text box. */
                    <div className="col-span-2 min-w-0 sm:col-span-3">
                      <Field required label={t("Time slot")}>
                        <div className="flex min-w-0 items-center gap-1">
                          <input
                            type="time"
                            aria-label={t("From")}
                            className={inputCls + " min-w-0 flex-1 px-2"}
                            {...target("slot")} value={slot.from}
                            onChange={(e) => setSlot((x) => ({ ...x, from: e.target.value }))}
                          />
                          <span className="shrink-0 text-muted-ink" aria-hidden>
                            -
                          </span>
                          <input
                            type="time"
                            aria-label={t("To")}
                            className={inputCls + " min-w-0 flex-1 px-2"}
                            value={slot.to}
                            onChange={(e) => setSlot((x) => ({ ...x, to: e.target.value }))}
                          />
                        </div>
                        {badSlot && (
                          <p className="mt-1.5 text-xs text-danger-text">
                            {t("The slot must end after it starts.")}
                          </p>
                        )}
                      </Field>
                    </div>
                  ) : (
                    <>
                      <Field required label={t("Check-out")}>
                        <input
                          type="date"
                          className={inputCls}
                          min={shiftDate(form.check_in_date || todayLocal(), 1)}
                          {...target("departure")} value={outDraft ?? checkOut}
                          onChange={(e) => {
                            const v = e.target.value
                            // keep a half-typed or too-early date in the box (it
                            // blocks Confirm); only a date after check-in sets the nights
                            if (!parseDate(v) || badDate || v <= form.check_in_date) {
                              setOutDraft(v)
                              return
                            }
                            setOutDraft(null)
                            set("nights", Math.max(1, nightsBetween(form.check_in_date, v)))
                          }}
                        />
                        {outError && (
                          <p className="mt-1.5 text-xs text-danger-text">
                            {t("Check-out must be after check-in.")}
                          </p>
                        )}
                      </Field>
                      <Field required label={t("Nights")}>
                        <input
                          type="number"
                          min={1}
                          className={inputCls}
                          value={form.nights}
                          onChange={(e) => {
                            setOutDraft(null)
                            set("nights", Math.max(1, Number(e.target.value)))
                          }}
                        />
                      </Field>
                    </>
                  )}
                  <Field required label={t("Adults")}>
                    <input
                      type="number"
                      min={1}
                      max={adultsCap || undefined}
                      className={inputCls}
                      value={form.adults}
                      onChange={(e) =>
                        set(
                          "adults",
                          clampGuests(Number(e.target.value), 1, adultsCap),
                        )
                      }
                    />
                  </Field>
                  <Field label={t("Children")}>
                    <input
                      type="number"
                      min={0}
                      max={childrenCap || undefined}
                      className={inputCls}
                      value={form.children}
                      onChange={(e) =>
                        set(
                          "children",
                          clampGuests(Number(e.target.value), 0, childrenCap),
                        )
                      }
                    />
                  </Field>
                </div>

                {!dayUse && !walkIn && (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Field label={t("Arrival time")}>
                      <input
                        type="time"
                        className={inputCls}
                        value={stayIn}
                        onChange={(e) => setStayTimes((x) => ({ ...x, from: e.target.value }))}
                      />
                    </Field>
                    <Field label={t("Departure time")}>
                      <input
                        type="time"
                        className={inputCls}
                        value={stayOut}
                        onChange={(e) => setStayTimes((x) => ({ ...x, to: e.target.value }))}
                      />
                    </Field>
                    <p className="col-span-2 self-end pb-2 text-xs text-muted-ink">
                      {t("Standard times from the property. Change them for this booking if the guest agreed otherwise.")}
                    </p>
                  </div>
                )}

                {options?.property.pets_allowed ? (
                  <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                    <Field label={t("Pets")}>
                      <input
                        type="number"
                        min={0}
                        className={inputCls}
                        value={form.pets}
                        onChange={(e) =>
                          set("pets", Math.max(0, Number(e.target.value)))
                        }
                      />
                    </Field>
                    {options.property.pet_charge > 0 && (
                      <p className="col-span-1 self-end pb-2 text-xs text-muted-ink sm:col-span-3">
                        {homeText(options.property.pet_charge)}{" "}
                        {t(petChargeBasis(options.property.pet_charge_basis))}
                      </p>
                    )}
                  </div>
                ) : null}

                {selectedRt && extraBeds > 0 && (
                  <div
                    className={
                      "space-y-2 rounded-xl border px-3 py-2.5 text-sm " +
                      (form.over_capacity_guests > 0
                        ? "border-warning-line bg-warning-subtle text-warning-ink"
                        : "border-line text-secondary-ink")
                    }
                  >
                    <div>
                      {t("Extra guests by arrangement - up to {beds} above the {cap} it sleeps, at a price you agree", {
                        beds: extraBeds,
                        cap: selectedRt.adults_capacity,
                      })}
                    </div>
                    <div className="grid gap-2 sm:grid-cols-2">
                      <label className="block">
                        <span className="mb-1 block text-xs font-medium">
                          {t("Extra guests")}
                        </span>
                        <input
                          type="number"
                          min={0}
                          max={extraBeds}
                          className={inputCls}
                          value={form.over_capacity_guests}
                          onChange={(e) =>
                            set(
                              "over_capacity_guests",
                              clampGuests(Number(e.target.value), 0, extraBeds),
                            )
                          }
                        />
                      </label>
                      {form.over_capacity_guests > 0 && (
                        <label className="block">
                          <span className="mb-1 block text-xs font-medium">
                            {t("Agreed charge / night, all of them")}
                          </span>
                          <input
                            type="number"
                            min={0}
                            step={moneyStep()}
                            inputMode="decimal"
                            className={inputCls}
                            value={form.over_capacity_charge}
                            onChange={(e) =>
                              set("over_capacity_charge", Math.max(0, Number(e.target.value)))
                            }
                            placeholder={t("What you agreed for them")}
                          />
                        </label>
                      )}
                    </div>
                    {form.over_capacity_guests > 0 && (
                      <input
                        className={inputCls}
                        {...target("extra")} value={form.over_capacity_reason}
                        onChange={(e) =>
                          set("over_capacity_reason", e.target.value)
                        }
                        placeholder={t("Who agreed it, and why (required)")}
                      />
                    )}
                  </div>
                )}

                <div className="grid gap-3 sm:grid-cols-2">
                  <Field label={t("Meal plan")}>
                    <select
                      className={inputCls}
                      value={form.meal_plan}
                      onChange={(e) => {
                        mealPlanTouched.current = true
                        set("meal_plan", e.target.value)
                      }}
                    >
                      <option value="">{t("Room only")}</option>
                      {options?.meal_plans.map((mp) => (
                        <option key={mp.name} value={mp.name}>
                          {translate(mp.label)} (+{homeText(mp.price_per_adult)}{translate("/adult)")}</option>
                      ))}
                    </select>
                  </Field>
                  {(planRequired || (options?.rate_plans.length ?? 0) > 0) && (
                    <Field label={t("Rate plan")}>
                      <select
                        className={inputCls}
                        {...target("rate-plan")}
                        value={form.rate_plan}
                        onChange={(e) => {
                          ratePlanTouched.current = true
                          set("rate_plan", e.target.value)
                        }}
                      >
                        <option value="" disabled={planRequired && !companyPlan} hidden={planRequired && !!form.rate_plan}>
                          {companyPlan
                            ? `${t("Company rate")}: ${companyPlan.rate_plan_name}`
                            : planRequired ? t("Choose a rate plan") : t("Standard rate (no plan)")}
                        </option>
                        {options?.rate_plans.map((rp) => (
                          <option key={rp.name} value={rp.name}>
                            {rp.rate_plan_name}
                            {rp.code ? ` (${rp.code})` : ""}
                          </option>
                        ))}
                      </select>
                    </Field>
                  )}
                  <Field label={t("Voucher")}>
                    <input
                      className={inputCls}
                      value={form.voucher_code}
                      onChange={(e) =>
                        set("voucher_code", e.target.value.toUpperCase())
                      }
                      placeholder={t("Optional code")}
                    />
                  </Field>
                </div>

                {compAsks && compGov && (
                  <div id="booking-comp" tabIndex={-1}>
                    <SupervisorSignOff
                      reason={t("A {category} rate plan needs a manager's sign-off.",
                        { category: t(compGov.rate_category) })}
                      supervisors={compGov.supervisors} supervisor={compSupervisor} pin={compPin}
                      onSupervisor={setCompSupervisor} onPin={setCompPin} />
                  </div>
                )}

                {canDiscount && (
                  <div id="booking-discount" tabIndex={-1}>
                    <DiscountPanel value={discount} onChange={setDiscount}
                      reasons={options?.discount_reasons ?? []} eligible={discountAllowed}
                      governance={discountGov}
                      rateCurrency={isForeignRate ? rateCurrency : undefined}
                      homeCurrency={loc.currency}
                      fxRate={rateFx}
                      nights={dayUse || !quote ? undefined : discountNights(quote)}
                      supervisor={discountSupervisor} pin={discountPin}
                      onSupervisor={setDiscountSupervisor} onPin={setDiscountPin} money={anyMoneyText}>
                      {canFixRate && (
                        <label className="flex items-center gap-2 text-xs text-secondary-ink">
                          <input type="checkbox" aria-label={t("Fixed rate override")} checked={fixedOverride}
                            onChange={(e) => setFixedOverride(e.target.checked)} />
                          {t("Fixed rate override")}
                        </label>
                      )}
                    </DiscountPanel>
                  </div>
                )}

                {form.room_type && (
                  <RateOptions
                    roomType={form.room_type}
                    checkIn={form.check_in_date}
                    checkOut={checkOut}
                    adults={form.adults}
                    children={form.children}
                    extraBeds={extra.extra_beds}
                    mealPlan={form.meal_plan || undefined}
                    voucher={form.voucher_code || undefined}
                    ratePlan={form.rate_plan}
                    company={form.company || undefined}
                    planRequired={planRequired}
                    onPickPlan={(p) => {
                      ratePlanTouched.current = true
                      set("rate_plan", p)
                    }}
                    nightPrices={nightPrices}
                    onNightPrices={setNightPrices}
                    canSetPrice={canSetPrice}
                    lockedNote={
                      options?.can_set_rate && otaTotal > 0
                        ? t("The OTA total sets the price, so nights cannot be priced one by one.")
                        : undefined
                    }
                  />
                )}

                {walkIn && (
                  <div className="space-y-3 rounded-xl border border-line p-4">
                    <h3 className="text-sm font-semibold text-ink">
                      {t("Payment")}
                    </h3>
                    <TenderPicker home={loc.currency} chosen={tender} modes={modes} rates={settlementRates}
                      onChange={({ mode, currency }) => {
                        setRequestedTender({ key: rateCurKey, currency })
                        setW("pay_mode", mode)
                        if (currency !== payCurrency) setManualPayment(null)
                      }} />
                    <div className="grid gap-3 sm:grid-cols-2">
                      <div>
                        <p className="mb-1 text-xs font-medium">{t("Amount collected")}</p>
                        <TenderInput currency={payCurrency} value={paymentInput} full={suggestedAmount}
                          onChange={(amount) => setManualPayment({ key: rateCurKey, currency: payCurrency, amount })}
                          onFull={() => setManualPayment(null)} />
                        <p className="mt-1.5 text-xs text-muted-ink">
                          {foreign ? null : payAmount <= 0
                            ? t("Nothing collected now - the full amount stays on the bill.")
                            : payAmount < grandTotal
                              ? t("{amt} left on the bill.", {
                                  amt: homeText(grandTotal - payAmount),
                                })
                              : t("Paid in full.")}
                        </p>
                      </div>
                      {tender.mode && tender.mode !== "Cash" && (
                        <Field label={t("Reference")}>
                          <input
                            className={inputCls}
                            value={walk.pay_reference}
                            onChange={(e) => setW("pay_reference", e.target.value)}
                            placeholder={t("Card slip / transfer ref (optional)")}
                          />
                        </Field>
                      )}
                    </div>
                    {foreign && <SettlementPreview result={fx} amount={payAmount} balance={grandTotal}/>}
                  </div>
                )}

                {!walkIn && (<>
                {moreRooms.length > 0 && (
                  <Field label={t("Group name")}>
                    <input
                      className={inputCls}
                      value={groupName}
                      onChange={(e) => setGroupName(e.target.value)}
                      placeholder={t("e.g. Infosys offsite (optional)")}
                    />
                  </Field>
                )}
                {moreRooms.map((r, i) => {
                  const rt = options?.room_types.find(
                    (o) => o.name === r.room_type,
                  )
                  return (
                  <div
                    key={i}
                    className="flex flex-wrap items-end gap-2 rounded-xl border border-line bg-canvas/50 px-3 py-2.5"
                  >
                    <span className="w-full text-xs font-medium uppercase tracking-wider text-muted-ink">{translate("Room ")}{i + 2}
                    </span>
                    <select
                      className={`${inputCls} !w-auto flex-1`}
                      aria-label={translate("Room {v0} type", { v0: String(i + 2) })}
                      value={r.room_type}
                      onChange={(e) =>
                        setMoreRooms((rs) =>
                          rs.map((x, j) =>
                            j === i ? { ...x, room_type: e.target.value } : x,
                          ),
                        )
                      }
                    >
                      {options?.room_types.map((rt) => (
                        <option key={rt.name} value={rt.name}>
                          {rt.room_type_name}
                        </option>
                      ))}
                    </select>
                    <label className="flex items-center gap-1 text-xs text-muted-ink">
                      {t("Qty")}
                      <input
                        type="number"
                        min={1}
                        max={50}
                        aria-label={translate("Room {v0} quantity", { v0: String(i + 2) })}
                        className={`${inputCls} !w-16`}
                        value={r.count}
                        onChange={(e) =>
                          setMoreRooms((rs) =>
                            rs.map((x, j) =>
                              j === i
                                ? {
                                    ...x,
                                    count: Math.min(
                                      50,
                                      Math.max(1, Math.floor(Number(e.target.value) || 1)),
                                    ),
                                  }
                                : x,
                            ),
                          )
                        }
                      />
                    </label>
                    {r.count === 1 && (
                      <input
                        className={`${inputCls} !w-auto min-w-36 flex-1`}
                        aria-label={translate("Room {v0} guest name", { v0: String(i + 2) })}
                        placeholder={t("Guest name (optional)")}
                        value={r.guest_name}
                        onChange={(e) =>
                          setMoreRooms((rs) =>
                            rs.map((x, j) =>
                              j === i ? { ...x, guest_name: e.target.value } : x,
                            ),
                          )
                        }
                      />
                    )}
                    <input
                      type="number"
                      min={1}
                      max={sleeps(rt, "adults") || undefined}
                      aria-label={translate("Room {v0} adults", { v0: String(i + 2) })}
                      className={`${inputCls} !w-16`}
                      value={r.adults}
                      onChange={(e) =>
                        setMoreRooms((rs) =>
                          rs.map((x, j) =>
                            j === i
                              ? {
                                  ...x,
                                  adults: clampGuests(
                                    Number(e.target.value), 1,
                                    sleeps(rt, "adults"),
                                  ),
                                }
                              : x,
                          ),
                        )
                      }
                    />
                    <select
                      className={`${inputCls} !w-auto`}
                      aria-label={translate("Room {v0} meal plan", { v0: String(i + 2) })}
                      value={r.meal_plan}
                      onChange={(e) =>
                        setMoreRooms((rs) =>
                          rs.map((x, j) =>
                            j === i ? { ...x, meal_plan: e.target.value } : x,
                          ),
                        )
                      }
                    >
                      <option value="">{translate("Room only")}</option>
                      {options?.meal_plans.map((mp) => (
                        <option key={mp.name} value={mp.name}>
                          {translate(mp.label)}
                        </option>
                      ))}
                    </select>
                    <button
                      className="rounded p-1.5 text-muted-ink hover:text-danger-text"
                      aria-label={translate("Remove room {v0}", { v0: String(i + 2) })}
                      onClick={() =>
                        setMoreRooms((rs) => rs.filter((_, j) => j !== i))
                      }
                    >
                      <Trash2 className="size-4" aria-hidden />
                    </button>
                  </div>
                  )
                })}

                {!dayUse && <button
                  type="button"
                  className="inline-flex items-center gap-1.5 rounded-lg border border-dashed border-control px-3 py-2 text-sm font-medium text-secondary-ink hover:border-accent-control hover:bg-accent-subtle/40 hover:text-accent-ink"
                  onClick={() =>
                    setMoreRooms((rs) => [
                      ...rs,
                      {
                        room_type: form.room_type,
                        adults: 2,
                        children: 0,
                        meal_plan: form.meal_plan,
                        guest_name: "",
                        count: 1,
                      },
                    ])
                  }
                >
                  <Plus className="size-4" aria-hidden />
                  {t("Add another room")}
                </button>}
                {!dayUse && !walkIn && (
                  <button
                    type="button"
                    className="ml-3 text-sm font-medium text-accent-ink hover:underline"
                    onClick={() => {
                      props.onClose()
                      navigate("/groups?new=1")
                    }}
                  >
                    {t("Group booking with a main room")}
                  </button>
                )}

                <div className="grid gap-3 sm:grid-cols-3">
                  <Field label={translate("Booked via")}>
                    <select
                      className={inputCls}
                      value={via.site}
                      onChange={(e) =>
                        setVia({
                          ...via,
                          site: e.target.value,
                          pct: String(options?.ota_commission?.[e.target.value] ?? ""),
                        })
                      }
                    >
                      <option value="">{translate("Direct")}</option>
                      {options?.ota_channels?.map((c) => (
                        <option key={c} value={c}>{c}</option>
                      ))}
                      <option value="other">{translate("Other site…")}</option>
                    </select>
                  </Field>
                  {via.site === "other" && (
                    <Field label={translate("Site name")}>
                      <input
                        className={inputCls}
                        value={via.other}
                        onChange={(e) => setVia({ ...via, other: e.target.value })}
                      />
                    </Field>
                  )}
                  {via.site && (
                    <>
                      <Field label={translate("OTA booking ref")}>
                        <input
                          className={inputCls}
                          value={via.ref}
                          onChange={(e) => setVia({ ...via, ref: e.target.value })}
                        />
                      </Field>
                      <Field
                        label={
                          moreRooms.length > 0
                            ? translate("Sold for, all rooms (incl. tax)")
                            : translate("Sold for (incl. tax)")
                        }
                      >
                        <input
                          className={inputCls}
                          inputMode="decimal"
                          placeholder={translate("Rate card price")}
                          value={via.total}
                          onChange={(e) => setVia({ ...via, total: e.target.value })}
                        />
                      </Field>
                      <Field label={translate("Commission %")}>
                        <input
                          className={inputCls}
                          inputMode="decimal"
                          value={via.pct}
                          onChange={(e) => setVia({ ...via, pct: e.target.value })}
                        />
                      </Field>
                    </>
                  )}
                </div>

                <div className="border-t border-line-subtle pt-2">
                  <button
                    type="button"
                    className="flex w-full items-center justify-between py-2 text-left text-sm font-medium text-secondary-ink"
                    onClick={() => setMoreOpen((o) => !o)}
                    aria-expanded={moreOpen}
                  >
                    <span>{t("Company, add-ons & arrival details")}</span>
                    <ChevronDown
                      className={
                        "size-4 text-muted-ink transition-transform " +
                        (moreOpen ? "rotate-180" : "")
                      }
                      aria-hidden
                    />
                  </button>

                  {moreOpen && (
                    <div className="space-y-5 pb-2 pt-1">
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field label={translate("Company (bill corporate)")}>
                          <select
                            className={inputCls}
                            value={form.company}
                            onChange={(e) => set("company", e.target.value)}
                          >
                            <option value="">-</option>
                            {options?.companies.map((c) => (
                              <option key={c.name} value={c.name}>
                                {c.company_name}
                              </option>
                            ))}
                          </select>
                        </Field>
                        <Field label={translate("Travel agent")}>
                          <select
                            className={inputCls}
                            value={form.travel_agent}
                            onChange={(e) =>
                              set("travel_agent", e.target.value)
                            }
                          >
                            <option value="">-</option>
                            {options?.travel_agents.map((t) => (
                              <option key={t.name} value={t.name}>
                                {t.agent_name} ({t.commission_pct}%)
                              </option>
                            ))}
                          </select>
                        </Field>
                        <Field label={translate("Market segment")}>
                          <select
                            className={inputCls}
                            value={form.market_segment}
                            onChange={(e) => set("market_segment", e.target.value)}
                          >
                            <option value="">{form.company ? translate("Corporate") : "-"}</option>
                            {(loc.market_codes ?? []).map((m) => (
                              <option key={m.value} value={m.value}>{translate(m.label)}</option>
                            ))}
                          </select>
                        </Field>
                        <Field label={t("Booking type")}>
                          <select
                            className={inputCls}
                            value={form.booking_type}
                            onChange={(e) => set("booking_type", e.target.value)}
                          >
                            <option value="">{form.company ? t("Corporate") : "-"}</option>
                            {(loc.booking_types ?? []).map((bt) => (
                              <option key={bt.value} value={bt.value}>{translate(bt.label)}</option>
                            ))}
                          </select>
                        </Field>
                      </div>

                      {moreRooms.length === 0 &&
                        (options?.experiences.length ?? 0) > 0 && (
                          <div>
                            <span className="mb-1.5 block text-sm font-medium text-secondary-ink">{translate("Add-ons")}</span>
                            <div className="flex flex-wrap gap-1.5">
                              {options?.experiences.map((x) => {
                                const on = (addonQty[x.name] ?? 0) > 0
                                return (
                                  <button
                                    key={x.name}
                                    type="button"
                                    aria-pressed={on}
                                    className={
                                      on
                                        ? "rounded-full bg-accent-fill px-3 py-1.5 text-sm font-medium text-on-solid"
                                        : "rounded-full border border-control px-3 py-1.5 text-sm text-secondary-ink hover:border-accent-control"
                                    }
                                    onClick={() =>
                                      setAddonQty((q) => ({
                                        ...q,
                                        [x.name]: on ? 0 : 1,
                                      }))
                                    }
                                  >
                                    {x.experience_name} · {homeText(x.price)}
                                  </button>
                                )
                              })}
                            </div>
                          </div>
                        )}

                      {moreRooms.length > 0 && (
                        <p className="text-xs text-muted-ink">{translate("Multi-room bookings are created as a group - vouchers, add-ons and booker details can be added per stay afterwards.")}</p>
                      )}

                      {moreRooms.length === 0 && (
                        <>
                          <label className="flex items-center gap-2 text-sm font-medium text-secondary-ink">
                            <input
                              type="checkbox"
                              className="size-4 accent-focus"
                              checked={offers}
                              onChange={(e) => setOffers(e.target.checked)}
                            />{translate("Guest agrees to offers on WhatsApp")}</label>
                          <label className="flex items-center gap-2 text-sm font-medium text-secondary-ink">
                            <input
                              type="checkbox"
                              className="size-4 accent-focus"
                              checked={onBehalf}
                              onChange={(e) => setOnBehalf(e.target.checked)}
                            />{translate("Booked on someone's behalf")}</label>
                          {onBehalf && (
                            <div className="space-y-3">
                              <div className="grid gap-3 sm:grid-cols-2">
                                <Field label={translate("Booker name")}>
                                  <input
                                    className={inputCls}
                                    value={form.booked_by_name}
                                    onChange={(e) =>
                                      set("booked_by_name", e.target.value)
                                    }
                                    placeholder={translate("Who arranged this stay")}
                                  />
                                </Field>
                                <Field label={translate("Booker phone")}>
                                  <PhoneInput
                                    id="booking-booker-phone" value={form.booked_by_phone}
                                    country={options?.property?.country}
                                    onChange={(v) =>
                                      set("booked_by_phone", v)
                                    }
                                  />
                                </Field>
                              </div>
                              <div className="grid gap-3 sm:grid-cols-2">
                                <Field label={translate("Relation")}>
                                  <select
                                    className={inputCls}
                                    value={form.booker_relation}
                                    onChange={(e) =>
                                      set("booker_relation", e.target.value)
                                    }
                                  >
                                    <option value="">-</option>
                                    {(loc.booker_relations ?? []).map((r) => (
                                      <option key={r.value} value={r.value}>{translate(r.label)}</option>
                                    ))}
                                  </select>
                                </Field>
                                <Field label={translate("Send links & updates to")}>
                                  <select
                                    className={inputCls}
                                    value={form.contact_preference}
                                    onChange={(e) =>
                                      set("contact_preference", e.target.value)
                                    }
                                  >
                                    <option value="Booker">{translate("Booker")}</option>
                                    <option value="Guest">{translate("Guest")}</option>
                                    <option value="Both">{translate("Both")}</option>
                                  </select>
                                </Field>
                              </div>
                            </div>
                          )}

                          <Field label={translate("Special requests")}>
                            <textarea
                              className={inputCls}
                              rows={2}
                              placeholder={translate("e.g. prayer mat, high floor, allergy notes")}
                              value={extra.special_requests}
                              onChange={(e) =>
                                setX("special_requests", e.target.value)
                              }
                            />
                          </Field>
                          <div className="flex flex-wrap gap-x-4 gap-y-2 text-sm">
                            {(
                              [
                                ["pickup_required", "Airport pickup"],
                                ["valet_parking", "Valet parking"],
                                ["early_checkin", "Early check-in"],
                                ["late_checkout", "Late check-out"],
                              ] as const
                            ).map(([k, lbl]) => (
                              <label
                                key={k}
                                className="flex items-center gap-1.5 font-medium text-secondary-ink"
                              >
                                <input
                                  type="checkbox"
                                  checked={extra[k]}
                                  onChange={(e) => setX(k, e.target.checked)}
                                />
                                {translate(lbl)}
                              </label>
                            ))}
                          </div>
                          {extra.pickup_required && (
                            <div className="grid gap-3 sm:grid-cols-2">
                              <Field label={translate("Arrival mode")}>
                                <select
                                  className={inputCls}
                                  value={extra.arrival_mode}
                                  onChange={(e) =>
                                    setX("arrival_mode", e.target.value)
                                  }
                                >
                                  <option value=""> - </option>
                                  <option value={"Air"}>{translate("Air")}</option>
                                  <option value={"Road"}>{translate("Road")}</option>
                                  <option value={"Rail"}>{translate("Rail")}</option>
                                  <option value={"Sea"}>{translate("Sea")}</option>
                                  <option value={"Own vehicle"}>{translate("Own vehicle")}</option>
                                </select>
                              </Field>
                              <Field label={translate("Flight / train no.")}>
                                <input
                                  className={inputCls}
                                  value={extra.arrival_ref}
                                  onChange={(e) =>
                                    setX("arrival_ref", e.target.value)
                                  }
                                />
                              </Field>
                              <Field label={translate("Arrival time")}>
                                <input
                                  type="datetime-local"
                                  className={inputCls}
                                  value={extra.arrival_datetime}
                                  onChange={(e) =>
                                    setX("arrival_datetime", e.target.value)
                                  }
                                />
                              </Field>
                              <Field label={translate("Extra beds")}>
                                <input
                                  type="number"
                                  min={0}
                                  className={inputCls}
                                  value={extra.extra_beds}
                                  onChange={(e) =>
                                    setX("extra_beds", Number(e.target.value))
                                  }
                                />
                              </Field>
                            </div>
                          )}
                          <div className="grid gap-3 sm:grid-cols-2">
                            <Field label={translate("Purpose of visit")}>
                              <select
                                className={inputCls}
                                value={extra.purpose}
                                onChange={(e) =>
                                  setX("purpose", e.target.value)
                                }
                              >
                                <option value=""> - </option>
                                <option value={"Leisure"}>{translate("Leisure")}</option>
                                <option value={"Business"}>{translate("Business")}</option>
                                <option value={"Event / Wedding"}>{translate("Event / Wedding")}</option>
                                <option value={"Medical"}>{translate("Medical")}</option>
                                <option value={"Pilgrimage"}>{translate("Pilgrimage")}</option>
                                <option value={"Crew"}>{translate("Crew")}</option>
                                <option value={"Other"}>{translate("Other")}</option>
                              </select>
                            </Field>
                            <Field label={translate("Guest category")}>
                              <select
                                className={inputCls}
                                value={extra.guest_category}
                                onChange={(e) =>
                                  setX("guest_category", e.target.value)
                                }
                              >
                                <option value="">{translate("Standard")}</option>
                                <option value={"VIP"}>{translate("VIP")}</option>
                                <option value={"Corporate"}>{translate("Corporate")}</option>
                                <option value={"Complimentary"}>{translate("Complimentary")}</option>
                                <option value={"Crew"}>{translate("Crew")}</option>
                              </select>
                            </Field>
                          </div>
                        </>
                      )}
                    </div>
                  )}
                </div>
                </>)}
              </div>
            </div>

            {/* Quote rail - always visible on lg */}
            <aside className="flex w-full shrink-0 flex-col border-t border-line bg-canvas lg:w-80 lg:border-l lg:border-t-0 xl:w-[22rem]">
              <div className="flex-1 overflow-y-auto px-6 py-5 md:px-7">
                <div className="mb-4 flex items-center justify-between">
                  <h3 className="text-xs font-semibold uppercase tracking-wider text-muted-ink">
                    {t("Quote")}
                  </h3>
                  {quoting && (
                    <Loader2
                      className="size-4 animate-spin text-muted-ink"
                      aria-label={t("Updating quote")}
                    />
                  )}
                </div>

                {quote ? (
                  <div className="space-y-2.5 text-[15px]">
                    <div className="flex justify-between gap-3 text-secondary-ink">
                      <span className="min-w-0 truncate">
                        {roomTypeName} ·{" "}
                        {quote.day_use
                          ? t("Day use {from}-{to}", { from: slot.from, to: slot.to })
                          : translate("{v0} night{v1}", { v0: String(quote.nights), v1: String(quote.nights === 1 ? "" : "s") })}
                      </span>
                      {!quoteTariff && !(quote.reservation_discount_total ?? 0) && <span className="shrink-0 tabular-nums">
                        {homeText(quote.room_total)}
                      </span>}
                    </div>
                    {/* label wraps cleanly so the full reason is visible;
                        the amount keeps its width */}
                    {(quote.reservation_discount_total ?? 0) > 0 && (<>
                      <div className="flex items-start justify-between gap-3 text-secondary-ink">
                        <span>{t("Base room rate")}</span>
                        <span className="shrink-0 whitespace-nowrap text-end tabular-nums">
                          {contractSummary ? (
                            <>
                              <span className="block font-medium text-ink">
                                {currencyText(contractSummary.currency, contractSummary.room_base_total, moneyLoc)}
                              </span>
                              <span className="block text-xs text-muted-ink">
                                ≈ {homeText(quote.room_base_total ?? quote.room_total)}
                              </span>
                            </>
                          ) : homeText(quote.room_base_total ?? quote.room_total)}
                        </span>
                      </div>
                      {(() => {
                        const discountLabel = discountReasonText
                          ? t("Discount ({reason})", { reason: discountReasonText })
                          : t("Discount")
                        return (
                          <div className="flex items-start justify-between gap-3 font-medium text-success-ink">
                            <span className="min-w-0 break-words" title={discountLabel}>
                              {discountLabel}
                            </span>
                            <span className="shrink-0 whitespace-nowrap text-end tabular-nums">
                              {contractSummary ? (
                                <>
                                  <span className="block">
                                    −{currencyText(contractSummary.currency, contractSummary.room_discount_total, moneyLoc)}
                                  </span>
                                  <span className="block text-xs font-normal text-success-ink/80">
                                    ≈ −{homeText(quote.reservation_discount_total ?? 0)}
                                  </span>
                                </>
                              ) : `−${homeText(quote.reservation_discount_total ?? 0)}`}
                            </span>
                          </div>
                        )
                      })()}
                      <div className="flex items-start justify-between gap-3 text-secondary-ink">
                        <span>{t("Net room rate")}</span>
                        {/* base less discount, on the basis both are entered
                            (gross where rates include tax) */}
                        <span className="shrink-0 whitespace-nowrap text-end tabular-nums">
                          {contractSummary ? (
                            <>
                              <span className="block font-semibold text-ink">
                                {currencyText(contractSummary.currency, contractSummary.room_net_total, moneyLoc)}
                              </span>
                              <span className="block text-xs text-muted-ink">
                                ≈ {homeText((quote.room_base_total ?? quote.room_total) - (quote.reservation_discount_total ?? 0))}
                              </span>
                            </>
                          ) : homeText(
                            (quote.room_base_total ?? quote.room_total) - (quote.reservation_discount_total ?? 0))}
                        </span>
                      </div>
                      {contractSummary && (
                        <p className="text-xs tabular-nums text-muted-ink">
                          {t("1 {cur} = {rate} {home}", {
                            cur: contractSummary.currency,
                            rate: formatFxRate(contractSummary.fx_rate, moneyLoc),
                            home: loc.currency,
                          })}
                          {contractSummary.fx_lock_policy ? ` · ${fxLockNote(contractSummary.fx_lock_policy)}` : ""}
                        </p>
                      )}
                    </>)}
                    {/* a discounted tariff shows base, discount and net above,
                        never the undiscounted tariff total beside them */}
                    {quoteTariff && !(quote.reservation_discount_total ?? 0) && <div className="space-y-1 text-sm">
                      <p><TariffRoomTotal tariff={quoteTariff} nights={quote.nights}
                        agreedRate={quote.agreed_rate ?? 0} locale={moneyLoc} /></p>
                    </div>}
                    {(quote.over_capacity_total ?? 0) > 0 && (
                      <div className="flex justify-between text-secondary-ink">
                        <span>{t("Extra guests")}</span>
                        <span className="tabular-nums">
                          {homeText(quote.over_capacity_total ?? 0)}
                        </span>
                      </div>
                    )}
                    {quote.meal_total > 0 && (
                      <div className="flex justify-between text-secondary-ink">
                        <span>{t("Meals")}</span>
                        <span className="tabular-nums">
                          {homeText(quote.meal_total)}
                        </span>
                      </div>
                    )}
                    {(quote.pet_charge || 0) > 0 && (
                      <div className="flex justify-between text-secondary-ink">
                        <span>
                          {t("Pets")} × {form.pets}
                        </span>
                        <span className="tabular-nums">
                          {homeText(quote.pet_charge || 0)}
                        </span>
                      </div>
                    )}
                    {quote.long_stay && (
                      <div className="flex justify-between font-medium text-success-ink">
                        <span>{t("Long stay {pct}% off (in the room price)", { pct: String(quote.long_stay.percent) })}</span>
                        <span className="tabular-nums">
                          −{homeText(quote.long_stay.amount)}
                        </span>
                      </div>
                    )}
                    {(quote.voucher_discount ?? quote.discount) > 0 && (
                      <div className="flex justify-between font-medium text-success-ink">
                        <span>{t("Voucher")}</span>
                        <span className="tabular-nums">
                          −{homeText(quote.voucher_discount ?? quote.discount)}
                        </span>
                      </div>
                    )}
                    {isTaxUndetermined(quoteTaxBasis) ? (
                      <p
                        role="status"
                        className="rounded-lg border border-warning-line bg-warning-subtle px-2.5 py-2 text-xs text-warning-ink"
                      >
                        {t(TAX_NOT_SET_UP_MSG)}
                      </p>
                    ) : quoteTaxBasis?.inclusive ? <p className="text-xs text-secondary-ink">
                      {t("Includes {tax} (≈ {amount} on the room). The invoice shows the exact tax.", {
                        tax: quoteTaxBasis.label,
                        amount: currencyText(quoteTariff?.property_currency || loc.currency,
                          quote.room_total * quoteTaxBasis.rate / (100 + quoteTaxBasis.rate), loc.locale),
                      })}
                    </p> : <div className="flex justify-between text-secondary-ink">
                      <span>
                        {loc.tax_label} {quote.tax_percent}%
                      </span>
                      <span className="tabular-nums">
                        {homeText(quote.tax_amount)}
                      </span>
                    </div>}
                    {moreQuotes.map((mq, i) =>
                      mq ? (
                        <div
                          key={i}
                          className="flex justify-between gap-3 text-secondary-ink"
                        >
                          <span className="min-w-0 truncate">{translate("Room ")}{i + 2} ·{" "}
                            {options?.room_types.find(
                              (rt) => rt.name === moreRooms[i]?.room_type,
                            )?.room_type_name ?? ""}
                            {(moreRooms[i]?.count ?? 1) > 1
                              ? ` × ${moreRooms[i].count}`
                              : ""}
                          </span>
                          <span className="shrink-0 tabular-nums">
                            {homeText(mq.amount_after_tax * (moreRooms[i]?.count ?? 1))}
                          </span>
                        </div>
                      ) : null,
                    )}
                    {addonsGross > 0 && (
                      <div className="flex justify-between text-secondary-ink">
                        <span>
                          {t("Add-ons (incl. {tax})", { tax: loc.tax_label })}
                        </span>
                        <span className="tabular-nums">
                          {homeText(addonsGross)}
                        </span>
                      </div>
                    )}

                    <div className="mt-3 rounded-xl border border-line bg-surface px-4 py-3.5 shadow-sm">
                      <div className="flex items-baseline justify-between gap-2">
                        <span>{t("Total")}{moreRooms.length > 0 ? t(" · all rooms") : ""}</span>
                        <span className="text-end">
                          <span className="block text-3xl font-semibold tabular-nums tracking-tight text-ink">
                            {pureContractStay && contractSummary
                              ? currencyText(contractSummary.currency, contractSummary.room_after_tax_total, moneyLoc)
                              : quoteTariff
                                ? currencyText(quoteTariff.property_currency, grandTotal, moneyLoc)
                                : homeText(grandTotal)}
                          </span>
                          {pureContractStay && contractSummary && (
                            <span className="mt-0.5 block text-xs tabular-nums text-muted-ink">
                              ≈ {currencyText(quoteTariff?.property_currency || loc.currency, grandTotal, moneyLoc)}
                            </span>
                          )}
                        </span>
                      </div>
                      <p className="mt-1 text-xs text-muted-ink">
                        {quote.day_use
                          ? `${formatDate(quoteStay.from)} · ${slot.from}-${slot.to}`
                          : `${formatDate(quoteStay.from)} → ${formatDate(quoteStay.to)}`}
                      </p>
                      {otaTotal > 0 && (
                        <p className="mt-1 text-xs font-medium text-indigo-text">
                          {t("Billed at the price {site} sold it for", { site: viaName })}
                        </p>
                      )}
                    </div>

                    {options?.property && (
                      <p className="pt-1 text-xs leading-relaxed text-muted-ink">
                        {cancelText}
                        {(options.property.no_show_charge || "None") !== "None" &&
                          translate(" No-show: {v0} charged.", { v0: String(String(options.property.no_show_charge).toLowerCase()) })}
                        {advanceNow > 0 && advancePercents.size <= 1 &&
                          translate(" Deposit expected now: {v0}{v1} ({v2}%).", { v0: "", v1: homeText(advanceNow), v2: String(advanceRooms[0]?.percent ?? 0) })}
                        {advanceNow > 0 && advancePercents.size > 1 &&
                          translate(" Advance {v0}{v1}:", { v0: "", v1: homeText(advanceNow) })}
                      </p>
                    )}
                    {options?.property && advanceNow > 0 && advancePercents.size > 1 && (
                      <ul className="text-xs text-muted-ink">
                        {advanceRooms.map((r, i) => (
                          <li key={i}>{`${r.name} · ${r.percent}% · ${homeText(r.amount)}`}</li>
                        ))}
                      </ul>
                    )}
                  </div>
                ) : (
                  <p className="text-sm text-muted-ink">
                    {error ? t("Fix the issue below to see a price.") : "…"}
                  </p>
                )}

                {error && (
                  <div className="mt-4 rounded-lg border border-danger-line bg-danger-subtle px-3 py-2.5 text-sm text-danger-ink">
                    {translate(error)}
                  </div>
                )}
              </div>

              <div className="shrink-0 space-y-2 border-t border-line bg-surface px-6 py-4 md:px-7">
                {walkIn ? (
                  <>
                    {tillClosed && (
                      <>
                        <TillCard till={till} onOpened={() => void refreshTill()}
                          purpose={t("to take the walk-in's payment")} />
                        <p className="text-xs text-muted-ink">
                          {t("Or set the amount to 0: the stay goes on the bill and the guest pays at check-out.")}
                        </p>
                      </>
                    )}
                    <Button
                      className="w-full justify-center py-2.5 text-base"
                      disabled={walkInBlocked || tillClosed}
                      onClick={() => submitWalkIn()}
                    >
                      {busy
                        ? t("Saving…")
                        : payAmount > 0
                          ? t("Collect {amt} and continue to check-in", {
                              amt: currencyText(payCurrency, payAmount, moneyLoc),
                            })
                          : t("Continue to check-in")}
                    </Button>
                    <Button
                      variant="outline"
                      className="w-full justify-center"
                      onClick={props.onClose}
                    >
                      {t("Cancel")}
                    </Button>
                  </>
                ) : (
                <>
                {messages.length > 0 && <div className="relative mb-2 text-sm text-danger-ink">
                  <Button variant="outline" aria-expanded={messagesOpen} onClick={() => setMessagesOpen(!messagesOpen)}><ShieldAlert className="size-4" />{t("Messages ({n})", {n: messages.length})}</Button>
                  {messagesOpen && <section aria-label={t("Booking messages")} className="absolute bottom-full z-10 mb-1 max-h-52 w-full overflow-auto rounded-lg border border-danger-line bg-surface p-2 shadow-lg">
                    <ul>{messages.map(m => <li key={m.field}>{m.field === "server" ? <span role="alert">{m.message}</span> : <button className="w-full p-2 text-start underline" onClick={() => jump(m.field)}>{m.message}</button>}</li>)}</ul>
                  </section>}
                </div>}
                <Button
                  className="w-full justify-center py-2.5 text-base"
                  disabled={busy || quoting}
                  onClick={() => submit()}
                >
                  {busy ? t("Booking…") : t("Confirm booking")}
                </Button>
                <div className="grid grid-cols-2 gap-2">
                  <Button
                    variant="outline"
                    className="justify-center"
                    onClick={props.onClose}
                  >
                    {t("Cancel")}
                  </Button>
                  <Button
                    variant="outline"
                    className="justify-center"
                    disabled={busy || !form.guest_name || badDate || pastCheckIn || badPhone || badSlot || outDraft !== null || blacklistBlocked || extraNeedsReason}
                    onClick={() => submit(true)}
                    title={t("Park this stay with no room; promote when inventory frees")}
                  >
                    {t("Waitlist")}
                  </Button>
                </div>
                </>
                )}
              </div>
            </aside>
          </div>
        )}
      </div>
      {guidedCheckIn && (
        <CheckInDialog
          reservation={guidedCheckIn}
          onClose={() => setGuidedCheckIn(null)}
          onDone={() => {
            setGuidedCheckIn(null)
            setWalkInCheckedIn(true)
            props.onBooked()
          }}
        />
      )}
    </div>
  )
}
