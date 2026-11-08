'use client';

import type { SourceBox, WarningDetail } from '@/lib/api';
import SourceButton from '@/components/SourceButton';
import { cn } from '@/lib/utils';

/** Accepts the new `{message, sources}` shape and legacy plain strings. */
export function normalizeWarnings(
  items: (WarningDetail | string)[] | null | undefined,
): WarningDetail[] {
  return (items ?? []).map((w) => (typeof w === 'string' ? { message: w, sources: [] } : w));
}

/** Extraction warnings, each with an eye button when its source is located in the PDF. */
export default function WarningList({
  warnings,
  title,
  onShowSource,
  className,
}: {
  warnings: WarningDetail[];
  title?: string;
  onShowSource?: (sources: SourceBox[]) => void;
  className?: string;
}) {
  if (warnings.length === 0) return null;
  return (
    <div className={cn('rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-800', className)}>
      {title && <p className="mb-1 font-medium">{title}</p>}
      <ul className="space-y-1">
        {warnings.map((w, i) => (
          <li key={`${i}-${w.message}`} className="flex items-start gap-2">
            <span aria-hidden className="mt-[7px] h-1.5 w-1.5 shrink-0 rounded-full bg-amber-700/70" />
            <span className="flex-1">{w.message}</span>
            {onShowSource && (w.sources?.length ?? 0) > 0 && (
              <SourceButton
                className="-my-1 h-7 w-7 text-amber-800/70 hover:bg-amber-100 hover:text-amber-900"
                label="Show this in the document"
                onClick={() => onShowSource(w.sources)}
              />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}
