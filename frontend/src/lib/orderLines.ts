/**
 * Pure order-line logic shared by the new-request and request-detail pages.
 * Mirrors the server rules (backend computes totals authoritatively):
 * - a line is active when its kind is not alternative/optional AND it has no
 *   parent or its parent is active;
 * - net = sum of active line totals; grand = net + tax.
 */
import type { LineKind, OrderLineInput, SourceBox } from '@/lib/api';
import type { ProcurementRequestWithDetails } from '@/types/database';

export type Line = OrderLineInput & {
  key: string;
  kind: LineKind;
  parentKey: string | null;
  /** Soft-removed in the editor: struck through, out of the totals, restorable. Never saved. */
  removed?: boolean;
};

export const LINE_KINDS: LineKind[] = [
  'item',
  'discount',
  'surcharge',
  'shipping',
  'fee',
  'alternative',
  'optional',
];

const round2 = (n: number) => Math.round(n * 100) / 100;

/** Give every line a stable key/kind/parentKey (hand-typed or legacy lines lack them). */
export function normalizeLines(lines: OrderLineInput[]): Line[] {
  const used = new Set<string>();
  return lines.map((l, i) => {
    let key = l.key || l.position || `L${i + 1}`;
    while (used.has(key)) key = `${key}#${i + 1}`;
    used.add(key);
    return { ...l, key, kind: l.kind ?? 'item', parentKey: l.parentKey ?? null };
  });
}

export function newLineKey(lines: Line[]): string {
  let n = lines.length + 1;
  while (lines.some((l) => l.key === `L${n}`)) n++;
  return `L${n}`;
}

export function byKey(lines: Line[], key: string | null | undefined): Line | undefined {
  return key ? lines.find((l) => l.key === key) : undefined;
}

export function isActive(line: Line, lines: Line[], seen = new Set<string>()): boolean {
  if (line.removed) return false;
  if (line.kind === 'alternative' || line.kind === 'optional') return false;
  if (!line.parentKey) return true;
  if (seen.has(line.key)) return false; // defensive: never loop on bad data
  seen.add(line.key);
  const parent = byKey(lines, line.parentKey);
  return !!parent && isActive(parent, lines, seen);
}

/** Recompute unit price (from list price + adjustment) and total. */
export function recalcLine(line: Line): Line {
  let unitPrice = line.unitPrice;
  if (line.listUnitPrice != null) {
    unitPrice =
      line.adjustmentPercent != null
        ? round2(line.listUnitPrice * (1 + line.adjustmentPercent / 100))
        : line.listUnitPrice;
  }
  return { ...line, unitPrice, totalPrice: round2(line.amount * unitPrice) };
}

export function computeTotals(lines: Line[], taxTotal: number): { net: number; grand: number } {
  const net = round2(
    lines.filter((l) => isActive(l, lines)).reduce((sum, l) => sum + l.totalPrice, 0),
  );
  return { net, grand: round2(net + (taxTotal || 0)) };
}

/**
 * Choose an alternative: it becomes the item; the item it replaced becomes an
 * alternative of it; sibling alternatives re-point to it. Adjustments stay
 * linked to the line they were quoted for.
 */
export function swapAlternative(lines: Line[], key: string): Line[] {
  const chosen = byKey(lines, key);
  const old = byKey(lines, chosen?.parentKey);
  if (!chosen || !old || chosen.kind !== 'alternative') return lines;
  return lines.map((l) => {
    if (l.key === chosen.key) return { ...l, kind: 'item', parentKey: null };
    if (l.key === old.key) return { ...l, kind: 'alternative', parentKey: chosen.key };
    if (l.kind === 'alternative' && l.parentKey === old.key) return { ...l, parentKey: chosen.key };
    return l;
  });
}

/** Change a line's kind, keeping links valid. */
export function setKind(lines: Line[], key: string, kind: LineKind): Line[] {
  return lines.map((l) => {
    if (l.key === key) return { ...l, kind, parentKey: kind === 'item' ? null : l.parentKey };
    // Lines that hung off a line that is no longer an item lose their link.
    if (l.parentKey === key && kind !== 'item') return { ...l, parentKey: null };
    return l;
  });
}

/** Soft remove / restore: the line stays visible (struck through) so it can come back. */
export function setRemoved(lines: Line[], key: string, removed: boolean): Line[] {
  return lines.map((l) => (l.key === key ? { ...l, removed } : l));
}

