/** Booking-page accent. Presets and stored hex values retain their hue while
 * the derived foregrounds and solid controls maintain their contrast. */

export const PRESETS: { name: string; hex: string }[] = [
  { name: "Emerald", hex: "#0f6b54" },
  { name: "Ocean", hex: "#0369a1" },
  { name: "Royal", hex: "#4f46e5" },
  { name: "Sunset", hex: "#c2410c" },
  { name: "Burgundy", hex: "#be123c" },
  { name: "Slate", hex: "#334155" },
]
const PRESET_HEX: Record<string, string> = Object.fromEntries(
  PRESETS.map((p) => [p.name, p.hex]),
)

const DEFAULT = "#0f6b54"

function toRgb(hex: string): [number, number, number] {
  const h = hex.slice(1)
  const n = h.length === 3 ? h.split("").map((c) => c + c).join("") : h
  const v = parseInt(n, 16)
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255]
}

function mix(hex: string, target: number, ratio: number): string {
  return "#" + toRgb(hex).map((channel) =>
    Math.round(channel + (target - channel) * ratio).toString(16).padStart(2, "0"),
  ).join("")
}

function luminance(hex: string) {
  const [red, green, blue] = toRgb(hex).map((channel) => {
    const s = channel / 255
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * red + 0.7152 * green + 0.0722 * blue
}

/** Resolve a stored hex color or a legacy preset name. */
export function accentHex(value: string | null | undefined): string {
  if (!value) return DEFAULT
  if (/^#(?:[\da-f]{3}|[\da-f]{6})$/i.test(value)) return value
  return PRESET_HEX[value] ?? DEFAULT
}

/** Inline custom properties for the public booking subtree. */
export function accentVars(value: string | null | undefined) {
  if (!value) return undefined
  const base = accentHex(value)
  let solid = base
  // Keep white labels readable even for an accent such as #ffffff.
  while (1.05 / (luminance(solid) + 0.05) < 4.5) solid = mix(solid, 0, 0.12)

  return {
    "--color-brand-50": `light-dark(${mix(base, 255, 0.92)}, ${mix(base, 0, 0.82)})`,
    "--color-brand-100": `light-dark(${mix(base, 255, 0.82)}, ${mix(base, 0, 0.72)})`,
    "--color-brand-600": `light-dark(${mix(solid, 0, 0.12)}, ${mix(base, 255, 0.62)})`,
    "--color-brand-700": `light-dark(${mix(solid, 0, 0.22)}, ${mix(base, 255, 0.72)})`,
    "--color-brand-fill": solid,
    "--color-brand-fill-hover": mix(solid, 0, 0.16),
    "--color-brand-deep": mix(solid, 0, 0.45),
  } as React.CSSProperties
}
