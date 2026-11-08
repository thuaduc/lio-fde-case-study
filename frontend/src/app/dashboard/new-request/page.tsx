'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useRouter } from 'next/navigation';
import * as api from '@/lib/api';
import { useCommodityGroups } from '@/lib/hooks/useCommodityGroups';
import { Button, Input, Alert, Loader } from '@/components/base';
import OrderLinesEditor from '@/components/OrderLinesEditor';
import SourceButton from '@/components/SourceButton';
import DocumentPanel, { type DocumentPanelHandle } from '@/components/DocumentPanel';
import { type Line, computeTotals, linesForSave, normalizeLines } from '@/lib/orderLines';

const LAST_DEPARTMENT_KEY = 'lio.lastDepartment';
const readLastDepartment = () => {
  try {
    return window.localStorage.getItem(LAST_DEPARTMENT_KEY) ?? '';
  } catch {
    return '';
  }
};

/** Everything a draft keeps (the PDF is stored separately as the draft's document). */
interface DraftData {
  requestorName: string;
  title: string;
  vendorName: string;
  vatId: string;
  department: string;
  commodityGroupId: number | null;
  orderLines: Line[];
  taxTotal: number;
  quotedGrandTotal: number | null;
  extractionWarnings: string[];
  fieldSources: api.FieldSources;
  totalCost: number;
}

type SaveState = { kind: 'idle' } | { kind: 'saving' } | { kind: 'saved'; at: string } | { kind: 'error' };

const isPdfFile = (f: File) => f.type === 'application/pdf' || /\.pdf$/i.test(f.name);

