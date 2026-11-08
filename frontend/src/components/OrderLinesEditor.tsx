'use client';

import { useEffect, useState } from 'react';
import type { LineKind } from '@/lib/api';
import type { CommodityGroup } from '@/types/database';
import { Input } from '@/components/base';
import SourceButton from '@/components/SourceButton';
import {
  LINE_KINDS,
  type Line,
  computeTotals,
  groupLines,
  isActive,
  newLineKey,
  recalcLine,
  setRemoved,
  lineGroupId,
  setKind,
  swapAlternative,
} from '@/lib/orderLines';
import { cn } from '@/lib/utils';

const KIND_LABEL: Record<LineKind, string> = {
  item: 'Item',
  alternative: 'Alternative',
  optional: 'Optional',
  discount: 'Discount',
  surcharge: 'Surcharge',
  shipping: 'Shipping',
  fee: 'Fee',
};

const KIND_TAG: Record<LineKind, string> = {
  item: 'bg-ink-800/[0.05] text-ink/60',
  alternative: 'bg-indigo-50 text-indigo-700',
  optional: 'bg-violet-50 text-violet-700',
  discount: 'bg-green-50 text-green-700',
  surcharge: 'bg-orange-50 text-amber-700',
  shipping: 'bg-cyan-50 text-accent-deep',
  fee: 'bg-cyan-50 text-accent-deep',
};

export const usd = (n: number | null | undefined) =>
  n == null
    ? '—'
    : (n < 0 ? '−$' : '$') +
      Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const toNum = (v: string): number | null => {
  const s = v.replace(/[$,\s]/g, '');
  if (s === '' || s === '-') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
};

const selectCls =
  'flex h-11 w-full rounded-xl border border-line bg-white px-3 text-sm text-ink focus:outline-none focus:ring-2 focus:ring-accent-deep/60 focus:border-accent-deep/40';

export interface OrderLinesEditorProps {
  lines: Line[];
  taxTotal: number;
  editable: boolean;
  onChange: (lines: Line[], taxTotal: number) => void;
  /** Highlight this line in the document panel. Omit when no PDF is attached. */
  onShowSource?: (line: Line) => void;
  /** Grand total printed on the quote, for the "matches quote" check. */
  quotedGrandTotal?: number | null;
  /** Extra content under a line in read mode (e.g. catalog suggestions). */
  renderLineExtra?: (line: Line) => React.ReactNode;
  lockedReason?: string;
  /** The org's active commodity groups, for the per-line picker. */
  groups?: CommodityGroup[];
}

