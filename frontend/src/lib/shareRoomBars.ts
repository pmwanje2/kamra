interface Stay {
  name: string
  status?: string
  share_group_id?: string | null
  is_share_primary?: number
  check_in_date: string
  check_out_date: string
}

/** Split at every change of occupants and show an in-house guest when possible. */
function datedShareBars<T extends Stay>(peers: T[]): (T & { share_count: number })[] {
  const dates = [...new Set(peers.flatMap((stay) => [stay.check_in_date, stay.check_out_date]))].sort()
  const ordered = [...peers].sort((a, b) =>
    Number(b.status === "Checked In") - Number(a.status === "Checked In") ||
    Number(!!b.is_share_primary) - Number(!!a.is_share_primary) ||
    a.check_in_date.localeCompare(b.check_in_date) || a.name.localeCompare(b.name))
  return dates.slice(0, -1).flatMap((start, index) => {
    const active = ordered.filter((stay) => stay.check_in_date <= start && start < stay.check_out_date)
    return active.length ? [{
      ...active[0], check_in_date: start, check_out_date: dates[index + 1], share_count: active.length,
    }] : []
  })
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
    if (peers.some((stay) => stay.status)) {
      bars.push(...datedShareBars(peers))
      continue
    }
    // Callers without stay statuses only need the union of their dates.
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
