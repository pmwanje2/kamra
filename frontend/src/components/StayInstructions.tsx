/*  Dated stay traces and departmental instructions - delegates to TracesPanel
    so all reads and writes flow through kamra.traces.* with business-date
    activation, overdue tracking, and property isolation. */

import TracesPanel, { type StayTraceItem } from "./TracesPanel"

export default function StayInstructions({
  reservation,
  canAdd = true,
  businessDate,
  checkInDate,
  initialTraces,
}: {
  reservation: string
  canAdd?: boolean
  businessDate?: string
  checkInDate?: string
  initialTraces?: StayTraceItem[]
}) {
  return (
    <TracesPanel
      reservation={reservation}
      canAdd={canAdd}
      businessDate={businessDate}
      checkInDate={checkInDate}
      initialTraces={initialTraces}
    />
  )
}