export default function OrderLinesEditor({
  lines,
  taxTotal,
  editable,
  onChange,
  onShowSource,
  quotedGrandTotal,
  renderLineExtra,
  lockedReason,
  groups,
}: OrderLinesEditorProps) {
  const groupName = (id: number | null | undefined) => {
    const g = groups?.find((x) => x.id === id);
    return g ? g.name : null;
  };
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const { items, quoteLevel } = groupLines(lines);
  const totals = computeTotals(lines, taxTotal);
  const itemLines = lines.filter((l) => l.kind === 'item');

  const update = (next: Line[]) => onChange(next, taxTotal);
  const patch = (key: string, changes: Partial<Line>, recalc = true) =>
    update(
      lines.map((l) =>
        l.key === key ? (recalc ? recalcLine({ ...l, ...changes }) : { ...l, ...changes }) : l,
      ),
    );

  const addLine = () => {
    const key = newLineKey(lines);
    update([
      ...lines,
      {
        key,
        kind: 'item',
        parentKey: null,
        positionDescription: '',
        amount: 0,
        unit: '',
        unitPrice: 0,
        totalPrice: 0,
        sources: [],
      },
    ]);
    setEditingKey(key);
  };

  const diff =
    quotedGrandTotal != null ? Math.round((totals.grand - quotedGrandTotal) * 100) / 100 : null;

  const docButton = (line: Line) =>
    onShowSource && (line.sources?.length ?? 0) > 0 ? (
      <SourceButton onClick={() => onShowSource(line)} />
    ) : null;

  const subline = (line: Line) => {
    const parts: string[] = [];
    if (line.kind !== 'item' && line.kind !== 'alternative') {
      const parent = itemLines.find((i) => i.key === line.parentKey);
      parts.push(parent ? `Applies to ${parent.position ?? parent.key}` : 'Whole quote');
    }
    let qty = `${line.amount} ${line.unit} × ${usd(line.unitPrice)}`;
    if (line.listUnitPrice != null && line.adjustmentPercent != null) {
      qty = `${line.amount} ${line.unit} × ${usd(line.listUnitPrice)} ${line.adjustmentPercent < 0 ? '−' : '+'}${Math.abs(line.adjustmentPercent)}%`;
    }
    parts.push(qty);
    const g = groupName(lineGroupId(line, lines));
    if (g) parts.push(g);
    return parts.join(' · ');
  };

  const readRow = (line: Line) => {
    const active = isActive(line, lines);
    return (
      <div
        key={line.key}
        role={editable ? 'button' : undefined}
        tabIndex={editable ? 0 : undefined}
        onClick={editable && !line.removed ? () => setEditingKey(line.key) : undefined}
        onKeyDown={editable && !line.removed ? (e) => e.key === 'Enter' && setEditingKey(line.key) : undefined}
        className={cn(
          'rounded-lg bg-ink-800/[0.02] p-3',
          editable && !line.removed && 'cursor-pointer transition-colors hover:bg-ink-800/[0.045]',
        )}
      >
        <div className="flex items-start justify-between gap-3">
          <div className={cn('min-w-0', line.removed && 'text-ink/40 line-through')}>
            <p
              title={line.positionDescription}
              className={cn('line-clamp-2 text-sm', line.removed ? 'text-ink/40' : 'text-ink', line.kind === 'item' && 'font-medium')}
            >
              {line.position && <span className="text-ink/45">{line.position} · </span>}
              {line.positionDescription || <span className="text-ink/40">Untitled line</span>}
              {line.kind !== 'item' && (
                <span className={cn('ml-1.5 inline-block rounded-full px-2 py-px align-[1px] text-xs font-medium', KIND_TAG[line.kind])}>
                  {KIND_LABEL[line.kind]}
                </span>
              )}
            </p>
            <p className="mt-0.5 text-sm text-ink/55">
              {subline(line)}
              {line.kind === 'alternative' && editable && line.parentKey && !line.removed && (
                <>
                  {' · '}
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      update(swapAlternative(lines, line.key));
                    }}
                    className="font-medium text-accent-deep hover:underline"
                  >
                    Use this option
                  </button>
                </>
              )}
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-1">
            {line.removed && editable && (
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  update(setRemoved(lines, line.key, false));
                }}
                className="mr-2 text-sm font-medium text-accent-deep hover:underline"
              >
                Restore
              </button>
            )}
            <p
              className={cn(
                'text-sm tabular-nums',
                active ? 'font-semibold text-ink' : 'font-medium text-ink/35',
                line.removed && 'line-through',
              )}
              title={active ? undefined : 'Not included in the total'}
            >
              {usd(line.totalPrice)}
            </p>
            {docButton(line)}
          </div>
        </div>
        {renderLineExtra?.(line)}
      </div>
    );
  };

  const editRow = (line: Line) => {
    const parentOptions = itemLines.filter((i) => i.key !== line.key);
    return (
      <div
        key={line.key}
        className="rounded-lg border border-accent-deep/35 bg-white p-4 shadow-[0_0_0_3px_rgba(174,223,233,0.35)]"
      >
        <div className="mb-3 flex items-center justify-between gap-2">
          <p className="text-sm font-medium text-ink">
            Editing {line.position ? `position ${line.position}` : 'line'}
          </p>
          <div className="flex items-center gap-3">
            {docButton(line)}
            <button
              type="button"
              onClick={() => {
                update(setRemoved(lines, line.key, true));
                setEditingKey(null);
              }}
              className="text-sm font-medium text-red-600 hover:text-red-700"
            >
              Remove
            </button>
            <button
              type="button"
              onClick={() => setEditingKey(null)}
              className="h-8 rounded-full bg-ink px-4 text-sm font-medium text-white hover:bg-ink-700"
            >
              Done
            </button>
          </div>
        </div>

        <Input
          label="Description"
          value={line.positionDescription}
          onChange={(e) => patch(line.key, { positionDescription: e.target.value }, false)}
        />

        <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <DecimalField
            label="Quantity"
            placeholder="0"
            value={line.amount}
            onValue={(n) => patch(line.key, { amount: n ?? 0 })}
          />
          <Input
            label="Unit"
            value={line.unit}
            onChange={(e) => patch(line.key, { unit: e.target.value }, false)}
          />
          <DecimalField
            label="Unit price ($)"
            placeholder="0.00"
            value={line.unitPrice}
            onValue={(n) =>
              patch(line.key, {
                unitPrice: n ?? 0,
                listUnitPrice: null,
                adjustmentPercent: null,
              })
            }
          />
          <DecimalField
            label="Adjustment (%)"
            title="Negative = discount, positive = surcharge (e.g. -20)"
            placeholder="e.g. -20"
            value={line.adjustmentPercent}
            onValue={(adj) => {
              patch(line.key, {
                adjustmentPercent: adj,
                // Apply the percentage to the list price; start from the current price.
                listUnitPrice: adj == null ? line.listUnitPrice : (line.listUnitPrice ?? line.unitPrice),
              });
            }}
            helperText={line.listUnitPrice != null ? `List price ${usd(line.listUnitPrice)}` : undefined}
          />
        </div>

        <div className="mt-3 grid grid-cols-1 gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="mb-1.5 block text-sm font-medium text-ink/70">Type</span>
            <select
              className={selectCls}
              value={line.kind}
              onChange={(e) => update(setKind(lines, line.key, e.target.value as LineKind))}
            >
              {LINE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {KIND_LABEL[k]}
                </option>
              ))}
            </select>
          </label>
          {line.kind !== 'item' && (
            <label className="block">
              <span className="mb-1.5 block text-sm font-medium text-ink/70">
                {line.kind === 'alternative' ? 'Replaces' : line.kind === 'optional' ? 'Add-on to' : 'Applies to'}
              </span>
              <select
                className={selectCls}
                value={line.parentKey ?? ''}
                onChange={(e) => patch(line.key, { parentKey: e.target.value || null }, false)}
              >
                {line.kind === 'alternative' ? (
                  <option value="">Choose a position…</option>
                ) : (
                  <option value="">Whole quote</option>
                )}
                {parentOptions.map((i) => (
                  <option key={i.key} value={i.key}>
                    {i.position ? `${i.position} · ` : ''}
                    {i.positionDescription.slice(0, 60)}
                  </option>
                ))}
              </select>
            </label>
          )}
          {groups && (
            <label className="block sm:col-span-2">
              <span className="mb-1.5 block text-sm font-medium text-ink/70">Commodity group</span>
              <select
                className={selectCls}
                value={line.commodityGroupId ?? ''}
                onChange={(e) =>
                  patch(line.key, { commodityGroupId: e.target.value ? Number(e.target.value) : null }, false)
                }
              >
                <option value="">
                  {line.parentKey && line.kind !== 'item' && lineGroupId(line, lines)
                    ? `Same as its position (${groupName(lineGroupId(line, lines))})`
                    : 'Choose a group…'}
                </option>
                {groups.map((g) => (
                  <option key={g.id} value={g.id}>
                    {g.category} · {g.name}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <p className="mt-3 text-right text-sm text-ink/55">
          Line total <span className="ml-2 font-semibold text-ink tabular-nums">{usd(line.totalPrice)}</span>
        </p>
      </div>
    );
  };

  const row = (line: Line) =>
    editable && editingKey === line.key && !line.removed ? editRow(line) : readRow(line);

  return (
    <div>
      {!editable && lockedReason && (
        <p className="mb-3 rounded-lg bg-ink-800/[0.04] px-3 py-2 text-sm text-ink/55">{lockedReason}</p>
      )}

      {lines.length === 0 ? (
        <p className="py-8 text-center text-sm text-ink/45">No order lines yet. Add one to get started.</p>
      ) : (
        <div className="space-y-2">
          {items.map(({ item, children }) => (
            <div key={item.key}>
              {row(item)}
              {children.length > 0 && (
                <div className="mt-1.5 ml-5 space-y-1.5 border-l-2 border-line pl-3">
                  {children.map((c) => row(c))}
                </div>
              )}
            </div>
          ))}
          {quoteLevel.map((l) => row(l))}
        </div>
      )}

      {editable && (
        <button
          type="button"
          onClick={addLine}
          className="mt-3 text-sm font-medium text-accent-deep hover:underline"
        >
          + Add line
        </button>
      )}

      <dl className="mt-4 grid grid-cols-[1fr_auto] gap-x-6 gap-y-1.5 border-t border-line pt-3 text-sm">
        <dt className="text-right text-ink/55">Net</dt>
        <dd className="text-right tabular-nums">{usd(totals.net)}</dd>
        <dt className="self-center text-right text-ink/55">Tax</dt>
        <dd className="text-right tabular-nums">
          {editable ? (
            <DecimalField
              plain
              aria-label="Tax"
              placeholder="0.00"
              value={taxTotal}
              onValue={(n) => onChange(lines, n ?? 0)}
              className="h-8 w-28 rounded-lg border border-line bg-white px-2 text-right text-sm tabular-nums focus:outline-none focus:ring-2 focus:ring-accent-deep/60"
            />
          ) : (
            usd(taxTotal)
          )}
        </dd>
        <dt className="text-right font-semibold text-ink">Total</dt>
        <dd className="text-right text-base font-semibold tabular-nums">{usd(totals.grand)}</dd>
        {diff != null && Math.abs(diff) >= 0.01 && (
          <dd className="col-span-2 text-right text-sm text-amber-700">
            {usd(Math.abs(diff))} {diff < 0 ? 'below' : 'above'} the quoted total {usd(quotedGrandTotal)}
          </dd>
        )}
      </dl>
    </div>
  );
}

/**
 * Text box for a number. Keeps exactly what is typed ("0.", "-", "1,5") so
 * partial input isn't rewritten, and starts empty instead of showing 0.
 */
function DecimalField({
  value,
  onValue,
  plain,
  ...rest
}: Omit<React.ComponentProps<typeof Input>, 'value' | 'onChange'> & {
  value: number | null | undefined;
  onValue: (n: number | null) => void;
  plain?: boolean;
}) {
  const shown = (v: number | null | undefined) => (v == null || v === 0 ? '' : String(v));
  const [text, setText] = useState(shown(value));
  // Follow outside changes (recalculation, swaps) without fighting the typist.
  useEffect(() => {
    const typed = toNum(text);
    if (typed !== (value ?? null) && !(text.trim() === '' && (value ?? 0) === 0)) setText(shown(value));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [value]);
  const props = {
    ...rest,
    inputMode: 'decimal' as const,
    value: text,
    onChange: (e: React.ChangeEvent<HTMLInputElement>) => {
      setText(e.target.value);
      onValue(toNum(e.target.value));
    },
  };
  return plain ? <input {...props} /> : <Input {...props} />;
}

