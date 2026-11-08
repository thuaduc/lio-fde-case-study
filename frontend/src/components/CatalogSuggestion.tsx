'use client';

import { useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import * as api from '@/lib/api';
import { type Line, recalcLine } from '@/lib/orderLines';

const usd = (n: number) => new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' }).format(n);
const href = (id: string) => `/dashboard/articles/${encodeURIComponent(id)}`;
const sameUnit = (a: string, b: string) => a.trim().toLowerCase() === b.trim().toLowerCase();

export function applyArticle(line: Line, s: api.ArticleSuggestion, amount = line.amount): Line {
  return recalcLine({
    ...line,
    originalQuote: line.originalQuote ?? (line.articleId ? null : {
      positionDescription: line.positionDescription, unitPrice: line.unitPrice,
      amount: line.amount, unit: line.unit, vendorItemNumber: line.vendorItemNumber,
      listUnitPrice: line.listUnitPrice, adjustmentPercent: line.adjustmentPercent,
    }),
    articleId: s.articleId, positionDescription: s.description,
    vendorItemNumber: s.articleNumber, amount, unitPrice: s.unitPrice, unit: s.unit,
    listUnitPrice: null, adjustmentPercent: null,
  });
}

export function restoreArticle(line: Line): Line {
  return line.originalQuote
    ? recalcLine({ ...line, ...line.originalQuote, originalQuote: null, articleId: null })
    : line;
}

export default function CatalogSuggestion({ line, suggestions, applied, editable, onApply, onRestore, currency = 'USD' }: {
  line: Line;
  currency?: string;
  suggestions?: api.ArticleSuggestion[];
  applied?: api.ArticleSuggestion;
  editable: boolean;
  onApply: (s: api.ArticleSuggestion, amount: number) => void;
  onRestore: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<api.Article | null>(null);
  const [search, setSearch] = useState('');
  const [supplier, setSupplier] = useState('');
  const [suppliers, setSuppliers] = useState<api.Supplier[]>([]);
  const [page, setPage] = useState<api.ArticlePage | null>(null);
  const [offset, setOffset] = useState(0);
  const [quantity, setQuantity] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);
  useEffect(() => {
    if (!open) return;
    let active = true;
    api.suppliers.list().then((s) => { if (active) setSuppliers(s); }).catch(() => {});
    return () => { active = false; };
  }, [open]);
  useEffect(() => {
    if (!open || selected) return;
    let active = true;
    setLoading(true); setError(''); setPage(null);
    const timer = setTimeout(() => {
      api.articles.list({ search, supplierId: supplier, offset, limit: 20 })
        .then((p) => { if (active) setPage(p); })
        .catch((e) => { if (active) setError(e.message || 'Could not load catalog.'); })
        .finally(() => { if (active) setLoading(false); });
    }, 250);
    return () => { active = false; clearTimeout(timer); };
  }, [open, selected, search, supplier, offset]);

  // A line can change or become locked while the dialog is open.
  useEffect(() => { setOpen(false); setSelected(null); }, [line.articleId, line.amount, line.unit, line.unitPrice, editable]);
  function choose(a: api.Article) {
    setSelected(a); setError('');
    setQuantity(sameUnit(line.unit, a.unit) ? String(line.amount) : '');
  }
  function browse() {
    setSelected(null); setError(''); setSearch(''); setSupplier(''); setOffset(0); setOpen(true);
  }
  async function reviewSuggestion(id: string) {
    setError('');
    try { choose(await api.articles.get(id)); setOpen(true); }
    catch (e) { setError(e instanceof Error ? e.message : 'Could not load article.'); }
  }
  function replace() {
    if (!selected || !editable) return;
    const amount = Number(quantity);
    if (!Number.isFinite(amount) || amount <= 0) { setError('Enter a quantity greater than zero.'); return; }
    if (selected.currency !== currency) { setError(`This request uses ${currency}. Choose a ${currency} catalog article.`); return; }
    onApply({ articleId: selected.id, articleNumber: selected.article_number,
      supplierName: selected.supplier_name, description: selected.description,
      unitPrice: Number(selected.unit_price), unit: selected.unit,
      quantity: Number(selected.quantity), matchType: 'similar', savingPerUnit: null,
    }, amount);
    setOpen(false); setSelected(null);
  }
  if (line.removed || (!line.articleId && !editable)) return null;
  const canReplace = editable && ['item', 'alternative', 'optional'].includes(line.kind);
  return (
    <div className="mt-2 space-y-2 text-sm" onClick={(e) => e.stopPropagation()}>
      {line.articleId && <p className="text-accent-deep">
        Catalog: <Link className="font-medium underline underline-offset-2" href={href(line.articleId)} target="_blank" rel="noopener noreferrer">
          {applied?.articleNumber || line.vendorItemNumber || 'View catalog article'} ↗
        </Link>{applied?.supplierName && <span className="text-ink/55"> · {applied.supplierName}</span>}
      </p>}
      {canReplace && <div className="flex flex-wrap items-center gap-3">
        <button type="button" className="rounded-lg border border-line px-3 py-1.5 font-medium hover:bg-canvas" onClick={browse}>Replace from catalog</button>
        {line.originalQuote && <button type="button" className="text-accent-deep hover:underline" onClick={onRestore}>Restore quoted item</button>}
      </div>}
      {canReplace && !line.articleId && suggestions?.map((s) => <div key={s.articleId} className="rounded-lg bg-accent-soft/20 px-3 py-2">
        <span className="text-ink/55">{s.matchType === 'same' ? 'Catalog match' : 'Similar item'}: </span>
        <Link href={href(s.articleId)} className="text-accent-deep underline" target="_blank" rel="noopener noreferrer">{s.articleNumber} · {s.description} ↗</Link>
        <span> · {usd(s.unitPrice)}/{s.unit}</span>
        <button type="button" onClick={() => reviewSuggestion(s.articleId)} className="ml-3 font-medium text-accent-deep hover:underline">Review replacement</button>
      </div>)}
      {!open && error && <p role="alert" className="text-red-700">{error}</p>}
      <dialog ref={dialog} onCancel={() => setOpen(false)} onClose={() => setOpen(false)} aria-labelledby={`catalog-title-${line.key}`}
        className="fixed inset-0 m-auto max-h-[85vh] w-[min(720px,95vw)] overflow-y-auto rounded-2xl border border-line bg-white p-6 text-ink shadow-xl backdrop:bg-ink/40">
        <div className="mb-4 flex items-center justify-between gap-4">
          <h2 id={`catalog-title-${line.key}`} className="text-lg font-semibold">{selected ? 'Review replacement' : 'Choose a catalog article'} · {line.position || line.key}</h2>
          <button type="button" onClick={() => setOpen(false)} aria-label="Close catalog" className="rounded-lg border border-line px-3 py-1">✕</button>
        </div>
        {selected ? <>
          <button type="button" onClick={() => setSelected(null)} className="mb-4 text-accent-deep hover:underline">← Back to catalog</button>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="rounded-xl bg-canvas p-4"><p className="mb-2 text-xs uppercase text-ink/50">Current item</p><p className="font-medium">{line.positionDescription}</p><p className="mt-2 text-ink/60">{line.amount} {line.unit} × {usd(line.unitPrice)}</p><p className="mt-2 font-semibold">{usd(line.totalPrice)}</p></div>
            <div className="rounded-xl bg-accent-soft/20 p-4"><p className="mb-2 text-xs uppercase text-ink/50">Catalog replacement</p><p className="font-medium">{selected.description}</p><Link href={href(selected.id)} target="_blank" rel="noopener noreferrer" className="text-accent-deep underline">{selected.article_number} ↗</Link><p className="mt-2 text-ink/60">{selected.supplier_name}</p><p className="mt-2 font-semibold">{selected.currency} {Number(selected.unit_price).toFixed(2)} / {selected.unit}</p></div>
          </div>
          {!sameUnit(line.unit, selected.unit) && <p className="my-4 rounded-lg bg-amber-50 p-3 text-amber-900">The unit changes from {line.unit || 'unspecified'} to {selected.unit}. Enter the new quantity; the quoted quantity is not converted.</p>}
          <label className="mt-4 block">New quantity ({selected.unit})<input type="number" min="0" step="any" value={quantity} onChange={(e) => setQuantity(e.target.value)} className="mt-1 block w-full rounded-lg border border-line px-3 py-2" /></label>
          <p className="mt-4 flex justify-between">New line total <strong>{Number(quantity) > 0 ? `${selected.currency} ${(Number(quantity) * Number(selected.unit_price)).toFixed(2)}` : '—'}</strong></p>
          <p className="mt-3 text-xs text-ink/55">Replaces the description, article number, supplier reference, unit and unit price. Original quoted values and PDF sources remain available. Save the request to keep changes.</p>
          <div className="mt-4 flex justify-end gap-3"><button type="button" onClick={() => setOpen(false)} className="rounded-lg border border-line px-4 py-2">Cancel</button><button type="button" onClick={replace} className="rounded-lg bg-accent-deep px-4 py-2 font-medium text-white">Replace this line</button></div>
        </> : <>
          <div className="mb-3 grid gap-3 sm:grid-cols-2"><input aria-label="Search catalog" value={search} onChange={(e) => { setSearch(e.target.value); setOffset(0); }} placeholder="Search article name or number" className="rounded-lg border border-line px-3 py-2" /><select aria-label="Filter by supplier" value={supplier} onChange={(e) => { setSupplier(e.target.value); setOffset(0); }} className="rounded-lg border border-line px-3 py-2"><option value="">All suppliers</option>{suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></div>
          {loading && <p role="status">Loading catalog…</p>}
          {!loading && page?.items.length === 0 && <p>No articles found. Try another search or supplier.</p>}
          <div className="space-y-2">{page?.items.map((a) => <button type="button" key={a.id} onClick={() => choose(a)} className="flex w-full items-start justify-between gap-4 rounded-xl border border-line p-3 text-left hover:bg-canvas"><span><span className="font-medium">{a.description}</span><span className="mt-1 block text-xs text-ink/55">{a.article_number} · {a.supplier_name}</span></span><span className="shrink-0 text-right">{a.currency} {Number(a.unit_price).toFixed(2)}<span className="block text-xs text-ink/55">/ {a.unit} →</span></span></button>)}</div>
          {page && <div className="mt-4 flex items-center justify-between"><button type="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 20))} className="disabled:opacity-30">Previous</button><span className="text-xs text-ink/55">{page.total ? offset + 1 : 0}–{offset + page.items.length} of {page.total}</span><button type="button" disabled={offset + page.items.length >= page.total} onClick={() => setOffset(offset + 20)} className="disabled:opacity-30">Next</button></div>}
        </>}
        {open && error && <p role="alert" className="mt-3 text-red-700">{error}</p>}
      </dialog>
    </div>
  );
}
