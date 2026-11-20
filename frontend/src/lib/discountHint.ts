/** A discount comes off posted room nights. Before the night audit has posted
 *  any, the server refuses with NoRoomNightsToDiscount and the desk is offered
 *  Advance bill (OPERA advance billing) to post the stay first. */

export const NO_ROOM_NIGHTS = "NoRoomNightsToDiscount"
export const ADVANCE_BILL_ACTION = "advance-bill"

/** True when a failed call is that refusal; keyed on the exception class. */
export function isNoRoomNights(e: unknown): boolean {
  const body = (e as { body?: unknown } | null)?.body
  if (typeof body !== "string") return false
  try {
    return (JSON.parse(body) as { exc_type?: string }).exc_type === NO_ROOM_NIGHTS
  } catch {
    return false
  }
}

/** The folio screen with its Advance bill action opened. */
export function advanceBillHref(folio: string): string {
  return `/billing/${encodeURIComponent(folio)}?action=${ADVANCE_BILL_ACTION}`
}
