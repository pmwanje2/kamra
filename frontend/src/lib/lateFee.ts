/** The Late check-out step's fee box (TKH-12). It starts on the property's
 *  own rule (checkout_context late_checkout.suggested_fee: Fixed, or a %
 *  of the last room night), empty when the property has no rule. A fee
 *  bigger than one night's room rate is almost always a mistake (the whole
 *  bill typed into the box), so the desk must confirm it first. */
export function lateFeeDefault(late: { suggested_fee?: number | null } | null | undefined): string {
  const fee = Number(late?.suggested_fee ?? 0)
  return fee > 0 ? String(fee) : ""
}

export function lateFeeExceedsNight(fee: number, nightRate?: number | null): boolean {
  const night = Number(nightRate ?? 0)
  return night > 0 && Number(fee) > night
}
