'use client';

import { useState } from 'react';
import type { ArticleSuggestion } from '@/lib/api';
import { type Line, recalcLine } from '@/lib/orderLines';

const usd = (n: number) =>
  (n < 0 ? '−$' : '$') + Math.abs(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** What a line looks like after switching to a catalog article (negotiated price). */
export function applyArticle(line: Line, s: ArticleSuggestion): Line {
  return recalcLine({
    ...line,
    articleId: s.articleId,
    unitPrice: s.unitPrice,
    unit: s.unit || line.unit,
    listUnitPrice: null,
    adjustmentPercent: null,
  });
}

/**
 * Under a line: the best catalog match (more on request) with the saving, and a
 * "Use catalog price" action. A line already on a catalog article shows which.
 */
export default function CatalogSuggestion({
  line,
  suggestions,
  applied,
  editable,
  onApply,
}: {
  line: Line;
  suggestions?: ArticleSuggestion[];
  /** Article details for lines switched in this session (supplier and number). */
  applied?: ArticleSuggestion;
  editable: boolean;
  onApply: (s: ArticleSuggestion) => void;
}) {
  const [showAll, setShowAll] = useState(false);

  if (line.articleId) {
    return (
      <p className="mt-2 text-sm text-accent-deep" onClick={(e) => e.stopPropagation()}>
        Catalog price
        {applied && (
          <span className="text-ink/55">
            {' '}
            · {applied.articleNumber}
            {applied.supplierName ? ` · ${applied.supplierName}` : ''}
          </span>
        )}
      </p>
    );
  }
  if (!editable || !suggestions?.length) return null;

  const shown = showAll ? suggestions : suggestions.slice(0, 1);
  return (
    <div
      className="mt-2 space-y-1.5 rounded-lg border border-accent-deep/15 bg-accent-soft/20 px-3 py-2 text-sm"
      onClick={(e) => e.stopPropagation()}
    >
      {shown.map((s) => {
        const saving = s.savingPerUnit != null ? s.savingPerUnit * (line.amount || 1) : null;
        return (
          <div key={s.articleId} className="flex flex-wrap items-center justify-between gap-2">
            <p className="min-w-0 flex-1 text-ink/70">
              <span className="font-medium text-accent-deep">
                {s.matchType === 'same' ? 'Catalog match' : 'Similar catalog item'}
              </span>
              {' · '}
              <span className="font-medium text-ink">{s.articleNumber}</span> {s.description}
              {s.supplierName ? ` · ${s.supplierName}` : ''}
              {' · '}
              <span className="font-medium text-ink">{usd(s.unitPrice)}</span>/{s.unit}
              {saving != null && saving > 0 && <span className="text-green-700"> · saves {usd(saving)}</span>}
              {saving != null && saving < 0 && <span className="text-ink/45"> · {usd(-saving)} more</span>}
            </p>
            <button
              type="button"
              onClick={() => onApply(s)}
              className="shrink-0 rounded-full bg-accent-deep px-3 py-1 text-xs font-medium text-white hover:brightness-110"
            >
              Use catalog price
            </button>
          </div>
        );
      })}
      {suggestions.length > 1 && (
        <button
          type="button"
          onClick={() => setShowAll((v) => !v)}
          className="text-xs font-medium text-accent-deep hover:underline"
        >
          {showAll ? 'Show fewer' : `${suggestions.length - 1} more catalog option${suggestions.length > 2 ? 's' : ''}`}
        </button>
      )}
    </div>
  );
}