export default function NewRequestPage() {
  const router = useRouter();
  const { data: commodityGroups } = useCommodityGroups();

  // Form state
  const [requestorName, setRequestorName] = useState('');
  const [title, setTitle] = useState('');
  const [vendorName, setVendorName] = useState('');
  const [vatId, setVatId] = useState('');
  const [department, setDepartment] = useState('');
  const [commodityGroupId, setCommodityGroupId] = useState<number | null>(null);
  const [orderLines, setOrderLines] = useState<Line[]>([]);
  const [taxTotal, setTaxTotal] = useState<number>(0);
  const [quotedGrandTotal, setQuotedGrandTotal] = useState<number | null>(null);
  const [extractionWarnings, setExtractionWarnings] = useState<string[]>([]);
  const [fieldSources, setFieldSources] = useState<api.FieldSources>({});

  /** All boxes for the given header fields, for one "show in document" click. */
  const boxesFor = (...keys: (keyof api.FieldSources)[]) =>
    keys.flatMap((k) => fieldSources[k] ?? []);
  const sectionHeading = (title: string, boxes: api.SourceBox[]) => (
    <div className="mb-4 flex items-center justify-between gap-2">
      <h2 className="text-xl font-semibold tracking-tight text-ink">{title}</h2>
      {fileUrl && boxes.length > 0 && (
        <SourceButton
          label={`Show ${title.toLowerCase()} in document`}
          onClick={() => docPanel.current?.showSource({ sources: boxes })}
        />
      )}
    </div>
  );
  const totals = computeTotals(orderLines, taxTotal);

  // ---- Drafts (server-side, Gmail-style): load ?draft=<id>, autosave, delete on submit.
  const [draftId, setDraftId] = useState<string | null>(null);
  const draftIdRef = useRef<string | null>(null);
  const creatingDraft = useRef<Promise<string> | null>(null);
  const [draftDocUploaded, setDraftDocUploaded] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>({ kind: 'idle' });
  const submitted = useRef(false);

  const rememberDraftId = (id: string) => {
    draftIdRef.current = id;
    setDraftId(id);
    // Keep the URL shareable with yourself: a refresh reopens this draft.
    window.history.replaceState(null, '', `/dashboard/new-request?draft=${id}`);
  };

  useEffect(() => {
    (async () => {
      const id = new URLSearchParams(window.location.search).get('draft');
      if (id) {
        try {
          const d = await api.drafts.get<DraftData>(id);
          const data = d.data;
          setRequestorName(data.requestorName ?? '');
          setTitle(data.title ?? '');
          setVendorName(data.vendorName ?? '');
          setVatId(data.vatId ?? '');
          setDepartment(data.department ?? '');
          setCommodityGroupId(data.commodityGroupId ?? null);
          setOrderLines(normalizeLines(data.orderLines ?? []));
          setTaxTotal(data.taxTotal ?? 0);
          setQuotedGrandTotal(data.quotedGrandTotal ?? null);
          setExtractionWarnings(data.extractionWarnings ?? []);
          setFieldSources(data.fieldSources ?? {});
          if (d.hasDocument) {
            const url = await api.drafts.documentUrl(id);
            if (url) {
              const blob = await (await fetch(url)).blob();
              URL.revokeObjectURL(url);
              setFile(new File([blob], d.fileName ?? 'document.pdf', { type: 'application/pdf' }));
              setDraftDocUploaded(true);
            }
          }
          rememberDraftId(id);
          setSaveState({ kind: 'saved', at: d.updatedAt });
        } catch {
          setError('This draft no longer exists. Starting a new request.');
          window.history.replaceState(null, '', '/dashboard/new-request');
        }
      } else {
        // Requestor defaults to the signed-in user; department to the one they used last.
        try {
          const { user, profile } = await api.auth.me();
          setRequestorName((cur) => cur || profile?.full_name || user.email);
        } catch {
          // no default
        }
        setDepartment((cur) => cur || readLastDepartment());
      }
      setLoaded(true);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** Draft id, creating the draft on first need (only one create in flight). */
  const ensureDraft = async (data: DraftData): Promise<string> => {
    if (draftIdRef.current) return draftIdRef.current;
    if (!creatingDraft.current) {
      creatingDraft.current = api.drafts.create(data).then((d) => {
        rememberDraftId(d.id);
        return d.id;
      });
      creatingDraft.current.catch(() => {
        creatingDraft.current = null;
      });
    }
    return creatingDraft.current;
  };

  // Document panel (side-by-side PDF with search-to-highlight)
  const docPanel = useRef<DocumentPanelHandle>(null);

  // UI state
  const [file, setFile] = useState<File | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [warning, setWarning] = useState('');
  const [extracting, setExtracting] = useState(false);

  const fileUrl = useMemo(() => (file ? URL.createObjectURL(file) : null), [file]);

  const draftData: DraftData = {
    requestorName, title, vendorName, vatId, department, commodityGroupId, orderLines,
    taxTotal, quotedGrandTotal, extractionWarnings, fieldSources, totalCost: totals.grand,
  };

  // Autosave ~1s after the last change, once there is something worth keeping.
  const draftJson = JSON.stringify(draftData);
  useEffect(() => {
    if (!loaded || submitted.current) return;
    const worthKeeping = !!file || orderLines.length > 0 || !!title || !!vendorName;
    if (!worthKeeping) return;
    const t = setTimeout(async () => {
      setSaveState({ kind: 'saving' });
      try {
        const data = JSON.parse(draftJson) as DraftData;
        const id = await ensureDraft(data);
        const summary = await api.drafts.update(id, data);
        setSaveState({ kind: 'saved', at: summary.updatedAt });
      } catch {
        setSaveState({ kind: 'error' });
      }
    }, 1000);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftJson, loaded, !!file]);

  // The PDF goes to the draft once (it is the draft's document from then on).
  useEffect(() => {
    if (!loaded || !file || draftDocUploaded || submitted.current) return;
    (async () => {
      try {
        const id = await ensureDraft(JSON.parse(draftJson) as DraftData);
        await api.drafts.uploadDocument(id, file);
        setDraftDocUploaded(true);
      } catch {
        setSaveState({ kind: 'error' });
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, loaded, draftDocUploaded]);

  const discardDraft = async () => {
    if (!confirm('Discard this draft? This cannot be undone.')) return;
    submitted.current = true;
    if (draftIdRef.current) await api.drafts.delete(draftIdRef.current).catch(() => {});
    router.push('/dashboard/requests');
  };

  // Handle file upload and extraction
  const handleFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const uploadedFile = e.target.files?.[0];
    if (!uploadedFile) return;

    if (!isPdfFile(uploadedFile)) {
      setError('Please upload a PDF file');
      return;
    }

    setFile(uploadedFile);
    setDraftDocUploaded(false);
    setError('');
    setSuccess('');
    setWarning('');
    setExtractionWarnings([]);
    setFieldSources({});
    setExtracting(true);

    try {
      // One call: rows + boxes from the PDF, structured lines, classification.
      const extractionResult = await api.extraction.extractPdf(uploadedFile);

      if (!extractionResult.success || !extractionResult.data) {
        throw new Error(extractionResult.error || 'Failed to extract data');
      }

      const extracted = extractionResult.data;

      // Step 3: Auto-fill form (including commodity group from extraction)
      setTitle(extracted.title || '');
      setVendorName(extracted.vendorName || '');
      setVatId(extracted.vatId || '');
      // Only a department the quote actually states replaces the default.
      if (extracted.department) setDepartment(extracted.department);
      setOrderLines(normalizeLines(extracted.orderLines || []));
      setTaxTotal(extracted.taxTotal ?? 0);
      setQuotedGrandTotal(extracted.totalCost || null);
      setExtractionWarnings(extractionResult.warnings ?? []);
      setFieldSources(extracted.fieldSources ?? {});

      // Set commodity group (now included in extraction)
      if (extracted.commodityGroupId) {
        setCommodityGroupId(extracted.commodityGroupId);
      }

      // Show warnings for missing fields
      // Department not on the quote is fine when it is already filled from the default.
      const missing = (extractionResult.missingFields ?? []).filter(
        (f) => !(f === 'Department' && (extracted.department || department)),
      );
      if (missing.length > 0) {
        setWarning(
          `⚠️ Could not extract: ${missing.join(', ')}. Please fill in these fields manually.`
        );
      }

      setSuccess('✓ PDF extracted successfully! Review and submit.');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } catch (err) {
      console.error('Extraction error:', err);
      setError(err instanceof Error ? err.message : 'Failed to extract data from PDF');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    } finally {
      setExtracting(false);
    }
  };

  // Submit form
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError('');
    setSuccess('');
    setWarning('');

    // Validation with scroll to top on error
    if (!commodityGroupId) {
      setError('Please select a commodity group');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    const linesToSave = linesForSave(orderLines);
    if (linesToSave.length === 0) {
      setError('Please add at least one order line');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }

    try {
      submitted.current = true;
      const created = await api.requests.create({
        draftId: draftIdRef.current,
        requestorName,
        titleShortDescription: title,
        vendorName,
        vatId,
        commodityGroupId: commodityGroupId,
        totalCost: totals.grand,
        netTotal: totals.net,
        taxTotal,
        fieldSources,
        department,
        orderLines: linesToSave,
      });

      try {
        if (department) window.localStorage.setItem(LAST_DEPARTMENT_KEY, department);
      } catch {
        // Storage unavailable; the default is a convenience only.
      }

      // Attach the original PDF so it shows on the detail page. A draft's PDF was
      // already moved to the request by the server.
      if (file && !(draftIdRef.current && draftDocUploaded)) {
        try {
          await api.requests.uploadDocument(created.id, file);
        } catch {
          // Non-fatal — the request itself was created.
        }
      }

      setSuccess('Request created successfully! Redirecting...');
      window.scrollTo({ top: 0, behavior: 'smooth' });
      setTimeout(() => {
        router.push('/dashboard/requests');
        router.refresh();
      }, 1500);
    } catch (err) {
      submitted.current = false;
      setError(err instanceof Error ? err.message : 'Failed to create request');
      window.scrollTo({ top: 0, behavior: 'smooth' });
    }
  };

  return (
    <div className={`${fileUrl ? 'max-w-[1600px]' : 'max-w-5xl'} mx-auto px-4 sm:px-6 lg:px-8 py-8`}>
      {/* Header */}
      <div className="mb-8 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-3xl font-semibold tracking-tight text-ink">New Procurement Request</h1>
          <p className="text-gray-600 mt-2">
            Upload a vendor offer PDF or fill in the details manually
          </p>
        </div>
        {(draftId || saveState.kind !== 'idle') && (
          <div className="flex items-center gap-4 pt-2 text-sm">
            <span className={saveState.kind === 'error' ? 'text-red-600' : 'text-ink/50'}>
              {saveState.kind === 'saving' && 'Saving draft…'}
              {saveState.kind === 'saved' &&
                `Draft saved ${new Date(saveState.at).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' })}`}
              {saveState.kind === 'error' && 'Couldn’t save draft'}
            </span>
            {draftId && (
              <button type="button" onClick={discardDraft} className="font-medium text-red-600 hover:text-red-700">
                Discard draft
              </button>
            )}
          </div>
        )}
      </div>

      {error && <Alert variant="error" className="mb-6">{error}</Alert>}
      {warning && <Alert variant="warning" className="mb-6">{warning}</Alert>}
      {success && <Alert variant="success" className="mb-6">{success}</Alert>}

      <div className={fileUrl ? 'grid grid-cols-1 xl:grid-cols-2 gap-6 items-start' : ''}>
      <form onSubmit={handleSubmit} className="space-y-8 min-w-0">
        {/* PDF Upload Section */}
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Upload Vendor Offer (Optional)</h2>
          <p className="text-sm text-gray-600 mb-4">
            Upload a PDF to automatically extract vendor details and order items
          </p>

          <div className="border-2 border-dashed border-gray-300 rounded-lg p-8 text-center hover:border-accent-deep/50 transition-colors">
            <input
              type="file"
              accept="application/pdf,.pdf"
              onChange={handleFileChange}
              className="hidden"
              id="pdf-upload"
              disabled={extracting}
            />
            <label htmlFor="pdf-upload" className="cursor-pointer">
              {extracting ? (
                <div className="flex flex-col items-center">
                  <Loader size="lg" />
                  <p className="mt-4 text-sm text-gray-600">Extracting data from PDF...</p>
                </div>
              ) : file ? (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-green-500"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M5 13l4 4L19 7"
                    />
                  </svg>
                  <p className="mt-2 text-sm font-medium text-gray-900">{file.name}</p>
                  <p className="mt-1 text-xs text-gray-500">Click to upload a different file</p>
                </div>
              ) : (
                <div>
                  <svg
                    className="mx-auto h-12 w-12 text-gray-400"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
                    />
                  </svg>
                  <p className="mt-2 text-sm text-gray-600">
                    Click to upload or drag and drop
                  </p>
                  <p className="mt-1 text-xs text-gray-500">PDF up to 10MB</p>
                </div>
              )}
            </label>
          </div>
        </div>

        {/* Request Details */}
        <div className="lio-card p-6 space-y-4">
          {sectionHeading('Request Details', boxesFor('department', 'quoteNumber', 'validUntil'))}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input
              label="Requestor Name *"
              value={requestorName}
              onChange={(e) => setRequestorName(e.target.value)}
              required
              placeholder="John Doe"
            />

            <Input
              label="Department"
              value={department}
              onChange={(e) => setDepartment(e.target.value)}
              placeholder="IT Department"
            />
          </div>

          <Input
            label="Title / Short Description *"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            required
            placeholder="Adobe Creative Cloud Subscription"
          />
        </div>

        {/* Vendor Information */}
        <div className="lio-card p-6 space-y-4">
          {sectionHeading('Vendor Information', boxesFor('vendorName', 'vatId'))}

          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Input
              label="Vendor Name *"
              value={vendorName}
              onChange={(e) => setVendorName(e.target.value)}
              required
              placeholder="Adobe Systems"
            />

            <Input
              label="Tax ID"
              value={vatId}
              onChange={(e) => setVatId(e.target.value)}
              placeholder="12-3456789"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-2">
              Commodity Group *
            </label>
            <select
              value={commodityGroupId || ''}
              onChange={(e) => setCommodityGroupId(Number(e.target.value))}
              required
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-gray-900 focus:ring-2 focus:ring-accent-deep/60 focus:border-accent-deep/40"
            >
              <option value="">
                {commodityGroups ? 'Select a commodity group' : 'Loading...'}
              </option>
              {commodityGroups?.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.id} - {group.category} - {group.name}
                </option>
              ))}
            </select>
          </div>
        </div>

        {/* Order Lines */}
        <div className="lio-card p-6">
          <h2 className="text-xl font-semibold tracking-tight text-ink mb-4">Order Lines *</h2>
          {extractionWarnings.length > 0 && (
            <Alert variant="warning" className="mb-4">
              <ul className="list-disc pl-4 space-y-0.5">
                {extractionWarnings.map((w) => (
                  <li key={w}>{w}</li>
                ))}
              </ul>
            </Alert>
          )}
          <OrderLinesEditor
            lines={orderLines}
            taxTotal={taxTotal}
            editable
            quotedGrandTotal={quotedGrandTotal}
            onChange={(lines, tax) => {
              setOrderLines(lines);
              setTaxTotal(tax);
            }}
            onShowSource={fileUrl ? (line) => docPanel.current?.showSource(line) : undefined}
          />
        </div>

        {/* Submit */}
        <div className="flex items-center justify-end space-x-4">
          <Button
            type="button"
            variant="outline"
            onClick={() => router.back()}
          >
            Cancel
          </Button>
          <Button type="submit" size="lg">
            Create Request
          </Button>
        </div>
      </form>
      {fileUrl && (
        <div className="xl:sticky xl:top-6">
          <DocumentPanel ref={docPanel} fileUrl={fileUrl} title={file?.name} />
        </div>
      )}
      </div>
    </div>
  );
}

