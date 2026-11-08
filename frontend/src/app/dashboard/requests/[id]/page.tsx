'use client';

import { useEffect, useRef, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import Link from 'next/link';
import * as api from '@/lib/api';
import { Button, Input, Alert } from '@/components/base';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import type { ProcurementRequestWithDetails } from '@/types/database';
import OrderLinesEditor from '@/components/OrderLinesEditor';
import SourceButton from '@/components/SourceButton';
import WarningList, { normalizeWarnings } from '@/components/WarningList';
import CatalogSuggestion, { applyArticle } from '@/components/CatalogSuggestion';
import { useCatalogSuggestions } from '@/lib/useCatalogSuggestions';
import DocumentPanel, { type DocumentPanelHandle } from '@/components/DocumentPanel';
import {
  type Line,
  linesForSave,
  linesFromRequest as toEditorLines,
  missingGroupError,
  primaryGroupId,
} from '@/lib/orderLines';
import { approvalLabel } from '@/lib/approval';

const STATUS_BADGE: Record<string, string> = {
  open: 'bg-green-100 text-green-800',
  in_progress: 'bg-amber-100 text-amber-800',
  closed: 'bg-ink-800/[0.08] text-ink/70',
};
const APPROVAL_BADGE: Record<string, string> = {
  approved: 'bg-green-100 text-green-800',
  rejected: 'bg-red-100 text-red-700',
  pending: 'bg-amber-100 text-amber-800',
};
const titleCase = (s: string) => s.replace('_', ' ').replace(/\b\w/g, (l) => l.toUpperCase());
const usd = (n: number | null) =>
  `$${(n || 0).toLocaleString('en-US', { minimumFractionDigits: 2 })}`;

export default function RequestDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { data: commodityGroups } = useCommodityGroups();

  const [request, setRequest] = useState<ProcurementRequestWithDetails | null>(null);
  const [activity, setActivity] = useState<api.RequestActivity[]>([]);
  const [role, setRole] = useState<api.MemberRole | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [lines, setLines] = useState<Line[]>([]);
  const [taxTotal, setTaxTotal] = useState(0);
  const [linesDirty, setLinesDirty] = useState(false);
  const docPanel = useRef<DocumentPanelHandle>(null);
  const linesEditable = !!request && request.status !== 'closed';
  const { byKey: catalog, loading: catalogLoading } = useCatalogSuggestions(lines, linesEditable);
  const [appliedArticles, setAppliedArticles] = useState<Record<string, api.ArticleSuggestion>>({});

  /** Click handler that highlights one header field, when its location is known. */
  const showField = (key: keyof api.FieldSources) => {
    const boxes = request?.fieldSources?.[key] ?? [];
    return pdfUrl && boxes.length > 0
      ? () => docPanel.current?.showSource({ sources: boxes })
      : undefined;
  };

  const adopt = (req: ProcurementRequestWithDetails) => {
    setRequest(req);
    setLines(toEditorLines(req));
    setTaxTotal(req.taxTotal ?? 0);
    setLinesDirty(false);
  };

  // Edit form state
  const [form, setForm] = useState({
    requestorName: '', title: '', vendorName: '', vatId: '', department: '',
    commodityGroupId: null as number | null, totalCost: 0,
  });

  const load = async () => {
    try {
      const [req, org, acts] = await Promise.all([
        api.requests.get(id),
        api.organizations.getMine(),
        api.requests.getActivity(id),
      ]);
      adopt(req);
      setRole(org.role);
      setActivity(acts);
      const url = await api.requests.documentUrl(id).catch(() => null);
      setPdfUrl(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load request');
    }
    setLoading(false);
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  // Revoke the object URL when it changes / unmounts.
  useEffect(() => {
    return () => {
      if (pdfUrl) URL.revokeObjectURL(pdfUrl);
    };
  }, [pdfUrl]);

  const refresh = async () => {
    const [req, acts] = await Promise.all([
      api.requests.get(id),
      api.requests.getActivity(id),
    ]);
    adopt(req);
    setActivity(acts);
  };

  const changeStatus = async (status: 'open' | 'in_progress' | 'closed') => {
    setBusy(true); setError(''); setSuccess('');
    try {
      await api.requests.updateStatus(id, status);
      setSuccess('Status updated.');
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update status');
    }
    setBusy(false);
  };

  const decide = async (decision: 'approved' | 'rejected') => {
    setBusy(true); setError(''); setSuccess('');
    try {
      await api.requests.decideApproval(id, decision);
      setSuccess(`Request ${decision}.`);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to submit decision');
    }
    setBusy(false);
  };

  const remove = async () => {
    if (!confirm('Delete this request? This cannot be undone.')) return;
    setBusy(true);
    try {
      await api.requests.delete(id);
      router.push('/dashboard/requests');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete request');
      setBusy(false);
    }
  };

  const openEdit = () => {
    if (!request) return;
    setForm({
      requestorName: request.requestor_name,
      title: request.title,
      vendorName: request.vendor_name,
      vatId: request.vat_id || '',
      department: request.department || '',
      commodityGroupId: request.commodity_group_id,
      totalCost: request.total_cost || 0,
    });
    setEditing(true);
  };

  /** Full update payload: header fields from `header`, lines + tax from the editor. */
  const updatePayload = (header: typeof form, editorLines: Line[], tax: number) => ({
    requestorName: header.requestorName,
    title: header.title,
    vendorName: header.vendorName,
    vatId: header.vatId,
    // Groups are booked per line; the server derives the request's from them.
    commodityGroupId: primaryGroupId(editorLines) ?? header.commodityGroupId ?? null,
    totalCost: header.totalCost,
    department: header.department,
    taxTotal: tax,
    fieldSources: request?.fieldSources ?? null,
    extractionWarnings: request ? normalizeWarnings(request.extractionWarnings) : null,
    quoteNumber: request?.quoteNumber ?? null,
    validUntil: request?.validUntil ?? null,
    orderLines: linesForSave(editorLines),
  });

  const headerFromRequest = () => ({
    requestorName: request!.requestor_name,
    title: request!.title,
    vendorName: request!.vendor_name,
    vatId: request!.vat_id || '',
    department: request!.department || '',
    commodityGroupId: request!.commodity_group_id,
    totalCost: request!.total_cost || 0,
  });

  const saveEdit = async () => {
    if (!request) return;
    setBusy(true); setError(''); setSuccess('');
    try {
      // Send the stored lines back unchanged (all rich fields included).
      await api.requests.update(id, updatePayload(form, toEditorLines(request), request.taxTotal ?? 0));
      setEditing(false);
      setSuccess('Request updated.');
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update request');
    }
    setBusy(false);
  };

  const saveLines = async () => {
    if (!request) return;
    setError(''); setSuccess('');
    const groupError = missingGroupError(linesForSave(lines));
    if (groupError) {
      setError(groupError);
      return;
    }
    setBusy(true);
    try {
      const wasApproved = request.approval_status === 'approved';
      const saved = await api.requests.update(id, updatePayload(headerFromRequest(), lines, taxTotal));
      await refresh();
      setSuccess(
        saved.auto_approved && !request.auto_approved
          ? 'Lines saved. The new total is below the approval threshold, so the request was approved automatically.'
          : wasApproved && saved.approval_status === 'pending'
            ? 'Lines saved. The total changed, so the request is open and pending approval again.'
            : 'Lines saved.',
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save lines');
    }
    setBusy(false);
  };

  if (loading) {
    return (
      <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <div className="flex items-center justify-center h-64">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-accent-deep"></div>
        </div>
      </div>
    );
  }

  if (!request) {
    return (
      <div className="max-w-3xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
        <Alert variant="error">{error || 'Request not found'}</Alert>
        <Link href="/dashboard/requests" className="text-accent-deep hover:underline text-sm mt-4 inline-block">
          ← Back to requests
        </Link>
      </div>
    );
  }

  const canApprove = (role === 'buyer' || role === 'admin') && request.approval_status === 'pending';
  // The threshold that applied when the request was auto-approved (newest entry).
  const autoApprovedAt = request.auto_approved
    ? activity.find((a) => a.action === 'auto_approved')?.detail?.threshold
    : undefined;
  const autoThreshold = typeof autoApprovedAt === 'number' ? autoApprovedAt : null;
  const showTitle = showField('title');

  return (
    <div className="max-w-[1600px] mx-auto px-4 sm:px-6 lg:px-8 py-8">
      <Link href="/dashboard/requests" className="text-sm text-ink/55 hover:text-ink">
        ← Back to requests
      </Link>

      {/* Header */}
      <div className="mt-3 mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="text-3xl font-semibold tracking-tight text-ink">{request.title}</h1>
            {showTitle && <SourceButton className="h-7 w-7" label="Show title in document" onClick={showTitle} />}
          </div>
          <div className="flex items-center gap-2 mt-2">
            <span className={`inline-flex px-2.5 py-1 text-xs font-semibold rounded-full ${STATUS_BADGE[request.status]}`}>
              {titleCase(request.status)}
            </span>
            <span className={`inline-flex px-2.5 py-1 text-xs font-semibold rounded-full ${APPROVAL_BADGE[request.approval_status]}`}>
              {approvalLabel(request)}
            </span>
            {autoThreshold !== null && (
              <span className="text-sm text-ink/55">below the {usd(autoThreshold)} threshold</span>
            )}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {canApprove && (
            <>
              <Button size="sm" onClick={() => decide('approved')} disabled={busy}>Approve</Button>
              <Button size="sm" variant="outline" className="text-red-600 border-red-300 hover:bg-red-50"
                onClick={() => decide('rejected')} disabled={busy}>Reject</Button>
            </>
          )}
          <Button size="sm" variant="outline" onClick={openEdit} disabled={busy}>Edit</Button>
          <Button size="sm" variant="outline" className="text-red-600 border-red-300 hover:bg-red-50"
            onClick={remove} disabled={busy}>Delete</Button>
        </div>
      </div>

      {error && <Alert variant="error" className="mb-6">{error}</Alert>}
      {success && <Alert variant="success" className="mb-6">{success}</Alert>}

      {/* Status control */}
      <div className="lio-card p-4 mb-6 flex flex-wrap items-center gap-3">
        <span className="text-sm font-medium text-ink/70">Status:</span>
        {(['open', 'in_progress', 'closed'] as const).map((s) => (
          <button
            key={s}
            onClick={() => changeStatus(s)}
            disabled={busy || request.status === s}
            className={`px-4 py-2 rounded-full text-sm font-medium transition-colors disabled:opacity-100 ${
              request.status === s
                ? 'bg-ink text-white'
                : 'bg-ink-800/[0.05] text-ink/70 hover:bg-ink-800/10'
            }`}
          >
            {titleCase(s)}
          </button>
        ))}
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left column: details, order lines, activity */}
        <div className="space-y-6">
          <div className="lio-card p-6">
            <h2 className="text-lg font-semibold tracking-tight text-ink mb-4">Details</h2>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-4 text-sm">
              <Field label="Requestor" value={request.requestor_name} onShow={showField('requestorName')} />
              <Field label="Department" value={request.department || '—'} onShow={showField('department')} />
              <Field label="Vendor" value={request.vendor_name} onShow={showField('vendorName')} />
              <Field label="Tax ID" value={request.vat_id || '—'} onShow={showField('vatId')} />
              <Field label="Quote number" value={request.quoteNumber || '—'} onShow={showField('quoteNumber')} />
              <Field
                onShow={showField('validUntil')}
                label="Valid until"
                value={request.validUntil ? new Date(`${request.validUntil}T00:00:00`).toLocaleDateString('en-US', { dateStyle: 'medium' }) : '—'}
              />
              <Field
                label="Primary commodity group"
                value={request.commodity_groups
                  ? `${request.commodity_groups.category} · ${request.commodity_groups.name}`
                  : '—'}
              />
              <Field label="Total Cost" value={usd(request.total_cost)} />
            </dl>
          </div>

          <div className="lio-card p-6">
            <div className="mb-4 flex items-center justify-between gap-3">
              <h2 className="text-lg font-semibold tracking-tight text-ink">Order Lines</h2>
              {linesDirty && (
                <div className="flex items-center gap-2">
                  <Button size="sm" variant="ghost" disabled={busy} onClick={() => adopt(request)}>
                    Discard
                  </Button>
                  <Button size="sm" loading={busy} disabled={busy} onClick={saveLines}>
                    Save lines
                  </Button>
                </div>
              )}
            </div>
            {linesDirty && request.approval_status === 'approved' && (
              <p className="mb-3 rounded-lg bg-amber-50 px-3 py-2 text-sm text-amber-800">
                This request is approved. If saving changes the total, it goes back to Open and pending
                approval unless the new total is below your organization&apos;s approval threshold.
              </p>
            )}
            {catalogLoading && (
              <p className="mb-3 text-sm text-ink/45">Looking for matching catalog articles…</p>
            )}
            <OrderLinesEditor
              renderLineExtra={(line) => (
                <CatalogSuggestion
                  line={line}
                  editable={linesEditable}
                  suggestions={catalog[line.key]}
                  applied={line.articleId ? appliedArticles[line.articleId] : undefined}
                  onApply={(s) => {
                    setAppliedArticles((a) => ({ ...a, [s.articleId]: s }));
                    setLines((ls) => ls.map((l) => (l.key === line.key ? applyArticle(l, s) : l)));
                    setLinesDirty(true);
                  }}
                />
              )}
              groups={commodityGroups}
              lines={lines}
              taxTotal={taxTotal}
              editable={linesEditable}
              lockedReason="Lines can't be changed once the request is closed."
              onChange={(next, tax) => {
                setLines(next);
                setTaxTotal(tax);
                setLinesDirty(true);
              }}
              onShowSource={pdfUrl ? (line) => docPanel.current?.showSource(line) : undefined}
            />
            <WarningList
              className="mt-4"
              title="Extraction notes"
              warnings={normalizeWarnings(request.extractionWarnings)}
              onShowSource={pdfUrl ? (sources) => docPanel.current?.showSource({ sources }) : undefined}
            />
          </div>

          <div className="lio-card p-6">
            <h2 className="text-lg font-semibold tracking-tight text-ink mb-4">Activity</h2>
            <ol className="relative border-l border-line ml-2">
              {activity.map((a) => (
                <li key={a.id} className="ml-4 pb-5 last:pb-0">
                  <div className="absolute -left-[5px] mt-1.5 h-2.5 w-2.5 rounded-full bg-accent-deep" />
                  <p className="text-sm text-ink">{a.summary}</p>
                  <p className="text-xs text-ink/45 mt-0.5">
                    {new Date(a.created_at).toLocaleString('en-US')}
                  </p>
                </li>
              ))}
              {activity.length === 0 && <p className="text-sm text-ink/50 ml-2">No activity yet.</p>}
            </ol>
          </div>
        </div>

        {/* Right column: PDF viewer */}
        <div className="lg:sticky lg:top-6 self-start">
          {pdfUrl ? (
            <DocumentPanel ref={docPanel} fileUrl={pdfUrl} title="Original Document" />
          ) : (
            <div className="lio-card p-6">
              <h2 className="text-lg font-semibold tracking-tight text-ink mb-4">Original Document</h2>
              <div className="h-[720px] rounded-lg border border-dashed border-line flex items-center justify-center text-center px-6">
                <p className="text-sm text-ink/45">
                  No PDF attached. Upload one when creating a request to see it here.
                </p>
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Edit modal */}
      {editing && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center p-4"
          style={{ backgroundColor: 'rgba(0,0,0,0.3)', backdropFilter: 'blur(4px)' }}
          onClick={() => setEditing(false)}
        >
          <div
            className="bg-white rounded-2xl shadow-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="border-b border-line p-6">
              <h2 className="text-2xl font-semibold tracking-tight text-ink">Edit Request</h2>
            </div>
            <div className="p-6 space-y-4">
              <div className="grid grid-cols-2 gap-4">
                <Input label="Requestor Name *" value={form.requestorName}
                  onChange={(e) => setForm({ ...form, requestorName: e.target.value })} />
                <Input label="Department" value={form.department}
                  onChange={(e) => setForm({ ...form, department: e.target.value })} />
              </div>
              <Input label="Title *" value={form.title}
                onChange={(e) => setForm({ ...form, title: e.target.value })} />
              <div className="grid grid-cols-2 gap-4">
                <Input label="Vendor Name *" value={form.vendorName}
                  onChange={(e) => setForm({ ...form, vendorName: e.target.value })} />
                <Input label="Tax ID" value={form.vatId}
                  onChange={(e) => setForm({ ...form, vatId: e.target.value })} />
              </div>
              <p className="text-xs text-ink/45">
                Order lines, commodity groups and totals are edited in the Order Lines card.
              </p>
            </div>
            <div className="border-t border-line p-6 bg-ink-800/[0.02] flex gap-3">
              <Button onClick={saveEdit} loading={busy} disabled={busy} className="flex-1">Save Changes</Button>
              <Button variant="outline" onClick={() => setEditing(false)} disabled={busy} className="flex-1">
                Cancel
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, value, onShow }: { label: string; value: string; onShow?: () => void }) {
  return (
    <div>
      <dt className="flex h-5 items-center gap-1 text-ink/45 font-medium">
        {label}
        {onShow && <SourceButton className="h-6 w-6 -my-0.5" label={`Show ${label.toLowerCase()} in document`} onClick={onShow} />}
      </dt>
      <dd className="text-ink mt-0.5">{value}</dd>
    </div>
  );
}
