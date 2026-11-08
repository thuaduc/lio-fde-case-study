/**
 * Chart colors and slice building.
 *
 * Categorical slots, teal first for the brand, validated with the data-viz
 * palette checks (lightness band, chroma floor, colour-blind and normal-vision
 * separation of neighbours, including a pie's last-to-first wrap). Keep the
 * order: it is what keeps neighbouring slices apart.
 */
export const SERIES_COLORS = [
  '#008fa7', // teal
  '#eb6834', // orange
  '#6250d6', // violet
  '#eda100', // yellow
  '#e87ba4', // magenta
  '#008300', // green
  '#2a78d6', // blue
  '#e34948', // red
] as const;

/** The folded tail ("Other"): a neutral that recedes behind the series. */
export const OTHER_COLOR = '#c3c2b7';

export interface Slice {
  name: string;
  value: number;
  color: string;
}

const round2 = (n: number) => Math.round(n * 100) / 100;

/**
 * Stable colours per entity: the largest first, by `value` across everything
 * the page shows, so one entity keeps its colour in every chart. Past eight
 * entities, the first seven keep a colour and the rest fold into "Other".
 */
export function colorsByRank(values: Map<number, number>): Map<number, string> {
  const ranked = [...values].filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
  const keep = ranked.length > SERIES_COLORS.length ? SERIES_COLORS.length - 1 : ranked.length;
  return new Map(ranked.slice(0, keep).map(([id], i) => [id, SERIES_COLORS[i]]));
}

/**
 * Slices largest first, each in its entity's colour; entities without a
 * colour (or a name) are summed into a trailing "Other".
 */
export function slices(
  values: Map<number, number>,
  colors: Map<number, string>,
  nameOf: (id: number) => string | undefined,
): Slice[] {
  const out: Slice[] = [];
  let other = 0;
  for (const [id, value] of values) {
    if (value <= 0) continue;
    const color = colors.get(id);
    const name = nameOf(id);
    if (color && name) out.push({ name, value, color });
    else other += value;
  }
  out.sort((a, b) => b.value - a.value);
  if (other > 0) out.push({ name: 'Other', value: round2(other), color: OTHER_COLOR });
  return out;
}
