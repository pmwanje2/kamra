/*  A manager's sign-off, in place: why it is needed, who can give it and
    their PIN. The one control the desk sees wherever a discount or a rate
    needs one (a booking's discount, Stay rate & daily details, new dates, a
    room move). The server checks the PIN and refuses the person asking. */

import type { ReactNode } from "react"
import type { DiscountGovernance } from "../lib/reservationDiscount"
import { useT } from "../lib/i18n"

const inputCls =
  "rounded-lg border border-control bg-surface px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-focus"

export default function SupervisorSignOff(props: {
  /** why a manager signs this off (signOffReasons for a discount) */
  reason?: ReactNode
  supervisors: { user: string; full_name: string }[]
  supervisor: string
  pin: string
  onSupervisor: (user: string) => void
  onPin: (pin: string) => void
}) {
  const { t } = useT()
  return (
    <div className="space-y-2 rounded-lg bg-warning-subtle p-2">
      <p className="text-xs text-warning-ink">
        {props.reason ? <>{props.reason}{" "}</> : null}
        {t("A manager signs this off with their PIN.")}
      </p>
      {props.supervisors.length === 0 ? (
        <p className="text-xs text-danger-ink">{t("No manager with a cashier PIN can approve here yet.")}</p>
      ) : (
        <div className="flex flex-wrap gap-2">
          <select className={inputCls} aria-label={t("Manager")} value={props.supervisor}
            onChange={(e) => props.onSupervisor(e.target.value)}>
            <option value="">{t("Manager…")}</option>
            {props.supervisors.map((s) => <option key={s.user} value={s.user}>{s.full_name}</option>)}
          </select>
          <input className={`${inputCls} w-24`} type="password" inputMode="numeric" autoComplete="off"
            value={props.pin} onChange={(e) => props.onPin(e.target.value.replace(/\D/g, "").slice(0, 8))}
            placeholder={t("Their PIN")} aria-label={t("Manager PIN")} />
        </div>
      )}
    </div>
  )
}

/** Why a discount needs a manager (kamra.api.discount_governance): over the
 * desk's limit, a comp, under the rate floor or a reason that needs one. */
export function SignOffReasons({ gov }: { gov: DiscountGovernance }) {
  const { t } = useT()
  return (
    <>
      {!gov.can_set_rate && gov.max_pct > gov.limit_pct
        ? t("A cut of {pct}% is over the front desk's limit of {limit}%.", {
            pct: gov.max_pct, limit: gov.limit_pct,
          }) + " " : ""}
      {gov.comp ? t("A 100% discount (comp) needs a manager's sign-off.") + " " : ""}
      {gov.floor_breach.length
        ? t("Below the rate floor on {dates}.", { dates: gov.floor_breach.join(", ") }) + " " : ""}
      {gov.reason_requires_supervisor ? t("This reason needs a manager's sign-off.") : ""}
    </>
  )
}
