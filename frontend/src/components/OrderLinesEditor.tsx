'use client';

import type { LineKind } from '@/lib/api';
import {
  LINE_KINDS,
  type Line,
  computeTotals,
  groupLines,
  isActive,
  newLineKey,
  recalcLine,
  removeLine,
  setKind,
  swapAlternative,
} from '@/lib/orderLines';
import { cn } from '@/lib/utils';

const KIND_STYLE: Record<LineKind, string> = {
  item: 'bg-ink-800/[0.06] text-ink-700',
  alternative: 'bg-indigo-50 text-indigo-700',
  optional: 'bg-violet-50 text-violet-700',
  discount: 'bg-green-50 text-green-700',
  surcharge: 'bg-orange-50 text-amber-700',
  shipping: 'bg-cyan-50 text-accent-deep',
  fee: 'bg-cyan-50 text-accent-deep',
};

const usd = (n: number) =>
  (n < 0 ? '−$' : '$') +
  Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const num = (v: string): number | null => (v.trim() === '' ? null : Number(v));

export interface OrderLinesEditorProps {
  lines: Line[];
  taxTotal: number;
  editable: boolean;
  onChange: (lines: Line[], taxTotal: number) => void;
  /** Highlight this line in the document panel. Omit when no PDF is attached. */
  onShowSource?: (line: Line) => void;
  /** Grand total printed on the quote, for the "matches quote" check. */
  quotedGrandTotal?: number | null;
  /** Extra content under a line (e.g. catalog suggestions). */
  renderLineExtra?: (line: Line) => React.ReactNode;
  lockedReason?: string;
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
}: OrderLinesEditorProps) {
  const { items, quoteLevel } = groupLines(lines);
  const totals = computeTotals(lines, taxTotal);
  const itemLines = lines.filter((l) => l.kind === 'item');

  const update = (next: Line[]) => onChange(next, taxTotal);
  const patch = (key: string, changes: Partial<Line>, recalc = true) =>
    update(lines.map((l) => (l.key === key ? (recalc ? recalcLine({ ...l, ...changes }) : { ...l, ...changes }) : l)));

  const addLine = () =>
    update([
      ...lines,
      {
        key: newLineKey(lines),
        kind: 'item',
        parentKey: null,
        positionDescription: '',
        amount: 1,
        unit: 'pieces',
        unitPrice: 0,
        totalPrice: 0,
        sources: [],
      },
    ]);

  const diff = quotedGrandTotal != null ? Math.round((totals.grand - quotedGrandTotal) * 100) / 100 : null;

  const row = (line: Line, child: boolean) => {
    const active = isActive(line, lines);
    const hasSource = !!onShowSource && (!!line.sourceText || (line.sources?.length ?? 0) > 0);
    const adjLabel =
      line.adjustmentPercent == null ? 'Disc./Surch. %' : line.adjustmentPercent < 0 ? 'Discount %' : 'Surcharge %';
    const verb = line.kind === 'alternative' ? 'Replaces' : line.kind === 'optional' ? 'Add-on to' : 'Applies to';
    const inputCls = cn(
      'rounded-md border border-transparent bg-transparent px-1.5 py-1 text-sm tabular-nums',
      editable && 'hover:border-line focus:border-accent-deep focus:bg-white focus:outline-none',
    );

    return (
      <div
        key={line.key}
        className={cn('rounded-xl border border-line p-3', active ? 'bg-white' : 'bg-ink-800/[0.015]', child ? 'mb-1.5' : 'mb-2')}
      >
        <div className="flex items-center gap-2">
          <span className="min-w-7 font-mono text-[11px] text-ink/45">{line.position ?? '—'}</span>
          {editable ? (
            <select
              aria-label="Line type"
              value={line.kind}
              onChange={(e) => update(setKind(lines, line.key, e.target.value as LineKind))}
              className={cn('rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide', KIND_STYLE[line.kind])}
            >
              {LINE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          ) : (
            <span className={cn('rounded-full px-2 py-0.5 text-[10.5px] font-semibold uppercase tracking-wide', KIND_STYLE[line.kind])}>
              {line.kind}
            </span>
          )}
          <input
            aria-label="Description"
            disabled={!editable}
            value={line.positionDescription}
            placeholder="Description"
            onChange={(e) => patch(line.key, { positionDescription: e.target.value }, false)}
            className={cn(inputCls, 'min-w-0 flex-1 font-medium', !active && 'text-ink/55')}
          />
          {line.kind === 'alternative' && editable && line.parentKey && (
            <button
              type="button"
              onClick={() => update(swapAlternative(lines, line.key))}
              className="whitespace-nowrap rounded-full border border-line bg-white px-3 py-1 text-xs hover:border-ink-600/30"
            >
              Use this option
            </button>
          )}
          {hasSource && (
            <button
              type="button"
              title="Show in document"
              aria-label="Show in document"
              onClick={() => onShowSource!(line)}
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-line bg-white text-ink/60 hover:border-ink-600/30 hover:text-ink"
            >
              <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" className="h-3.5 w-3.5">
                <path d="M5 2.5h6.5L15 6v11.5H5z" />
                <path d="M11.5 2.5V6H15" />
                <circle cx="9.5" cy="11" r="2.2" />
                <path d="M11.2 12.7l1.8 1.8" />
              </svg>
            </button>
          )}
          {editable && (
            <button
              type="button"
              title="Remove line"
              aria-label="Remove line"
              onClick={() => update(removeLine(lines, line.key))}
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-ink/35 hover:bg-red-50 hover:text-red-600"
            >
              ✕
            </button>
          )}
        </div>

        <div className="mt-1.5 flex flex-wrap items-end gap-1.5">
          <Field label="Qty">
            <input
              disabled={!editable}
              inputMode="decimal"
              value={line.amount}
              onChange={(e) => patch(line.key, { amount: num(e.target.value) ?? 0 })}
              className={cn(inputCls, 'w-14 text-right')}
            />
          </Field>
          <Field label="Unit">
            <input
              disabled={!editable}
              value={line.unit}
              onChange={(e) => patch(line.key, { unit: e.target.value }, false)}
              className={cn(inputCls, 'w-20')}
            />
          </Field>
          <Field label="List price">
            <input
              disabled={!editable}
              inputMode="decimal"
              placeholder="—"
              value={line.listUnitPrice ?? ''}
              onChange={(e) => patch(line.key, { listUnitPrice: num(e.target.value) })}
              className={cn(inputCls, 'w-20 text-right')}
            />
          </Field>
          <Field label={adjLabel}>
            <input
              disabled={!editable}
              inputMode="decimal"
              placeholder="—"
              value={line.adjustmentPercent ?? ''}
              onChange={(e) => patch(line.key, { adjustmentPercent: num(e.target.value) })}
              className={cn(inputCls, 'w-16 text-right')}
            />
          </Field>
          <Field label="Unit price">
            <input
              disabled={!editable}
              inputMode="decimal"
              value={line.unitPrice}
              onChange={(e) =>
                patch(line.key, { unitPrice: num(e.target.value) ?? 0, listUnitPrice: null, adjustmentPercent: null })
              }
              className={cn(inputCls, 'w-24 text-right')}
            />
          </Field>
          <div
            className={cn(
              'ml-auto text-right text-[15px] font-semibold tabular-nums',
              !active && 'font-medium text-ink/45 line-through',
            )}
          >
            {usd(line.totalPrice)}
          </div>
        </div>

        {(child || line.kind !== 'item') && (
          <div className="mt-1.5 flex items-center gap-1.5 text-xs text-ink/55">
            {verb}
            <select
              disabled={!editable}
              value={line.parentKey ?? ''}
              onChange={(e) => patch(line.key, { parentKey: e.target.value || null }, false)}
              className={cn(inputCls, 'max-w-[18rem] text-xs')}
            >
              {line.kind !== 'alternative' && <option value="">Whole quote</option>}
              {line.kind === 'alternative' && !line.parentKey && <option value="">Choose a position…</option>}
              {itemLines
                .filter((i) => i.key !== line.key)
                .map((i) => (
                  <option key={i.key} value={i.key}>
                    Pos. {i.position ?? i.key} · {i.positionDescription.slice(0, 40)}
                  </option>
                ))}
            </select>
            {line.kind === 'alternative' || line.kind === 'optional'
              ? <span>· not in total</span>
              : !active && <span>· not in total (its position isn’t chosen)</span>}
          </div>
        )}

        {renderLineExtra?.(line)}
      </div>
    );
  };

  return (
    <div>
      {!editable && lockedReason && (
        <div className="mb-3 rounded-lg bg-ink-800/[0.05] px-3 py-2 text-xs text-ink/60">{lockedReason}</div>
      )}

      {lines.length === 0 ? (
        <div className="py-8 text-center text-sm text-ink/45">No order lines yet. Add one to get started.</div>
      ) : (
        <>
          {items.map(({ item, children }) => (
            <div key={item.key}>
              {row(item, false)}
              {children.length > 0 && (
                <div className="-mt-0.5 mb-2 ml-5 border-l-2 border-line pl-3">{children.map((c) => row(c, true))}</div>
              )}
            </div>
          ))}
          {quoteLevel.length > 0 && (
            <>
              <div className="mb-1.5 mt-4 text-[11px] uppercase tracking-wider text-ink/50">Quote-level charges</div>
              {quoteLevel.map((l) => row(l, false))}
            </>
          )}
        </>
      )}

      {editable && (
        <button
          type="button"
          onClick={addLine}
          className="mt-1 rounded-full border border-line bg-white px-4 py-1.5 text-sm hover:border-ink-600/30"
        >
          + Add line
        </button>
      )}

      <div className="mt-4 grid grid-cols-[1fr_auto] items-center gap-x-4 gap-y-1 border-t border-line pt-3 text-sm">
        <span className="text-right text-ink/55">Net</span>
        <span className="min-w-28 text-right tabular-nums">{usd(totals.net)}</span>
        <span className="text-right text-ink/55">Tax</span>
        <span className="text-right">
          <input
            aria-label="Tax"
            disabled={!editable}
            inputMode="decimal"
            value={taxTotal}
            onChange={(e) => onChange(lines, num(e.target.value) ?? 0)}
            className="w-28 rounded-md border border-transparent bg-transparent px-1.5 py-1 text-right tabular-nums hover:border-line focus:border-accent-deep focus:outline-none"
          />
        </span>
        <span className="text-right font-semibold text-ink">Grand total</span>
        <span className="text-right text-lg font-semibold tabular-nums">{usd(totals.grand)}</span>
        {diff != null && (
          <span
            className={cn(
              'col-span-2 mt-1 justify-self-end rounded-full px-2.5 py-0.5 text-xs',
              Math.abs(diff) < 0.01 ? 'bg-green-50 text-green-700' : 'bg-orange-50 text-amber-700',
            )}
          >
            {Math.abs(diff) < 0.01
              ? `Matches the quote's total ${usd(quotedGrandTotal!)}`
              : `${usd(Math.abs(diff))} ${diff < 0 ? 'below' : 'above'} the quoted ${usd(quotedGrandTotal!)}`}
          </span>
        )}
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col">
      <span className="pl-1.5 text-[10px] uppercase tracking-wider text-ink/50">{label}</span>
      {children}
    </label>
  );
}
