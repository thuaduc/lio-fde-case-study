'use client';

import { useEffect, useRef, useState } from 'react';
import * as api from '@/lib/api';
import type { Line } from '@/lib/orderLines';

/**
 * Catalog suggestions per line key, refreshed (debounced) when a line's
 * description, unit or price changes. Only item lines not already linked to an
 * article are matched; nothing is fetched while `enabled` is false.
 */
export function useCatalogSuggestions(lines: Line[], enabled: boolean) {
  const [byKey, setByKey] = useState<Record<string, api.ArticleSuggestion[]>>({});
  const seq = useRef(0);

  const candidates = lines.filter(
    (l) => !l.removed && l.kind === 'item' && !l.articleId && l.positionDescription.trim().length >= 3,
  );
  const signature = JSON.stringify(
    candidates.map((l) => [l.key, l.positionDescription, l.unit, l.unitPrice, l.vendorItemNumber ?? null]),
  );

  useEffect(() => {
    if (!enabled || candidates.length === 0) {
      setByKey({});
      return;
    }
    const mine = ++seq.current;
    const t = setTimeout(async () => {
      try {
        const res = await api.articles.suggestions(
          candidates.map((l) => ({
            description: l.positionDescription,
            unit: l.unit || null,
            unitPrice: l.unitPrice || null,
            vendorItemNumber: l.vendorItemNumber ?? null,
          })),
        );
        if (mine !== seq.current) return; // a newer request superseded this one
        const next: Record<string, api.ArticleSuggestion[]> = {};
        for (const r of res) {
          const line = candidates[r.lineIndex];
          if (line && r.suggestions.length > 0) next[line.key] = r.suggestions;
        }
        setByKey(next);
      } catch {
        // Suggestions are optional; the form works without them.
      }
    }, 600);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signature, enabled]);

  return byKey;
}
