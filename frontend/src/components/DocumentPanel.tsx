'use client';

import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from 'react';
import 'pdfjs-dist/web/pdf_viewer.css';
import type { SourceBox } from '@/lib/api';
import { cn } from '@/lib/utils';

/**
 * Side-by-side PDF viewer built on pdf.js's own viewer components, so the page
 * can drive it: `showSource(line)` outlines the line's stored source boxes
 * (from the extraction's PDF row coordinates) and scrolls them into view.
 * The toolbar search box is a plain manual search.
 */

export interface DocumentPanelHandle {
  showSource(line: { sources?: SourceBox[] }): void;
}

interface Props {
  fileUrl: string;
  title?: string;
  className?: string;
}

/* eslint-disable @typescript-eslint/no-explicit-any -- pdf.js viewer objects are loaded dynamically */
type Viewer = {
  pdfjs: any;
  viewer: any;
  eventBus: any;
  doc: any;
};

const DocumentPanel = forwardRef<DocumentPanelHandle, Props>(function DocumentPanel(
  { fileUrl, title, className },
  ref,
) {
  const containerRef = useRef<HTMLDivElement>(null);
  const viewerDivRef = useRef<HTMLDivElement>(null);
  const v = useRef<Viewer | null>(null);
  const boxesRef = useRef<SourceBox[]>([]);
  const [page, setPage] = useState(1);
  const [pageCount, setPageCount] = useState(0);
  const [query, setQuery] = useState('');
  const [matches, setMatches] = useState<{ current: number; total: number } | null>(null);
  const [error, setError] = useState('');

  const drawBoxes = useCallback(() => {
    const viewer = v.current?.viewer;
    if (!viewer) return;
    document.querySelectorAll('.lio-source-box').forEach((el) => el.remove());
    for (const b of boxesRef.current) {
      const pageView = viewer.getPageView(b.page - 1);
      if (!pageView?.div) continue;
      const el = document.createElement('div');
      el.className = 'lio-source-box';
      Object.assign(el.style, {
        position: 'absolute',
        left: `${b.x0 * 100}%`,
        top: `${b.y0 * 100}%`,
        width: `${(b.x1 - b.x0) * 100}%`,
        height: `${(b.y1 - b.y0) * 100}%`,
        outline: '2px solid #0c6f80',
        background: 'rgba(111,199,216,0.18)',
        borderRadius: '3px',
        pointerEvents: 'none',
        zIndex: '5',
      });
      pageView.div.appendChild(el);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        setError('');
        const pdfjs: any = await import('pdfjs-dist');
        pdfjs.GlobalWorkerOptions.workerSrc = new URL(
          'pdfjs-dist/build/pdf.worker.min.mjs',
          import.meta.url,
        ).toString();
        const viewerMod: any = await import('pdfjs-dist/web/pdf_viewer.mjs');
        if (cancelled || !containerRef.current || !viewerDivRef.current) return;

        const eventBus = new viewerMod.EventBus();
        const linkService = new viewerMod.PDFLinkService({ eventBus });
        const findController = new viewerMod.PDFFindController({ eventBus, linkService });
        const viewer = new viewerMod.PDFViewer({
          container: containerRef.current,
          viewer: viewerDivRef.current,
          eventBus,
          linkService,
          findController,
        });
        linkService.setViewer(viewer);

        eventBus.on('pagesinit', () => {
          viewer.currentScaleValue = 'page-width';
        });
        eventBus.on('pagechanging', (e: any) => setPage(e.pageNumber));
        eventBus.on('pagerendered', () => drawBoxes());
        eventBus.on('updatefindmatchescount', (e: any) => setMatches(e.matchesCount));
        eventBus.on('updatefindcontrolstate', (e: any) => {
          if (e.matchesCount) setMatches(e.matchesCount);
        });

        const doc = await pdfjs.getDocument(fileUrl).promise;
        if (cancelled) {
          doc.destroy();
          return;
        }
        viewer.setDocument(doc);
        linkService.setDocument(doc);
        setPageCount(doc.numPages);
        setPage(1);
        v.current = { pdfjs, viewer, eventBus, doc };
      } catch (err) {
        console.error('PDF viewer failed', err);
        if (!cancelled) setError('Could not display this PDF.');
      }
    })();
    return () => {
      cancelled = true;
      v.current?.doc?.destroy();
      v.current = null;
      boxesRef.current = [];
    };
  }, [fileUrl, drawBoxes]);

  const find = useCallback((text: string, findPrevious = false, type = '') => {
    const cur = v.current;
    if (!cur || !text.trim()) return;
    cur.eventBus.dispatch('find', {
      source: null,
      type,
      query: text.trim(),
      caseSensitive: false,
      entireWord: false,
      highlightAll: true,
      findPrevious,
      matchDiacritics: false,
    });
  }, []);

  useImperativeHandle(
    ref,
    () => ({
      showSource(line) {
        const cur = v.current;
        if (!cur) return;
        boxesRef.current = line.sources ?? [];
        const first = boxesRef.current[0];
        if (!first) return;
        containerRef.current
          ?.closest('[data-document-panel]')
          ?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        cur.viewer.scrollPageIntoView({ pageNumber: first.page });
        // Page divs exist for every page (rendered lazily), so the overlay can be
        // placed right away and scrolled to the middle of the viewer.
        requestAnimationFrame(() => {
          drawBoxes();
          document.querySelector('.lio-source-box')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        });
      },
    }),
    [drawBoxes],
  );

  const zoom = (factor: number) => {
    const viewer = v.current?.viewer;
    if (viewer) viewer.currentScale = Math.min(4, Math.max(0.3, viewer.currentScale * factor));
  };

  const btn =
    'inline-flex h-7 min-w-7 items-center justify-center rounded-md px-1.5 text-sm text-ink/60 hover:bg-ink-800/[0.06] hover:text-ink disabled:opacity-40';

  return (
    <div data-document-panel className={cn('lio-card p-4', className)}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="truncate text-lg font-semibold tracking-tight text-ink">{title ?? 'Original Document'}</h2>
      </div>

      <div className="mb-2 flex flex-wrap items-center gap-1 rounded-lg border border-line bg-canvas px-2 py-1">
        <button type="button" className={btn} disabled={page <= 1} onClick={() => v.current?.viewer && (v.current.viewer.currentPageNumber = page - 1)} aria-label="Previous page">
          ‹
        </button>
        <span className="text-xs tabular-nums text-ink/60">
          {page} / {pageCount || '…'}
        </span>
        <button type="button" className={btn} disabled={page >= pageCount} onClick={() => v.current?.viewer && (v.current.viewer.currentPageNumber = page + 1)} aria-label="Next page">
          ›
        </button>
        <span className="mx-1 h-4 w-px bg-line" />
        <button type="button" className={btn} onClick={() => zoom(1 / 1.15)} aria-label="Zoom out">
          −
        </button>
        <button type="button" className={btn} onClick={() => zoom(1.15)} aria-label="Zoom in">
          +
        </button>
        <button type="button" className={cn(btn, 'text-xs')} onClick={() => v.current?.viewer && (v.current.viewer.currentScaleValue = 'page-width')}>
          Fit
        </button>
        <span className="mx-1 h-4 w-px bg-line" />
        <form
          className="flex min-w-0 flex-1 items-center gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            find(query, false, 'again');
          }}
        >
          <input
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              find(e.target.value);
            }}
            placeholder="Search document"
            className="min-w-0 flex-1 rounded-md border border-line bg-white px-2 py-1 text-xs focus:border-accent-deep focus:outline-none"
          />
          <button type="button" className={btn} onClick={() => find(query, true, 'again')} aria-label="Previous match">
            ↑
          </button>
          <button type="submit" className={btn} aria-label="Next match">
            ↓
          </button>
          {query && matches && (
            <span className="whitespace-nowrap text-[11px] text-ink/50">
              {matches.total ? `${matches.current} of ${matches.total}` : 'No match'}
            </span>
          )}
        </form>
      </div>

      <div className="relative h-[75vh] min-h-[480px] overflow-hidden rounded-lg border border-line bg-[#e9ecef]">
        {error ? (
          <div className="flex h-full items-center justify-center text-sm text-ink/50">{error}</div>
        ) : (
          <div ref={containerRef} className="absolute inset-0 overflow-auto">
            <div ref={viewerDivRef} className="pdfViewer" />
          </div>
        )}
      </div>
    </div>
  );
});

export default DocumentPanel;