/** Lines to send to the API: soft-removed lines are dropped, links to them cleared. */
export function linesForSave(lines: Line[]): Line[] {
  const removed = new Set(lines.filter((l) => l.removed).map((l) => l.key));
  return lines
    .filter((l) => !l.removed)
    .map(({ removed: _removed, ...l }) =>
      l.parentKey && removed.has(l.parentKey)
        ? { ...l, parentKey: null, kind: l.kind === 'alternative' ? 'optional' : l.kind }
        : l,
    );
}

/** A line's effective group: its own, or (for a linked charge) its parent's. */
export function lineGroupId(line: Line, lines: Line[]): number | null {
  if (line.commodityGroupId) return line.commodityGroupId;
  const parent = byKey(lines, line.parentKey);
  return parent?.commodityGroupId ?? null;
}

/** Spend per group: each active line's total booked under its effective group (no tax). */
export function groupSpend(lines: Line[]): Map<number, number> {
  const spend = new Map<number, number>();
  for (const l of lines) {
    if (!isActive(l, lines)) continue;
    const g = lineGroupId(l, lines);
    if (g) spend.set(g, round2((spend.get(g) ?? 0) + l.totalPrice));
  }
  return spend;
}

/** Request-level group = the one with the largest spend among active lines (server rule). */
export function primaryGroupId(lines: Line[]): number | null {
  const spend = groupSpend(lines);
  let best: number | null = null;
  for (const [g, v] of spend) if (best === null || v > (spend.get(best) ?? 0)) best = g;
  return best;
}

/** Source boxes of the active lines booked to a group: where its spend comes from. */
export function groupSources(lines: Line[], groupId: number | null | undefined): SourceBox[] {
  if (groupId == null) return [];
  return lines
    .filter((l) => isActive(l, lines) && lineGroupId(l, lines) === groupId)
    .flatMap((l) => l.sources ?? []);
}

/** The save error when an active line has no group (same wording as the server), else null. */
export function missingGroupError(lines: Line[]): string | null {
  const missing = lines
    .filter((l) => isActive(l, lines) && !lineGroupId(l, lines))
    .map((l) => l.positionDescription.trim() || l.position || l.key);
  return missing.length
    ? `Every order line needs a commodity group (missing on: ${missing.join(', ')})`
    : null;
}

/** Map stored lines (snake_case base + camelCase rich fields) to editor lines. */
export const linesFromRequest = (req: ProcurementRequestWithDetails): Line[] =>
  normalizeLines(
    (req.order_lines || []).map((l) => ({
      positionDescription: l.position_description,
      unitPrice: l.unit_price,
      amount: l.amount,
      unit: l.unit,
      totalPrice: l.total_price,
      key: l.key,
      parentKey: l.parentKey ?? null,
      kind: l.kind ?? 'item',
      position: l.position ?? null,
      vendorItemNumber: l.vendorItemNumber ?? null,
      listUnitPrice: l.listUnitPrice ?? null,
      adjustmentPercent: l.adjustmentPercent ?? null,
      sources: l.sources ?? [],
      sourceText: l.sourceText ?? null,
      articleId: l.articleId ?? null,
      originalQuote: l.originalQuote ?? null,
      commodityGroupId: l.commodityGroupId ?? null,
    })),
  );

/** Every group a request books spend under (plus its summary group, for old requests). */
export function requestGroupIds(req: ProcurementRequestWithDetails): Set<number> {
  const ids = new Set(groupSpend(linesFromRequest(req)).keys());
  if (req.commodity_group_id) ids.add(req.commodity_group_id);
  return ids;
}

/** Spend per group across requests, added up line by line. */
export function spendByGroup(requests: ProcurementRequestWithDetails[]): Map<number, number> {
  const total = new Map<number, number>();
  for (const req of requests) {
    for (const [g, v] of groupSpend(linesFromRequest(req))) {
      total.set(g, round2((total.get(g) ?? 0) + v));
    }
  }
  return total;
}

/** Items in order, each followed by its children; then whole-quote charges. */
export function groupLines(lines: Line[]): {
  items: { item: Line; children: Line[] }[];
  quoteLevel: Line[];
} {
  const items = lines
    .filter((l) => l.kind === 'item')
    .map((item) => ({
      item,
      children: lines
        .filter((l) => l.parentKey === item.key && l.kind !== 'item')
        .sort((a, b) => Number(b.kind === 'alternative') - Number(a.kind === 'alternative')),
    }));
  const placed = new Set(items.flatMap((g) => [g.item.key, ...g.children.map((c) => c.key)]));
  return { items, quoteLevel: lines.filter((l) => !placed.has(l.key)) };
}
