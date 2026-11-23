/** Where a function's money and bill go (kamra.banquet.function_sheet).
 *  Under the ERP accounting bridge a standalone function bills on its own
 *  event folio and its tax invoice comes from that folio; without it only a
 *  function tied to a group can post to a bill (the group's master folio). */
export function eventBilling(fn: {
  fiscal_bridge?: number
  group_booking?: string | null
  status: string
  event_folio?: string | null
}) {
  const bridge = !!fn.fiscal_bridge
  return {
    canPost: fn.status !== "Enquiry" && (!!fn.group_booking || bridge),
    invoiceOnFolio: bridge,
    folio: fn.event_folio ?? null,
  }
}
