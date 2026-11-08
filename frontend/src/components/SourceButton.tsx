'use client';

import { cn } from '@/lib/utils';

/** Eye button that shows where something came from in the document panel. */
export default function SourceButton({
  onClick,
  label = 'Show in document',
  className,
}: {
  onClick: () => void;
  label?: string;
  className?: string;
}) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation();
        onClick();
      }}
      className={cn(
        'inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-ink/45 hover:bg-ink-800/[0.06] hover:text-ink',
        className,
      )}
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
        className="h-[18px] w-[18px]"
      >
        <path d="M2.5 12S6 5 12 5s9.5 7 9.5 7-3.5 7-9.5 7S2.5 12 2.5 12z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
    </button>
  );
}
