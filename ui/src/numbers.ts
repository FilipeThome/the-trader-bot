/**
 * A number typed in the panel: digits with at most one decimal separator, "," or ".".
 *
 * Returns null when the text is not a number or is ambiguous: "1.000" is one thousand
 * in pt-BR and one in English, so a three-digit fraction after a non-zero whole part is
 * refused instead of guessed. Thousands grouping is never accepted.
 */
export function parseDecimal(text: string): number | null {
  const value = text.trim();
  if (!/^\d+([.,]\d+)?$/.test(value)) return null;
  const [whole, fraction] = value.split(/[.,]/);
  if (fraction !== undefined && fraction.length === 3 && Number(whole) !== 0) return null;
  const parsed = Number(fraction === undefined ? whole : `${whole}.${fraction}`);
  return Number.isFinite(parsed) ? parsed : null;
}

/** A whole number typed in the panel, or null. */
export function parseWhole(text: string): number | null {
  const value = text.trim();
  return /^\d+$/.test(value) ? Number(value) : null;
}

/** "+1,25 R": a price move in units of the entry risk; a dash when unknown. */
export function formatR(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2).replace(".", ",")} R`;
}
