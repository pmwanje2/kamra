/** The tax rate the folio's Post a charge box starts on: the property's own
 *  rate for a guest service when the pack offers it, else the lowest rate
 *  offered (0% for a property that charges no tax). Never a rate the server
 *  would refuse or a hardcoded one the hotel does not charge. */
export function defaultChargeTaxRate(rates: number[], servicesRate?: number | null): string {
  if (servicesRate != null && rates.includes(Number(servicesRate))) return String(Number(servicesRate))
  if (!rates.length) return "0"
  return String(Math.min(...rates))
}
