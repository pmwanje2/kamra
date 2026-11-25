interface Stay {
  name: string
  share_group_id?: string | null
  is_share_primary?: number
  check_in_date: string
  check_out_date: string
}

/** Collapse connected date ranges while keeping gaps and unrelated bookings. */
export function shareRoomBars<T extends Stay>(bookings: T[]): (T & { share_count?: number })[] {
  const bars: (T & { share_count?: number })[] = []
  const groups = new Map<string, T[]>()
  for (const stay of bookings) {
    if (!stay.share_group_id) {
      bars.push(stay)
      continue
    }
    const peers = groups.get(stay.share_group_id) ?? []
    peers.push(stay)
    groups.set(stay.share_group_id, peers)
  }
  for (const peers of groups.values()) {
    peers.sort((a, b) => a.check_in_date.localeCompare(b.check_in_date) || a.name.localeCompare(b.name))
    let start = peers[0].check_in_date
    let end = peers[0].check_out_date
    let lead = peers[0]
    let count = 0
    for (const stay of peers) {
      if (stay.check_in_date >= end && count) {
        bars.push({ ...lead, check_in_date: start, check_out_date: end, share_count: count })
        start = stay.check_in_date
        end = stay.check_out_date
        lead = stay
        count = 0
      }
      if (stay.is_share_primary) lead = stay
      if (stay.check_out_date > end) end = stay.check_out_date
      count++
    }
    bars.push({ ...lead, check_in_date: start, check_out_date: end, share_count: count })
  }
  return bars.sort((a, b) => a.check_in_date.localeCompare(b.check_in_date) || a.name.localeCompare(b.name))
}
